"""Repositorios de PostgreSQL (fuente principal de datos operativos y comerciales)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.orm import Session

from logistica.repositories.base import translate_errors
from logistica.repositories.connections import Connections

ENGINE = "PostgreSQL"


@contextmanager
def unit_of_work(connections: Connections) -> Iterator[Session]:
    """Una transacción de PostgreSQL para una operación de servicio.

    El servicio abre la unidad de trabajo y se la pasa a cada repositorio que usa: todo se confirma junto
    al salir del bloque o se revierte ante cualquier error. Los errores del driver salen traducidos.
    """
    with translate_errors(ENGINE), connections.pg_session() as session:
        yield session


class PostgresRepository:
    """Base de los repositorios de PostgreSQL.

    Cada operación pública abre su propia unidad de trabajo con `_session()` (commit al salir bien, rollback
    ante error) o recibe una `Session` del servicio cuando varias operaciones deben confirmarse juntas.
    Nunca se comparte una `Session` entre hilos.
    """

    def __init__(self, connections: Connections):
        self._conn = connections

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with translate_errors(ENGINE), self._conn.pg_session() as session:
            yield session
