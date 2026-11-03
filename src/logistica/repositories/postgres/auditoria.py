"""Repositorio de la tabla `auditoria` (solo inserta y consulta: la tabla es inmutable)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.auditoria import AuditoriaDTO, PaginaAuditoria


class AuditoriaRepository:
    def insertar(
        self,
        tx: Session,
        *,
        accion: str,
        resultado: str,
        usuario_id: uuid.UUID | None,
        sesion_id: uuid.UUID | None,
        instancia_id: uuid.UUID | None,
        entidad: str | None,
        entidad_id: uuid.UUID | None,
        detalle: dict[str, Any],
    ) -> uuid.UUID:
        registro_id = uuid.uuid4()
        tx.execute(
            text(
                "INSERT INTO auditoria (id, accion, resultado, usuario_id, sesion_id, instancia_id, entidad, "
                "entidad_id, detalle) VALUES (:id, :accion, :resultado, :usuario, :sesion, :instancia, "
                ":entidad, :entidad_id, CAST(:detalle AS jsonb))"
            ),
            {
                "id": registro_id,
                "accion": accion,
                "resultado": resultado,
                "usuario": usuario_id,
                "sesion": sesion_id,
                "instancia": instancia_id,
                "entidad": entidad,
                "entidad_id": entidad_id,
                "detalle": json.dumps(detalle, default=str),
            },
        )
        return registro_id

    def listar(
        self,
        tx: Session,
        *,
        accion: str | None,
        usuario_id: uuid.UUID | None,
        desde: datetime | None,
        hasta: datetime | None,
        limite: int,
        offset: int,
    ) -> PaginaAuditoria:
        # Se pide un registro de más para saber si hay otra página sin hacer un COUNT(*) sobre toda la tabla.
        rows = tx.execute(
            text(
                """
                SELECT a.id, a.fecha, a.accion, a.resultado, a.usuario_id, u.email, a.entidad, a.entidad_id,
                       a.detalle
                FROM auditoria a
                LEFT JOIN usuario u ON u.id = a.usuario_id
                WHERE (CAST(:accion AS text) IS NULL OR a.accion = :accion)
                  AND (CAST(:usuario AS uuid) IS NULL OR a.usuario_id = :usuario)
                  AND (CAST(:desde AS timestamptz) IS NULL OR a.fecha >= :desde)
                  AND (CAST(:hasta AS timestamptz) IS NULL OR a.fecha < :hasta)
                ORDER BY a.fecha DESC, a.id
                LIMIT :limite OFFSET :offset
                """
            ),
            {
                "accion": accion,
                "usuario": usuario_id,
                "desde": desde,
                "hasta": hasta,
                "limite": limite + 1,
                "offset": offset,
            },
        ).all()
        registros = [
            AuditoriaDTO(
                r.id,
                r.fecha,
                r.accion,
                r.resultado,
                r.usuario_id,
                r.email,
                r.entidad,
                r.entidad_id,
                dict(r.detalle),
            )
            for r in rows[:limite]
        ]
        return PaginaAuditoria(registros, hay_mas=len(rows) > limite)

    def acciones(self, tx: Session) -> list[str]:
        return list(tx.execute(text("SELECT DISTINCT accion FROM auditoria ORDER BY accion")).scalars())
