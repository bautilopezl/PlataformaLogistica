"""DTO de presentación del módulo comercial (lo que los servicios devuelven a la UI)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class CargoDTO:
    id: uuid.UUID
    envio_id: uuid.UUID
    envio_codigo: str
    concepto: str
    cantidad: Decimal
    precio_unitario: Decimal
    moneda_codigo: str
    estado: str
    creado_en: datetime
    factura_id: uuid.UUID | None  # factura en la que se facturó (vigente o anulada)

    @property
    def importe(self) -> Decimal:
        from logistica.domain.comercial import subtotal

        return subtotal(self.cantidad, self.precio_unitario)


@dataclass(frozen=True)
class RenglonDTO:
    renglon: int
    concepto: str
    cantidad: Decimal
    precio_unitario: Decimal
    subtotal: Decimal
    envio_id: uuid.UUID | None
    cargo_envio_id: uuid.UUID | None


@dataclass(frozen=True)
class FacturaDTO:
    id: uuid.UUID
    tercero_id: uuid.UUID
    tercero_razon_social: str
    sentido: str
    tipo_comprobante: str
    serie: str | None
    numero: str | None
    emitida_en: datetime | None
    vence_en: datetime | None
    moneda_codigo: str
    importe_total: Decimal
    estado: str
    creado_en: datetime
    anulada_en: datetime | None
    motivo_anulacion: str | None
    aplicado: Decimal  # suma de aplicaciones vigentes
    renglones: list[RenglonDTO] = field(default_factory=list)

    @property
    def codigo(self) -> str:
        from logistica.domain.comercial import codigo_factura

        return codigo_factura(self.serie, self.numero)

    @property
    def pendiente(self) -> Decimal:
        return self.importe_total - self.aplicado if self.estado == "EMITIDA" else Decimal("0")


@dataclass(frozen=True)
class PagoDTO:
    id: uuid.UUID
    tercero_id: uuid.UUID
    tercero_razon_social: str
    sentido: str
    fecha: datetime
    monto: Decimal
    moneda_codigo: str
    medio: str
    referencia: str | None
    estado: str
    anulado_en: datetime | None
    motivo_anulacion: str | None
    aplicado: Decimal

    @property
    def disponible(self) -> Decimal:
        return self.monto - self.aplicado if self.estado == "CONFIRMADO" else Decimal("0")


@dataclass(frozen=True)
class AplicacionDTO:
    id: uuid.UUID
    pago_id: uuid.UUID
    factura_id: uuid.UUID
    factura_codigo: str
    monto_aplicado: Decimal
    creado_en: datetime
    anulada_en: datetime | None
    motivo_anulacion: str | None


@dataclass(frozen=True)
class CuentaDTO:
    id: uuid.UUID
    tercero_id: uuid.UUID
    tercero_razon_social: str
    naturaleza: str
    moneda_codigo: str
    saldo: Decimal  # derivado de los movimientos
    ultimo_cierre: date | None


@dataclass(frozen=True)
class MovimientoDTO:
    id: uuid.UUID
    fecha: datetime
    sentido: str
    importe: Decimal
    descripcion: str
    factura_id: uuid.UUID | None
    pago_id: uuid.UUID | None
    reversa_de_id: uuid.UUID | None


@dataclass(frozen=True)
class PaginaMovimientos:
    movimientos: list[MovimientoDTO]
    siguiente: tuple[datetime, uuid.UUID] | None


@dataclass(frozen=True)
class CierreDTO:
    id: uuid.UUID
    cuenta_id: uuid.UUID
    periodo: date
    saldo_inicial: Decimal
    total_aumentos: Decimal
    total_disminuciones: Decimal
    saldo_final: Decimal
    cerrado_en: datetime
