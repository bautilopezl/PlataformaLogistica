"""DTO para consultas de trazabilidad y proyección de grafos."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


EventoCursor = tuple[datetime, uuid.UUID]


@dataclass(frozen=True)
class EventoTrazabilidadDTO:
    id: uuid.UUID
    fecha_ocurrencia: datetime
    fecha_registro: datetime
    tipo: str
    descripcion: str | None
    tramo_id: uuid.UUID | None
    ubicacion_id: uuid.UUID | None
    contenedor_ids: tuple[uuid.UUID, ...]
    detalles: dict[str, Any]
    sincronizacion: str


@dataclass(frozen=True)
class PaginaEventosDTO:
    eventos: list[EventoTrazabilidadDTO]
    siguiente: EventoCursor | None
    pendientes_mongo: int = 0
    fallidas_mongo: int = 0


@dataclass(frozen=True)
class TramoProyectadoDTO:
    id: uuid.UUID
    orden: int
    origen_id: uuid.UUID
    origen_nombre: str | None
    destino_id: uuid.UUID
    destino_nombre: str | None
    operador_id: uuid.UUID
    operador_razon_social: str | None
    medio: str
    estado: str
    vehiculo: str | None
    salida_estimada_en: datetime | None
    llegada_estimada_en: datetime | None
    salida_real_en: datetime | None
    llegada_real_en: datetime | None


@dataclass(frozen=True)
class ProyeccionRutaDTO:
    codigo: str
    estado: str
    version: int
    tramos: list[TramoProyectadoDTO] = field(default_factory=list)


@dataclass(frozen=True)
class TramoRutaDTO:
    id: uuid.UUID
    orden: int
    origen_id: uuid.UUID
    origen_nombre: str
    destino_id: uuid.UUID
    destino_nombre: str
    operador_id: uuid.UUID
    operador_razon_social: str
    medio: str
    estado: str
    vehiculo: str | None
    salida_estimada_en: datetime | None
    llegada_estimada_en: datetime | None
    salida_real_en: datetime | None
    llegada_real_en: datetime | None


@dataclass(frozen=True)
class RutaTrazabilidadDTO:
    envio_id: uuid.UUID
    codigo: str
    estado_proyeccion: str
    version_operativa: int
    version_proyectada: int | None
    tramos: list[TramoRutaDTO]
    nota_catalogo: str
    truncada: bool = False


@dataclass(frozen=True)
class AsignacionContenedorBaseDTO:
    id: uuid.UUID
    envio_id: uuid.UUID
    envio_codigo: str
    cliente_id: uuid.UUID
    cliente_razon_social: str
    estado: str
    reserva_desde: datetime
    reserva_hasta: datetime
    uso_desde: datetime | None
    uso_hasta: datetime | None


@dataclass(frozen=True)
class TramoContenedorDTO:
    id: uuid.UUID
    orden: int
    origen_nombre: str
    destino_nombre: str
    operador_razon_social: str
    medio: str
    estado: str
    salida_estimada_en: datetime
    llegada_estimada_en: datetime
    salida_real_en: datetime | None
    llegada_real_en: datetime | None
    planificado_durante_reserva: bool
    realizado_durante_uso: bool


@dataclass(frozen=True)
class AsignacionContenedorDTO:
    id: uuid.UUID
    envio_id: uuid.UUID
    envio_codigo: str
    cliente_id: uuid.UUID
    cliente_razon_social: str
    estado: str
    reserva_desde: datetime
    reserva_hasta: datetime
    uso_desde: datetime | None
    uso_hasta: datetime | None
    tramos: list[TramoContenedorDTO]


@dataclass(frozen=True)
class PosicionContenedorDTO:
    fecha_medicion: datetime
    latitud: float
    longitud: float
    sensor_id: uuid.UUID


@dataclass(frozen=True)
class RecorridoContenedorDTO:
    contenedor_id: uuid.UUID
    codigo: str
    asignaciones: list[AsignacionContenedorDTO]
    truncado: bool = False


@dataclass(frozen=True)
class RelacionRedDTO:
    cliente_id: uuid.UUID
    cliente_razon_social: str
    envio_id: uuid.UUID
    envio_codigo: str
    envio_version_proyectada: int
    tramo_id: uuid.UUID
    tramo_orden: int
    operador_id: uuid.UUID
    operador_razon_social: str


@dataclass(frozen=True)
class RedTrazabilidadDTO:
    estado_grafo: str
    relaciones: list[RelacionRedDTO]
    truncada: bool
    envios_omitidos: bool
    versiones_desfasadas: int
