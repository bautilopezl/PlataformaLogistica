"""Registros de auditoría (DTO de presentación). La fuente es la tabla `auditoria` de PostgreSQL."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

EXITO = "EXITO"
RECHAZO = "RECHAZO"


@dataclass(frozen=True)
class AuditoriaDTO:
    id: uuid.UUID
    fecha: datetime
    accion: str
    resultado: str
    usuario_id: uuid.UUID | None
    usuario_email: str | None
    entidad: str | None
    entidad_id: uuid.UUID | None
    detalle: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PaginaAuditoria:
    registros: list[AuditoriaDTO]
    hay_mas: bool  # True si existe al menos un registro después de esta página
