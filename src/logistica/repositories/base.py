"""Traducción de errores de los drivers a la jerarquía de `core.errors`.

Los repositorios envuelven cada acceso a un motor con `translate_errors(...)`. Así los servicios y la UI
solo conocen errores propios (`IntegrityError`, `DatabaseUnavailableError`, ...) y el error original queda
encadenado (`__cause__`) para el log.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import neo4j.exceptions as neo4j_exc
import pymongo.errors as mongo_exc
import sqlalchemy.exc as sa_exc

from logistica.core import errors

log = logging.getLogger(__name__)

# Códigos de MongoDB: "Document failed validation" ($jsonSchema) y clave duplicada.
_MONGO_VALIDATION_CODE = 121
_MONGO_DUPLICATE_KEY_CODE = 11000

# Clases SQLSTATE de PostgreSQL que sí significan "no hay base": 08 = falla de conexión,
# 57P = el servidor se está apagando o no acepta conexiones. Otros OperationalError (deadlock 40P01,
# serialización 40001, statement_timeout 57014) son problemas de la operación, no de disponibilidad:
# se propagan sin traducir para que el servicio decida si reintenta.
_PG_UNAVAILABLE_SQLSTATE = ("08", "57P")


def _pg_is_unavailable(exc: sa_exc.DBAPIError) -> bool:
    if exc.connection_invalidated:
        return True
    sqlstate = getattr(exc.orig, "sqlstate", None)
    # Sin SQLSTATE = el error ocurrió antes de hablar con el servidor (conexión rechazada, timeout).
    return sqlstate is None or sqlstate.startswith(_PG_UNAVAILABLE_SQLSTATE)


def _mongo_bulk_codes(exc: mongo_exc.BulkWriteError) -> set[int]:
    return {e.get("code") for e in (exc.details or {}).get("writeErrors", [])}


def _translate(engine: str, exc: Exception) -> errors.LogisticaError | None:
    # --- PostgreSQL (SQLAlchemy envuelve a psycopg) ---
    if isinstance(exc, sa_exc.IntegrityError):
        return errors.IntegrityError(str(exc.orig or exc))
    if isinstance(exc, sa_exc.DataError):  # texto más largo que la columna, número fuera de rango, etc.
        return errors.ValidationError(
            str(exc.orig or exc), user_message="Algún dato excede el tamaño o formato permitido."
        )
    if isinstance(exc, sa_exc.OperationalError):
        return errors.DatabaseUnavailableError(engine, str(exc)) if _pg_is_unavailable(exc) else None
    if isinstance(exc, (sa_exc.InterfaceError, sa_exc.TimeoutError)):  # TimeoutError = pool agotado
        return errors.DatabaseUnavailableError(engine, str(exc))
    # --- MongoDB (DuplicateKeyError es subclase de WriteError: va primero) ---
    if isinstance(exc, mongo_exc.DuplicateKeyError):
        return errors.IntegrityError(str(exc))
    if isinstance(exc, mongo_exc.WriteError) and exc.code == _MONGO_VALIDATION_CODE:
        return errors.ValidationError(str(exc))
    if isinstance(exc, mongo_exc.BulkWriteError):  # insert_many / bulk_write
        codes = _mongo_bulk_codes(exc)
        if _MONGO_DUPLICATE_KEY_CODE in codes:
            return errors.IntegrityError(str(exc))
        if _MONGO_VALIDATION_CODE in codes:
            return errors.ValidationError(str(exc))
    if isinstance(exc, (mongo_exc.ConnectionFailure, mongo_exc.ServerSelectionTimeoutError)):
        return errors.DatabaseUnavailableError(engine, str(exc))
    # --- Neo4j ---
    if isinstance(exc, neo4j_exc.ConstraintError):
        return errors.IntegrityError(str(exc))
    # TransientError (deadlock, líder no disponible): se puede reintentar más tarde, como una caída.
    if isinstance(
        exc,
        (
            neo4j_exc.ServiceUnavailable,
            neo4j_exc.SessionExpired,
            neo4j_exc.AuthError,
            neo4j_exc.TransientError,
        ),
    ):
        return errors.DatabaseUnavailableError(engine, str(exc))
    return None


@contextmanager
def translate_errors(engine: str) -> Iterator[None]:
    """Convierte errores conocidos de driver en errores de la aplicación; el resto se propaga igual."""
    try:
        yield
    except errors.LogisticaError:
        raise
    except Exception as exc:
        translated = _translate(engine, exc)
        if translated is None:
            raise
        raise translated from exc
