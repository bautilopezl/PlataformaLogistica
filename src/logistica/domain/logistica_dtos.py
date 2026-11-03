"""DTO de presentación de logística (lo que los servicios devuelven a la UI)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class UbicacionDTO:
    id: uuid.UUID
    nombre: str
    tipo: str
    pais_codigo: str
    ciudad: str
    direccion: str | None
    latitud: Decimal | None
    longitud: Decimal | None
    activo: bool


@dataclass(frozen=True)
class VehiculoDTO:
    id: uuid.UUID
    operador_id: uuid.UUID
    operador_razon_social: str
    tipo: str
    identificacion: str
    estado: str


@dataclass(frozen=True)
class ContenedorDTO:
    id: uuid.UUID
    codigo_internacional: str
    tipo: str
    capacidad_kg: Decimal
    capacidad_m3: Decimal
    estado: str


@dataclass(frozen=True)
class SensorDTO:
    id: uuid.UUID
    codigo: str
    tipo: str
    fabricante: str
    modelo: str | None
    estado: str
    intervalo_esperado_seg: int
    capacidades: frozenset[str]
    contenedor_actual: str | None = None  # código del contenedor donde está instalado ahora


@dataclass(frozen=True)
class InstalacionDTO:
    id: uuid.UUID
    sensor_id: uuid.UUID
    contenedor_id: uuid.UUID
    contenedor_codigo: str
    instalado_en: datetime
    retirado_en: datetime | None
    observaciones: str | None


@dataclass(frozen=True)
class EnvioDTO:
    id: uuid.UUID
    codigo: str
    cliente_id: uuid.UUID
    cliente_razon_social: str
    origen_id: uuid.UUID
    origen_nombre: str
    destino_id: uuid.UUID
    destino_nombre: str
    descripcion_carga: str
    estado: str
    prioridad: str
    peso_kg: Decimal
    volumen_m3: Decimal
    creado_en: datetime
    entrega_estimada_en: datetime | None
    entrega_comprometida_en: datetime | None
    entrega_real_en: datetime | None
    version: int


@dataclass(frozen=True)
class TramoDTO:
    id: uuid.UUID
    envio_id: uuid.UUID
    orden: int
    origen_id: uuid.UUID
    origen_nombre: str
    destino_id: uuid.UUID
    destino_nombre: str
    medio: str
    operador_id: uuid.UUID
    operador_razon_social: str
    vehiculo_id: uuid.UUID | None
    vehiculo_identificacion: str | None
    estado: str
    salida_estimada_en: datetime
    llegada_estimada_en: datetime
    salida_real_en: datetime | None
    llegada_real_en: datetime | None
    costo: Decimal | None  # None si quien consulta no debe verlo (AGENTS.md §11)
    moneda_codigo: str | None


@dataclass(frozen=True)
class AsignacionDTO:
    id: uuid.UUID
    envio_id: uuid.UUID
    contenedor_id: uuid.UUID
    contenedor_codigo: str
    estado: str
    reserva_desde: datetime
    reserva_hasta: datetime
    uso_desde: datetime | None
    uso_hasta: datetime | None
    peso_asignado_kg: Decimal
    volumen_asignado_m3: Decimal


@dataclass(frozen=True)
class EnvioDetalleDTO:
    envio: EnvioDTO
    tramos: list[TramoDTO] = field(default_factory=list)
    asignaciones: list[AsignacionDTO] = field(default_factory=list)


@dataclass(frozen=True)
class EventoOutboxDTO:
    """Evento registrado para propagar a Mongo/Neo4j (la entrega real es de la fase 3)."""

    id: uuid.UUID
    evento_tipo: str
    agregado_version: int
    creado_en: datetime
    tipo_logistico: str | None  # tipo del evento en eventos_logisticos, si va a Mongo
    pendiente: bool
