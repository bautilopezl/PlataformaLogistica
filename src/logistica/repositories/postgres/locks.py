"""Advisory locks de PostgreSQL para coordinar procesos periódicos entre instancias (AGENTS.md §12).

Reglas:
- El lock es de SESIÓN y se toma en una conexión DEDICADA, distinta de la que usa la tarea: así la tarea puede
  hacer sus propias transacciones (commit/rollback) sin soltar el lock por accidente.
- `pg_try_advisory_lock` no espera: si otra instancia lo tiene, esta corrida se omite (no se encolan).
- Se libera SIEMPRE en `finally`. Si no se pudo liberar, la conexión se INVALIDA (se cierra de verdad) en
  lugar de volver al pool: una conexión con un lock activo en el pool bloquearía esa tarea para siempre.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Engine

from logistica.repositories.base import translate_errors
from logistica.repositories.postgres import ENGINE

log = logging.getLogger(__name__)

# hashtextextended convierte la clave legible ("proceso:CIERRE_MENSUAL:2026-10") en el bigint del lock.
_TOMAR = text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))")
_SOLTAR = text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))")


@contextmanager
def advisory_lock(engine: Engine, clave: str) -> Iterator[bool]:
    """`with advisory_lock(engine, clave) as tomado:` → True si esta instancia tiene el lock durante el
    bloque."""
    with translate_errors(ENGINE):
        conexion = engine.connect()
    tomado = False
    respuesta_conocida = False  # ¿sabemos con certeza si el servidor nos dio el lock?
    try:
        with translate_errors(ENGINE):
            tomado = bool(conexion.execute(_TOMAR, {"k": clave}).scalar_one())
            respuesta_conocida = True
            conexion.commit()  # el lock es de sesión: sobrevive al fin de esta transacción
        yield tomado
    finally:
        # La conexión vuelve al pool SOLO si se confirmó que no conserva el lock: o nunca lo tuvo (respuesta
        # conocida y negativa) o se liberó y el servidor lo confirmó. Ante cualquier duda (un error al tomarlo
        # o al soltarlo, incluso una interrupción con Ctrl+C) se invalida: al cerrarse, PostgreSQL suelta el
        # lock.
        segura = respuesta_conocida and not tomado
        try:
            if tomado:
                segura = bool(conexion.execute(_SOLTAR, {"k": clave}).scalar_one())
                conexion.commit()
        except Exception:  # noqa: BLE001 — se registra; la conexión se invalida abajo
            log.exception("No se pudo liberar el lock %s", clave)
        finally:
            if not segura:
                conexion.invalidate()  # nunca devolver al pool una conexión que pueda conservar el lock
            conexion.close()
