"""Repositorio de `lecturas_sensor` (MongoDB, fuente principal de las lecturas IoT).

- Inserción idempotente por `_id` estable: un reenvío del mismo dispositivo no duplica ni modifica.
- Reserva ATÓMICA para evaluar (`find_one_and_update`): dos evaluadores nunca toman la misma lectura; una
  reserva vencida (el evaluador se cayó) se puede retomar. Mismo esquema que la outbox de PostgreSQL:
  `propietario_id` + `version_reclamo`, y solo marca quien sigue siendo dueño.
- Histórico paginado de forma estable por (`fecha_medicion`, `_id`), apoyado en los índices de setup_mongo.py.
Posición en GeoJSON: [longitud, latitud]. Los campos sin medición se omiten (no se guarda null).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument

from logistica.repositories.mongo import MongoRepository

INDICE_POR_CAMPO = {
    "sensor_id": "ix_lectura_sensor_fecha",
    "envio_id": "ix_lectura_envio_fecha",
    "contenedor_id": "ix_lectura_contenedor_fecha",
}


class LecturasRepository(MongoRepository):
    COLLECTION = "lecturas_sensor"

    # ------------------------------------------------------------------ escritura
    def insertar(self, doc: dict[str, Any]) -> bool:
        """Inserta si no existe. Devuelve True si la insertó ahora (False = reenvío ya guardado)."""
        campos = {k: v for k, v in doc.items() if k != "_id"}
        with self._collection() as c:
            r = c.update_one({"_id": doc["_id"]}, {"$setOnInsert": campos}, upsert=True)
        return r.upserted_id is not None

    def reservar(self, *, propietario_id: uuid.UUID, ahora: datetime, reserva_s: float,
                 sensor_ids: list[uuid.UUID] | None = None) -> dict[str, Any] | None:
        """Toma UNA lectura lista para evaluar (la más vieja). None si no hay.

        Lista = PENDIENTE con su próximo intento vencido, o EN_PROCESO con la reserva vencida.
        `sensor_ids` limita a esos sensores (diagnóstico y pruebas).
        """
        filtro: dict[str, Any] = {"$or": [
            {"evaluacion.estado": "PENDIENTE", "evaluacion.proximo_intento_en": {"$lte": ahora}},
            {"evaluacion.estado": "EN_PROCESO", "evaluacion.bloqueada_hasta": {"$lt": ahora}},
        ]}
        if sensor_ids is not None:
            filtro["sensor_id"] = {"$in": list(sensor_ids)}
        with self._collection() as c:
            return c.find_one_and_update(
                filtro,
                {"$set": {"evaluacion.estado": "EN_PROCESO", "evaluacion.propietario_id": propietario_id,
                          "evaluacion.bloqueada_hasta": ahora + timedelta(seconds=reserva_s)},
                 "$inc": {"evaluacion.intentos": 1, "evaluacion.version_reclamo": 1}},
                sort=[("fecha_medicion", ASCENDING), ("_id", ASCENDING)],
                return_document=ReturnDocument.AFTER,
            )

    def _propio(self, doc: dict[str, Any]) -> dict[str, Any]:
        ev = doc["evaluacion"]
        return {"_id": doc["_id"], "evaluacion.estado": "EN_PROCESO",
                "evaluacion.propietario_id": ev["propietario_id"], "evaluacion.version_reclamo": ev["version_reclamo"]}

    def marcar_procesada(self, doc: dict[str, Any], *, version_reglas: str, ahora: datetime,
                         resultado: str) -> bool:
        """PROCESADA, solo si el reclamo sigue siendo de quien la reservó. `resultado`: la clasificación."""
        with self._collection() as c:
            r = c.update_one(self._propio(doc), {
                "$set": {"evaluacion.estado": "PROCESADA", "evaluacion.version_reglas": version_reglas,
                         "evaluacion.procesada_en": ahora, "evaluacion.resultado": resultado},
                "$unset": {"evaluacion.bloqueada_hasta": "", "evaluacion.ultimo_error": ""},
            })
        return r.modified_count == 1

    def marcar_revision(self, doc: dict[str, Any], *, motivo: str, ahora: datetime) -> bool:
        """ERROR para revisión humana (sin contexto, reloj adelantado). Se conserva: nunca se borra."""
        with self._collection() as c:
            r = c.update_one(self._propio(doc), {
                "$set": {"evaluacion.estado": "ERROR", "evaluacion.resultado": motivo, "evaluacion.procesada_en": ahora},
                "$unset": {"evaluacion.bloqueada_hasta": ""},
            })
        return r.modified_count == 1

    def reintentar(self, doc: dict[str, Any], *, error: str, ahora: datetime, espera_s: float,
                   max_intentos: int, gasta_intento: bool = True) -> str:
        """Falla al evaluar: vuelve a PENDIENTE con espera, o queda ERROR si agotó los intentos.

        `gasta_intento=False` cuando la culpa no es de la lectura (PostgreSQL no respondía): se devuelve el
        intento que sumó la reserva, así una caída larga nunca manda lecturas válidas a revisión.
        """
        intentos = doc["evaluacion"].get("intentos", 0)
        estado = "ERROR" if gasta_intento and intentos >= max_intentos else "PENDIENTE"
        cambios: dict[str, Any] = {
            "$set": {"evaluacion.estado": estado, "evaluacion.ultimo_error": error[:2000],
                     "evaluacion.proximo_intento_en": ahora + timedelta(seconds=espera_s)},
            "$unset": {"evaluacion.bloqueada_hasta": ""},
        }
        if not gasta_intento:
            cambios["$inc"] = {"evaluacion.intentos": -1}
        with self._collection() as c:
            c.update_one(self._propio(doc), cambios)
        return estado

    # ------------------------------------------------------------------ consultas
    def historial(self, *, campo: str, valor: uuid.UUID, desde: datetime | None, hasta: datetime | None,
                  limite: int, despues: tuple[datetime, uuid.UUID] | None = None,
                  tramo_ids: list[uuid.UUID] | None = None) -> list[dict[str, Any]]:
        """Lecturas de un sensor/envío/contenedor en [desde, hasta), ordenadas por (fecha, _id).

        `despues` es el cursor de la página anterior (fecha y _id de su última lectura): paginación estable
        aunque lleguen lecturas nuevas mientras se navega.
        """
        with self._collection() as c:
            return list(c.find(self._filtro_historial(campo, valor, desde, hasta, despues, tramo_ids))
                        .sort([("fecha_medicion", ASCENDING), ("_id", ASCENDING)]).limit(limite))

    def _filtro_historial(self, campo, valor, desde, hasta, despues, tramo_ids=None) -> dict[str, Any]:
        if campo not in INDICE_POR_CAMPO:
            raise ValueError(f"campo de consulta no permitido: {campo}")
        filtro: dict[str, Any] = {campo: valor}
        if tramo_ids is not None:  # alcance del operador: solo lecturas tomadas durante SUS tramos
            filtro["tramo_id"] = {"$in": list(tramo_ids)}
        rango: dict[str, Any] = {}
        if desde is not None:
            rango["$gte"] = desde
        if hasta is not None:
            rango["$lt"] = hasta
        if despues is not None:
            fecha, ultimo_id = despues
            filtro["$or"] = [{"fecha_medicion": {"$gt": fecha}}, {"fecha_medicion": fecha, "_id": {"$gt": ultimo_id}}]
            rango["$gte"] = max(rango.get("$gte", fecha), fecha)
        if rango:
            filtro["fecha_medicion"] = rango
        return filtro

    def indice_usado(self, *, campo: str, valor: uuid.UUID, desde: datetime | None,
                     hasta: datetime | None) -> set[str]:
        """Índices del plan ganador de `historial` (explain): para verificar que no hay un COLLSCAN."""
        with self._collection() as c:
            plan = (c.find(self._filtro_historial(campo, valor, desde, hasta, None))
                    .sort([("fecha_medicion", ASCENDING), ("_id", ASCENDING)]).explain())
        return set(_indices(plan["queryPlanner"]["winningPlan"]))

    def posiciones(self, *, envio_id: uuid.UUID, limite: int = 2000,
                   tramo_ids: list[uuid.UUID] | None = None) -> list[dict[str, Any]]:
        """Hasta las últimas 2000 posiciones GPS, devueltas en orden temporal."""
        filtro: dict[str, Any] = {"envio_id": envio_id, "posicion": {"$exists": True}}
        if tramo_ids is not None:
            filtro["tramo_id"] = {"$in": list(tramo_ids)}
        with self._collection() as c:
            docs = list(c.find(filtro, {"posicion": 1, "fecha_medicion": 1, "sensor_id": 1})
                        .sort([("fecha_medicion", DESCENDING), ("_id", DESCENDING)]).limit(limite))
        return list(reversed(docs))

    def posiciones_asignacion(
        self,
        *,
        envio_id: uuid.UUID,
        contenedor_id: uuid.UUID,
        desde: datetime,
        hasta: datetime | None,
        tramo_ids: list[uuid.UUID] | None,
        limite: int,
    ) -> list[dict[str, Any]]:
        """Lee posiciones medidas solo durante el uso de una asignación concreta."""
        if tramo_ids is not None and not tramo_ids:
            return []
        filtro: dict[str, Any] = {
            "envio_id": envio_id,
            "contenedor_id": contenedor_id,
            "fecha_medicion": {"$gte": desde},
            "posicion": {"$exists": True},
        }
        if hasta is not None:
            filtro["fecha_medicion"]["$lt"] = hasta
        if tramo_ids is not None:
            filtro["tramo_id"] = {"$in": tramo_ids}
        with self._collection() as c:
            docs = list(
                c.find(filtro, {"posicion": 1, "fecha_medicion": 1, "sensor_id": 1})
                .sort([("fecha_medicion", DESCENDING), ("_id", DESCENDING)])
                .limit(limite)
            )
        return list(reversed(docs))

    def ultimas(self, *, envio_id: uuid.UUID,
                tramo_ids: list[uuid.UUID] | None = None) -> list[dict[str, Any]]:
        """Últimas 20 lecturas del envío, filtradas por los tramos visibles."""
        filtro = self._filtro_historial("envio_id", envio_id, None, None, None, tramo_ids)
        with self._collection() as c:
            return list(c.find(filtro).sort([("fecha_medicion", DESCENDING), ("_id", DESCENDING)]).limit(20))

    def para_revision(self, *, limite: int = 200) -> list[dict[str, Any]]:
        with self._collection() as c:
            return list(c.find({"evaluacion.estado": "ERROR"}).sort("fecha_medicion", ASCENDING).limit(limite))


def _indices(plan: dict[str, Any]) -> list[str]:
    nombres = [plan["indexName"]] if "indexName" in plan else []
    for hijo in plan.get("inputStages", []) + ([plan["inputStage"]] if "inputStage" in plan else []):
        nombres.extend(_indices(hijo))
    return nombres
