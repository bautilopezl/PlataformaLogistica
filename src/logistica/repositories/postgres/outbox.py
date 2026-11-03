"""Repositorio de la outbox (tablas `outbox_evento` y `outbox_entrega`).

El servicio registra el evento EN LA MISMA transacción que el cambio de negocio: si el cambio se confirma,
el evento también, y un trabajador (fase 3) lo entregará a cada destino al menos una vez. El `id` del evento
es el `_id` del documento en Mongo, así una reentrega no duplica.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.logistica_dtos import EventoOutboxDTO

NEO4J = "NEO4J_LOGISTICA"
MONGO = "MONGO_EVENTOS"


class OutboxRepository:
    def registrar(
        self,
        tx: Session,
        *,
        agregado_tipo: str,
        agregado_id: uuid.UUID,
        agregado_version: int,
        evento_tipo: str,
        payload: dict[str, Any],
        destinos: tuple[str, ...],
    ) -> uuid.UUID:
        evento_id = uuid.uuid4()
        tx.execute(
            text(
                "INSERT INTO outbox_evento (id, agregado_tipo, agregado_id, agregado_version, evento_tipo, "
                "payload)"
                " VALUES (:id, :tipo, :agregado, :version, :evento, CAST(:payload AS jsonb))"
            ),
            {
                "id": evento_id,
                "tipo": agregado_tipo,
                "agregado": agregado_id,
                "version": agregado_version,
                "evento": evento_tipo,
                "payload": json.dumps({"event_id": str(evento_id), **payload}, default=_json),
            },
        )
        for destino in destinos:
            tx.execute(
                text("INSERT INTO outbox_entrega (evento_id, destino) VALUES (:id, :destino)"),
                {"id": evento_id, "destino": destino},
            )
        return evento_id

    def de_agregado(self, tx: Session, agregado_id: uuid.UUID) -> list[EventoOutboxDTO]:
        rows = tx.execute(
            text(
                """
                SELECT e.id, e.evento_tipo, e.agregado_version, e.creado_en, e.payload ->> 'tipo' AS tipo,
                       bool_or(x.estado <> 'COMPLETADA') AS pendiente
                FROM outbox_evento e JOIN outbox_entrega x ON x.evento_id = e.id
                WHERE e.agregado_id = :id
                GROUP BY e.id
                ORDER BY e.agregado_version
                """
            ),
            {"id": agregado_id},
        )
        return [
            EventoOutboxDTO(r.id, r.evento_tipo, r.agregado_version, r.creado_en, r.tipo, r.pendiente)
            for r in rows
        ]


def _json(valor: Any) -> str:
    """Fechas en ISO 8601 con zona (las lee `datetime.fromisoformat`); UUID y Decimal como texto."""
    if isinstance(valor, datetime):
        if valor.tzinfo is None:
            raise ValueError("fecha sin zona horaria en un evento de outbox")
        return valor.isoformat()
    return str(valor)
