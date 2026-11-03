"""Contrato outbox → `eventos_logisticos` (Mongo). Lo usará el consumidor de la fase 3.

El payload de la outbox es JSON (texto): los UUID y las fechas viajan como cadenas. Esta función los vuelve a
convertir a los tipos que exige el validador $jsonSchema (UUID binData, BSON Date UTC). El `_id` del documento
es el `event_id` de la outbox: una reentrega no duplica (inserción idempotente por `_id`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from logistica.repositories.mongo import MongoRepository

_UUIDS = ("envio_id", "tramo_id", "ubicacion_id", "usuario_id")


def documento_evento(payload: dict[str, Any], *, fecha_registro: datetime) -> dict[str, Any]:
    """Arma el documento de `eventos_logisticos` a partir del payload de un evento de la outbox."""
    doc: dict[str, Any] = {
        "_id": uuid.UUID(str(payload["event_id"])),
        "tipo": payload["tipo"],
        "fecha_ocurrencia": _fecha(payload["fecha_ocurrencia"]),
        "fecha_registro": fecha_registro.astimezone(timezone.utc),
        "detalles": dict(payload.get("detalles") or {}),
        "contenedor_ids": [uuid.UUID(str(c)) for c in payload.get("contenedor_ids", [])],
    }
    for clave in _UUIDS:
        if payload.get(clave):
            doc[clave] = uuid.UUID(str(payload[clave]))
    if payload.get("descripcion"):
        doc["descripcion"] = str(payload["descripcion"])
    return doc


def _fecha(valor: Any) -> datetime:
    fecha = valor if isinstance(valor, datetime) else datetime.fromisoformat(str(valor))
    if fecha.tzinfo is None:
        raise ValueError("fecha sin zona horaria en el payload")
    return fecha.astimezone(timezone.utc)


class EventosRepository(MongoRepository):
    """Histórico de eventos logísticos (colección `eventos_logisticos`, fuente principal de este dato)."""

    COLLECTION = "eventos_logisticos"

    def guardar(self, doc: dict[str, Any]) -> bool:
        """Inserta el documento si no existe (idempotente por `_id`). Devuelve True si lo insertó ahora.

        `$setOnInsert` + `upsert`: una reentrega del mismo evento no lo duplica ni lo modifica. El validador
        $jsonSchema de la colección rechaza un documento inválido (ValidationError).
        """
        campos = {k: v for k, v in doc.items() if k != "_id"}
        with self._collection() as coleccion:
            resultado = coleccion.update_one({"_id": doc["_id"]}, {"$setOnInsert": campos}, upsert=True)
        return resultado.upserted_id is not None

    def de_envio(self, envio_id: uuid.UUID) -> list[dict[str, Any]]:
        with self._collection() as coleccion:
            return list(coleccion.find({"envio_id": envio_id}).sort([("fecha_ocurrencia", 1), ("_id", 1)]))
