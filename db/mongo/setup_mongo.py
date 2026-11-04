"""Creación de colecciones, validadores $jsonSchema e índices de MongoDB (idempotente).

Colecciones: lecturas_sensor, eventos_logisticos, mensajes.
Todos los _id son UUID (BSON binData subtipo 4, `uuidRepresentation="standard"` en PyMongo).
Las fechas son BSON Date en UTC.
"""
from __future__ import annotations

import logging

from pymongo import ASCENDING, GEOSPHERE
from pymongo.database import Database

log = logging.getLogger(__name__)

UUID = {"bsonType": "binData"}
NUM = {"bsonType": ["int", "long", "double", "decimal"]}
DATE = {"bsonType": "date"}


def _num(minimum=None, maximum=None) -> dict:
    schema = dict(NUM)
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


# --------------------------------------------------------------------------- lecturas_sensor
LECTURAS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "sensor_id", "fecha_medicion", "fecha_recepcion", "mediciones", "origen"],
        "properties": {
            "_id": UUID,
            "sensor_id": UUID,
            # Contexto vigente AL MEDIR (puede faltar: lectura sin envío no genera alertas de carga)
            "asignacion_sensor_id": UUID,
            "contenedor_id": UUID,
            "envio_id": UUID,
            "tramo_id": UUID,
            "fecha_medicion": DATE,
            "fecha_recepcion": DATE,
            "mediciones": {
                "bsonType": "object",
                "additionalProperties": False,
                "properties": {
                    "temperatura_c": _num(-100, 150),
                    "humedad_pct": _num(0, 100),
                    "vibracion_g": _num(0),
                    "bateria_pct": _num(0, 100),
                },
            },
            "posicion": {
                "bsonType": "object",
                "required": ["type", "coordinates"],
                "properties": {
                    "type": {"enum": ["Point"]},
                    # GeoJSON: [longitud, latitud]
                    "coordinates": {
                        "bsonType": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": [_num(-180, 180), _num(-90, 90)],
                    },
                },
            },
            "origen": {"enum": ["SIMULADOR", "IMPORTADOR", "DISPOSITIVO"]},
            # Seguimiento durable de la evaluación de reglas (Documento 2, "Sincronización")
            "evaluacion": {
                "bsonType": "object",
                "required": ["estado"],
                "properties": {
                    "estado": {"enum": ["PENDIENTE", "EN_PROCESO", "PROCESADA", "ERROR"]},
                    "version_reglas": {"bsonType": "string"},
                    "intentos": {"bsonType": ["int", "long"], "minimum": 0},
                    "proximo_intento_en": DATE,
                    "bloqueada_hasta": DATE,
                    "procesada_en": DATE,
                    "propietario_id": UUID,
                    "version_reclamo": {"bsonType": ["int", "long"], "minimum": 0},
                },
            },
        },
    }
}

# --------------------------------------------------------------------------- eventos_logisticos
_TIPOS_LIBRES = [
    "CARGA_INICIADA", "EN_TRANSITO", "TRAMO_INICIADO", "TRAMO_FINALIZADO", "DESCARGA",
    "ENTREGADO", "CANCELADO", "INCIDENTE_ABIERTO", "INCIDENTE_RESUELTO",
]

EVENTOS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "envio_id", "tipo", "fecha_ocurrencia", "fecha_registro", "detalles"],
        "properties": {
            "_id": UUID,                       # = event_id de la outbox: la reentrega no duplica
            "envio_id": UUID,
            "tramo_id": UUID,
            "contenedor_ids": {"bsonType": "array", "items": UUID},
            "tipo": {"bsonType": "string"},
            "fecha_ocurrencia": DATE,
            "fecha_registro": DATE,
            "ubicacion_id": UUID,
            "descripcion": {"bsonType": "string"},
            "usuario_id": UUID,
            "detalles": {"bsonType": "object"},
        },
        # Estructura de `detalles` según el tipo de evento. Los conjuntos de `tipo` son
        # disjuntos, por lo que exactamente una rama valida cada documento.
        "oneOf": [
            {
                "properties": {
                    "tipo": {"enum": ["RETRASO"]},
                    "detalles": {
                        "bsonType": "object",
                        "required": ["motivo"],
                        "properties": {
                            "motivo": {"bsonType": "string", "minLength": 1},
                            "demora_estimada_min": {"bsonType": ["int", "long"], "minimum": 0},
                        },
                    },
                }
            },
            {
                "properties": {
                    "tipo": {"enum": ["ADUANA"]},
                    "detalles": {
                        "bsonType": "object",
                        "properties": {
                            "etapa": {"enum": ["INGRESO", "INSPECCION", "LIBERACION"]},
                            "referencia": {"bsonType": "string"},
                        },
                    },
                }
            },
            {"properties": {"tipo": {"enum": _TIPOS_LIBRES}, "detalles": {"bsonType": "object"}}},
        ],
    }
}

# --------------------------------------------------------------------------- mensajes
MENSAJES_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "conversacion_id", "remitente_id", "fecha_envio", "contenido", "tipo"],
        "properties": {
            "_id": UUID,
            "conversacion_id": UUID,
            "remitente_id": UUID,
            "fecha_envio": DATE,
            "contenido": {"bsonType": "string", "minLength": 1, "maxLength": 4000},
            "tipo": {"enum": ["TEXTO", "ARCHIVO", "SISTEMA"]},
        },
    }
}

COLECCIONES = {
    "lecturas_sensor": LECTURAS_VALIDATOR,
    "eventos_logisticos": EVENTOS_VALIDATOR,
    "mensajes": MENSAJES_VALIDATOR,
}

INDICES = {
    "lecturas_sensor": [
        ([("sensor_id", ASCENDING), ("fecha_medicion", ASCENDING)], "ix_lectura_sensor_fecha", {}),
        ([("envio_id", ASCENDING), ("fecha_medicion", ASCENDING)], "ix_lectura_envio_fecha", {}),
        ([("contenedor_id", ASCENDING), ("fecha_medicion", ASCENDING)], "ix_lectura_contenedor_fecha", {}),
        ([("posicion", GEOSPHERE)], "ix_lectura_posicion_2dsphere", {}),
        (
            [("evaluacion.estado", ASCENDING), ("evaluacion.proximo_intento_en", ASCENDING)],
            "ix_lectura_evaluacion",
            {},
        ),
    ],
    "eventos_logisticos": [
        ([("envio_id", ASCENDING), ("fecha_ocurrencia", ASCENDING)], "ix_evento_envio_fecha", {}),
        ([("contenedor_ids", ASCENDING), ("fecha_ocurrencia", ASCENDING)], "ix_evento_contenedor_fecha", {}),
        (
            [("envio_id", ASCENDING), ("fecha_ocurrencia", ASCENDING), ("_id", ASCENDING)],
            "ix_evento_envio_fecha_id",
            {},
        ),
    ],
    "mensajes": [
        (
            [("conversacion_id", ASCENDING), ("fecha_envio", ASCENDING), ("_id", ASCENDING)],
            "ix_mensaje_conversacion_fecha_id",
            {},
        ),
    ],
}


def apply(db: Database) -> None:
    """Crea o actualiza colecciones, validadores e índices. Seguro de re-ejecutar."""
    existentes = set(db.list_collection_names())
    for nombre, validator in COLECCIONES.items():
        if nombre in existentes:
            db.command("collMod", nombre, validator=validator, validationLevel="strict", validationAction="error")
            log.info("Validador actualizado en %s", nombre)
        else:
            db.create_collection(nombre, validator=validator, validationLevel="strict", validationAction="error")
            log.info("Colección %s creada", nombre)
        for keys, index_name, options in INDICES.get(nombre, []):
            db[nombre].create_index(keys, name=index_name, **options)
    aplicar_sharding(db)


# Particionamiento (sharding). La clave compuesta reparte las lecturas por sensor y conserva el orden por
# fecha dentro de cada sensor, que es el patrón de la consulta histórica. Se usa una colección común y no
# una de series temporales porque estas no admiten validador $jsonSchema ni índices únicos, no se pueden
# convertir desde una colección existente y limitan los índices geoespaciales.
SHARD_KEYS = {
    "lecturas_sensor": {"sensor_id": ASCENDING, "fecha_medicion": ASCENDING},
}


def es_cluster_con_sharding(db: Database) -> bool:
    """True si la conexión es a un mongos (clúster); un mongod suelto devuelve False."""
    return db.client.admin.command("hello").get("msg") == "isdbgrid"


def aplicar_sharding(db: Database) -> bool:
    """Reparte las colecciones de SHARD_KEYS si hay un clúster. En un servidor único no hace nada.

    Es idempotente: volver a ejecutar shardCollection con la misma clave no cambia nada. Cada clave
    necesita un índice que la respalde, y esos índices ya los crea apply().
    """
    if not es_cluster_con_sharding(db):
        log.info("MongoDB sin clúster (servidor único): se omite el sharding de %s", ", ".join(SHARD_KEYS))
        return False
    admin = db.client.admin
    admin.command("enableSharding", db.name)
    for nombre, clave in SHARD_KEYS.items():
        admin.command("shardCollection", f"{db.name}.{nombre}", key=clave)
        log.info("Colección %s particionada con clave %s", nombre, dict(clave))
    return True


def drop_all(db: Database) -> None:
    for nombre in COLECCIONES:
        db.drop_collection(nombre)
