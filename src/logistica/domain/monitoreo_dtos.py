"""DTO de presentación del monitoreo (lo que los servicios devuelven a la UI)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class CondicionDatos:
    """Umbrales de un envío (vacío = esa variable no se controla). Ver condicion_monitoreo."""

    temperatura_min_c: Any = None
    temperatura_max_c: Any = None
    humedad_min_pct: Any = None
    humedad_max_pct: Any = None
    vibracion_max_g: Any = None
    bateria_min_pct: Any = None
    tiempo_max_sin_lecturas_seg: int = 180


@dataclass(frozen=True)
class AlertaDTO:
    id: uuid.UUID
    envio_id: uuid.UUID
    envio_codigo: str
    contenedor_id: uuid.UUID
    contenedor_codigo: str
    tramo_id: uuid.UUID | None
    sensor_id: uuid.UUID | None
    sensor_codigo: str | None
    tipo: str
    severidad: str
    estado: str
    primera_deteccion_en: datetime
    ultima_deteccion_en: datetime
    normalizada_en: datetime | None
    cerrada_en: datetime | None
    cierre_tipo: str | None
    motivo_cierre: str | None
    reconocida_en: datetime | None
    incidente_id: uuid.UUID | None


@dataclass(frozen=True)
class IncidenteDTO:
    id: uuid.UUID
    envio_id: uuid.UUID
    envio_codigo: str
    tramo_id: uuid.UUID | None
    contenedor_id: uuid.UUID | None
    tipo: str
    severidad: str
    estado: str
    descripcion: str
    ocurrido_en: datetime
    abierto_en: datetime
    responsable_id: uuid.UUID | None
    responsable_email: str | None
    resuelto_en: datetime | None
    resolucion: str | None
    alerta_origen_id: uuid.UUID | None
    alertas: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class EstadoSensorDTO:
    """Estado actual de un sensor instalado. `sin_comunicacion_s` se CALCULA al consultar."""

    sensor_id: uuid.UUID
    sensor_codigo: str
    sensor_estado: str
    contenedor_codigo: str | None
    envio_id: uuid.UUID | None
    envio_codigo: str | None
    ultima_medicion_en: datetime | None
    sin_comunicacion_s: int | None  # None = no se espera comunicación (no está en un envío en tránsito)
    sin_comunicacion: bool
    alertas_activas: int


@dataclass(frozen=True)
class LecturaDTO:
    id: uuid.UUID
    sensor_id: uuid.UUID
    fecha_medicion: datetime
    fecha_recepcion: datetime
    temperatura_c: Decimal | None
    humedad_pct: Decimal | None
    vibracion_g: Decimal | None
    bateria_pct: Decimal | None
    latitud: float | None
    longitud: float | None
    origen: str
    evaluacion: str | None  # PENDIENTE, PROCESADA, ERROR...


@dataclass(frozen=True)
class PaginaLecturas:
    lecturas: list[LecturaDTO]
    siguiente: tuple[datetime, uuid.UUID] | None  # cursor para la próxima página; None = no hay más


@dataclass(frozen=True)
class PosicionDTO:
    fecha_medicion: datetime
    latitud: float
    longitud: float
    sensor_id: uuid.UUID
