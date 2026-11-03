"""Registro de auditoría de operaciones relevantes.

Cada registro va a la tabla `auditoria` DENTRO de la transacción de la operación (se pasa su `tx`): si la
operación se confirma, el registro también; si se revierte, no queda ninguno. Además se escribe una línea
en el log (`logistica.auditoria`) para tener rastro aunque la base no responda.

Solo identificadores y un detalle mínimo: nunca contraseñas, hashes ni datos personales innecesarios.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from logistica.domain.acceso import Principal
from logistica.domain.auditoria import EXITO
from logistica.repositories.postgres.auditoria import AuditoriaRepository

log = logging.getLogger("logistica.auditoria")
_repo = AuditoriaRepository()


def registrar(
    tx: Any,
    accion: str,
    principal: Principal | None,
    *,
    entidad: str | None = None,
    entidad_id: uuid.UUID | None = None,
    resultado: str = EXITO,
    usuario_id: uuid.UUID | None = None,
    sesion_id: uuid.UUID | None = None,
    instancia_id: uuid.UUID | None = None,
    **detalle: Any,
) -> None:
    """Agrega un registro. `usuario_id`/`sesion_id` se usan cuando no hay `principal` (login, logout)."""
    usuario = principal.usuario_id if principal else usuario_id
    sesion = principal.sesion_id if principal else sesion_id
    _repo.insertar(
        tx,
        accion=accion,
        resultado=resultado,
        usuario_id=usuario,
        sesion_id=sesion,
        instancia_id=instancia_id,
        entidad=entidad,
        entidad_id=entidad_id,
        detalle=detalle,
    )
    # En el log, el id de sesión va abreviado: mientras está abierta funciona como credencial.
    log.info(
        "accion=%s resultado=%s usuario=%s sesion=%s entidad=%s:%s %s",
        accion,
        resultado,
        usuario or "-",
        f"{str(sesion)[:8]}…" if sesion else "-",
        entidad or "-",
        entidad_id or "-",
        " ".join(f"{k}={v}" for k, v in sorted(detalle.items())),
    )
