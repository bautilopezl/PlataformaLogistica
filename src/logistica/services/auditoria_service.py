"""Consulta del registro de auditoría (requiere auditoria.consultar; solo ADMINISTRADOR en la semilla)."""

from __future__ import annotations

import uuid
from datetime import datetime

from logistica.core.errors import ValidationError
from logistica.domain.acceso import Permiso
from logistica.domain.auditoria import PaginaAuditoria
from logistica.repositories.connections import Connections
from logistica.repositories.postgres.auditoria import AuditoriaRepository
from logistica.services.autorizacion import Autorizador

LIMITE_MAXIMO = 500


class AuditoriaService:
    def __init__(self, connections: Connections, autorizador: Autorizador):
        self._conn = connections
        self._aut = autorizador
        self._repo = AuditoriaRepository()

    def listar(
        self,
        sesion_id: uuid.UUID,
        *,
        accion: str | None = None,
        usuario_id: uuid.UUID | None = None,
        desde: datetime | None = None,
        hasta: datetime | None = None,
        limite: int = 100,
        offset: int = 0,
    ) -> PaginaAuditoria:
        """Registros más recientes primero, paginados. `desde`/`hasta` deben tener zona horaria."""
        if not 1 <= limite <= LIMITE_MAXIMO or offset < 0:
            raise ValidationError("paginación inválida", user_message="Parámetros de página inválidos.")
        for fecha in (desde, hasta):
            if fecha is not None and fecha.tzinfo is None:
                raise ValidationError(
                    "fecha sin zona horaria", user_message="Las fechas deben tener zona horaria."
                )
        with self._aut.operacion(sesion_id, Permiso.AUDITORIA_CONSULTAR) as (tx, _p):
            return self._repo.listar(
                tx,
                accion=accion or None,
                usuario_id=usuario_id,
                desde=desde,
                hasta=hasta,
                limite=limite,
                offset=offset,
            )

    def acciones(self, sesion_id: uuid.UUID) -> list[str]:
        """Acciones distintas registradas (para el filtro de la pantalla)."""
        with self._aut.operacion(sesion_id, Permiso.AUDITORIA_CONSULTAR) as (tx, _p):
            return self._repo.acciones(tx)
