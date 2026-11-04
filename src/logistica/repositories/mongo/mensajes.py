"""Texto inmutable por UUID; confirmación de persistencia y paginación descendente."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from pymongo import ReturnDocument, timeout
from pymongo.write_concern import WriteConcern

from logistica.core.errors import IntegrityError
from logistica.domain.comunicacion import (
    CursorMensaje,
    MensajeDTO,
    ParticipanteDTO,
    techo_milisegundo,
)
from logistica.repositories.mongo import MongoRepository


def _dto(doc: dict) -> MensajeDTO:
    return MensajeDTO(
        doc["_id"], doc["conversacion_id"], doc["remitente_id"],
        doc["fecha_envio"].replace(tzinfo=timezone.utc), doc["contenido"],
    )


class MensajesRepository(MongoRepository):
    COLLECTION = "mensajes"

    def guardar(self, mensaje: MensajeDTO) -> MensajeDTO:
        """Un reintento conserva fecha y texto; un UUID con otro contenido se rechaza."""
        campos = {
            "conversacion_id": mensaje.conversacion_id,
            "remitente_id": mensaje.remitente_id,
            "fecha_envio": mensaje.fecha_envio,
            "contenido": mensaje.contenido,
            "tipo": "TEXTO",
        }
        with timeout(5), self._collection() as collection:
            collection = collection.with_options(write_concern=WriteConcern(w="majority"))
            doc = collection.find_one_and_update(
                {"_id": mensaje.id}, {"$setOnInsert": campos},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
        if any(doc[k] != campos[k] for k in ("conversacion_id", "remitente_id", "contenido", "tipo")):
            raise IntegrityError(
                "UUID de mensaje reutilizado con otro contenido",
                user_message="Ese identificador ya corresponde a otro mensaje. No se modificó.",
            )
        return _dto(doc)

    def historial(
        self, ident: UUID, periodos: tuple[ParticipanteDTO, ...], hasta: datetime,
        antes: CursorMensaje | None, limite: int,
    ) -> tuple[MensajeDTO, ...]:
        condiciones: list[dict] = [
            {"conversacion_id": ident}, {"fecha_envio": {"$lt": hasta}},
            {"$or": [
                {"fecha_envio": {
                    "$gte": techo_milisegundo(m.ingreso_en),
                    **({"$lt": techo_milisegundo(m.salida_en)} if m.salida_en else {}),
                }}
                for m in periodos
            ]},
        ]
        if antes:
            fecha, ident_ultimo = antes
            condiciones.append({"$or": [
                {"fecha_envio": {"$lt": fecha}},
                {"fecha_envio": fecha, "_id": {"$lt": ident_ultimo}},
            ]})
        with timeout(5), self._collection() as collection:
            documentos = list(
                collection.find({"$and": condiciones})
                .sort([("fecha_envio", -1), ("_id", -1)]).limit(limite)
            )
        return tuple(_dto(doc) for doc in documentos)
