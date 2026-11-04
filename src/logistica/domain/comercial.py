"""Reglas comerciales puras (AGENTS.md §10.3): importes, facturas, pagos, cuentas corrientes y cierres.

Sin I/O: todo se prueba sin base. Convenciones:
- Dinero en `Decimal`, nunca `float`. Subtotales e importes a 2 decimales con redondeo comercial (HALF_UP).
- No se mezclan monedas: una factura, un pago y una cuenta tienen UNA moneda.
- Cuenta POR COBRAR (naturaleza COBRAR): lo que un cliente nos debe. Factura de VENTA la AUMENTA; un COBRO la
  DISMINUYE. Cuenta POR PAGAR (PAGAR): lo que le debemos a un operador/proveedor. Factura de COMPRA la
  AUMENTA; un PAGO la DISMINUYE. El saldo se DERIVA de los movimientos (que nunca se modifican: se corrigen
  con reversas).
SUPUESTOS C1..C7: ver AGENTS.md §16 ("Supuestos de la fase 6").
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from logistica.core.errors import BusinessRuleError, ValidationError

CENTAVO = Decimal("0.01")
VENCIMIENTO_DIAS = 30  # SUPUESTO C3: vencimiento de las facturas de venta
# El reloj de cada PC puede ir algo adelantado respecto del de PostgreSQL (la referencia): igual que §10.4.
TOLERANCIA_RELOJ = timedelta(seconds=120)
TIPO_FACTURA = "FACTURA"
_MONEDA = re.compile(r"^[A-Z]{3}$")
_MAX_IMPORTE = Decimal("9999999999999999.99")  # NUMERIC(18,2)
_MAX_CANTIDAD = Decimal("99999999999.999")  # NUMERIC(14,3)
_MAX_PRECIO = Decimal("99999999999999.9999")  # NUMERIC(18,4)


class Sentido(StrEnum):
    VENTA = "VENTA"
    COMPRA = "COMPRA"


class SentidoPago(StrEnum):
    COBRO = "COBRO"  # nos pagan (cliente)
    PAGO = "PAGO"  # pagamos (operador/proveedor)


class Naturaleza(StrEnum):
    COBRAR = "COBRAR"
    PAGAR = "PAGAR"


class EstadoCargo(StrEnum):
    PENDIENTE = "PENDIENTE"
    FACTURADO = "FACTURADO"
    CANCELADO = "CANCELADO"


class EstadoFactura(StrEnum):
    BORRADOR = "BORRADOR"
    EMITIDA = "EMITIDA"
    ANULADA = "ANULADA"


class EstadoPago(StrEnum):
    CONFIRMADO = "CONFIRMADO"
    ANULADO = "ANULADO"


class Medio(StrEnum):
    EFECTIVO = "EFECTIVO"
    TRANSFERENCIA = "TRANSFERENCIA"
    TARJETA = "TARJETA"
    OTRO = "OTRO"


AUMENTA = "AUMENTA"
DISMINUYE = "DISMINUYE"


# --------------------------------------------------------------------------- valores
def decimal_de(
    valor: Any, nombre: str, *, decimales: int, positivo: bool = True, maximo: Decimal = _MAX_IMPORTE
) -> Decimal:
    """Convierte un texto/número a Decimal exacto con a lo sumo `decimales` decimales (sin redondear en
    silencio)."""
    if isinstance(valor, float):
        raise ValidationError(
            f"{nombre} como float", user_message=f"{nombre.capitalize()}: use un número decimal."
        )
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, AttributeError):
        raise ValidationError(
            f"{nombre} inválido: {valor!r}", user_message=f"{nombre.capitalize()} no es un número válido."
        ) from None
    escala = Decimal(1).scaleb(-decimales)
    # quantize falla con valores de más de 28 dígitos (por ejemplo 1e30): es un dato inválido, no un error.
    try:
        exacto = d.is_finite() and d.quantize(escala) == d
    except InvalidOperation:
        exacto = False
    if not exacto or d > maximo or (d <= 0 if positivo else d < 0):
        regla = "mayor a cero" if positivo else "no negativo"
        raise ValidationError(
            f"{nombre} fuera de rango: {valor!r}",
            user_message=f"{nombre.capitalize()} debe ser {regla}, con hasta {decimales} decimales.",
        )
    return d


def moneda_de(valor: Any) -> str:
    m = str(valor or "").strip().upper()
    if not _MONEDA.match(m):
        raise ValidationError(
            f"moneda inválida: {valor!r}", user_message="La moneda debe ser un código ISO de 3 letras."
        )
    return m


def texto_de(valor: Any, nombre: str, maximo: int) -> str:
    t = str(valor or "").strip()
    if not t or len(t) > maximo:
        raise ValidationError(
            f"{nombre} vacío o largo", user_message=f"Indique {nombre} (hasta {maximo} caracteres)."
        )
    return t


def subtotal(cantidad: Decimal, precio_unitario: Decimal) -> Decimal:
    return (cantidad * precio_unitario).quantize(CENTAVO, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- cargos y facturas
@dataclass(frozen=True)
class CargoDatos:
    """Lo que se le cobra al cliente por un envío (≠ costo del tramo, que es lo que nos cobra el operador)."""

    concepto: str
    cantidad: Any
    precio_unitario: Any
    moneda_codigo: str

    def normalizado(self) -> CargoDatos:
        # SUPUESTO C5: el IMPORTE (cantidad × precio, redondeado) debe ser de al menos un centavo. Un cargo
        # que redondea a cero haría una factura en cero, y la facturación automática de todo el envío
        # fallaría.
        cantidad = decimal_de(self.cantidad, "la cantidad", decimales=3, maximo=_MAX_CANTIDAD)
        precio = decimal_de(self.precio_unitario, "el precio unitario", decimales=4, maximo=_MAX_PRECIO)
        if subtotal(cantidad, precio) < CENTAVO:
            raise ValidationError(
                "importe en cero", user_message="El importe del cargo debe ser de al menos 0,01."
            )
        return CargoDatos(
            texto_de(self.concepto, "el concepto", 200), cantidad, precio, moneda_de(self.moneda_codigo)
        )


@dataclass(frozen=True)
class Renglon:
    concepto: str
    cantidad: Decimal
    precio_unitario: Decimal
    cargo_id: uuid.UUID | None = None
    envio_id: uuid.UUID | None = None

    @property
    def subtotal(self) -> Decimal:
        return subtotal(self.cantidad, self.precio_unitario)

    def normalizado(self) -> Renglon:
        return Renglon(
            texto_de(self.concepto, "el concepto", 200),
            decimal_de(self.cantidad, "la cantidad", decimales=3, maximo=_MAX_CANTIDAD),
            decimal_de(
                self.precio_unitario, "el precio unitario", decimales=4, positivo=False, maximo=_MAX_PRECIO
            ),
            self.cargo_id,
            self.envio_id,
        )


def total_de(renglones: list[Renglon]) -> Decimal:
    """Total de la factura = suma de subtotales ya redondeados (así cuadra con los renglones guardados)."""
    if not renglones:
        raise BusinessRuleError("factura sin renglones", user_message="La factura no tiene renglones.")
    total = sum((r.subtotal for r in renglones), Decimal("0"))
    if total <= 0:
        raise BusinessRuleError(
            "factura en cero", user_message="El total de la factura debe ser mayor a cero."
        )
    if total > _MAX_IMPORTE:
        raise ValidationError("total excesivo", user_message="El total de la factura es demasiado grande.")
    return total


@dataclass(frozen=True)
class CargoPendiente:
    id: uuid.UUID
    envio_id: uuid.UUID
    concepto: str
    cantidad: Decimal
    precio_unitario: Decimal
    moneda_codigo: str


def agrupar_por_moneda(cargos: list[CargoPendiente]) -> dict[str, list[Renglon]]:
    """Una factura por moneda (nunca se mezclan). El orden de los renglones es el orden recibido."""
    grupos: dict[str, list[Renglon]] = {}
    for c in cargos:
        grupos.setdefault(c.moneda_codigo, []).append(
            Renglon(c.concepto, c.cantidad, c.precio_unitario, cargo_id=c.id, envio_id=c.envio_id)
        )
    return dict(sorted(grupos.items()))


# --------------------------------------------------------------------------- numeración (SUPUESTO C1)
def serie_venta(emitida_en: datetime) -> str:
    """Serie de las ventas: `FV-<año UTC>`. Correlativo por año; un número anulado no se reutiliza."""
    return f"FV-{emitida_en.astimezone(timezone.utc).year}"


def siguiente_numero(ultimo: str | None) -> str:
    return f"{(int(ultimo) if ultimo else 0) + 1:06d}"


def codigo_factura(serie: str | None, numero: str | None) -> str:
    return f"{serie}-{numero}" if serie and numero else "(borrador)"


def vencimiento(emitida_en: datetime) -> datetime:
    return emitida_en + timedelta(days=VENCIMIENTO_DIAS)


# --------------------------------------------------------------------------- cuentas y movimientos
def naturaleza_factura(sentido: str) -> Naturaleza:
    return Naturaleza.COBRAR if sentido == Sentido.VENTA else Naturaleza.PAGAR


def naturaleza_pago(sentido: str) -> Naturaleza:
    return Naturaleza.COBRAR if sentido == SentidoPago.COBRO else Naturaleza.PAGAR


def sentido_pago_para(sentido_factura: str) -> SentidoPago:
    """Un COBRO solo se aplica a facturas de VENTA; un PAGO, a facturas de COMPRA."""
    return SentidoPago.COBRO if sentido_factura == Sentido.VENTA else SentidoPago.PAGO


def opuesto(sentido_movimiento: str) -> str:
    return DISMINUYE if sentido_movimiento == AUMENTA else AUMENTA


def saldo(movimientos: list[tuple[str, Decimal]]) -> Decimal:
    """Saldo derivado: aumentos − disminuciones."""
    return sum((i if s == AUMENTA else -i for s, i in movimientos), Decimal("0"))


# --------------------------------------------------------------------------- aplicación de pagos
@dataclass(frozen=True)
class PagoSaldo:
    id: uuid.UUID
    tercero_id: uuid.UUID
    sentido: str
    moneda_codigo: str
    estado: str
    monto: Decimal
    aplicado: Decimal  # suma de aplicaciones vigentes

    @property
    def disponible(self) -> Decimal:
        return self.monto - self.aplicado


@dataclass(frozen=True)
class FacturaSaldo:
    id: uuid.UUID
    tercero_id: uuid.UUID
    sentido: str
    moneda_codigo: str
    estado: str
    total: Decimal
    aplicado: Decimal

    @property
    def pendiente(self) -> Decimal:
        return self.total - self.aplicado


def exigir_aplicable(pago: PagoSaldo, factura: FacturaSaldo, monto: Decimal) -> None:
    """Una aplicación coincide en tercero, moneda y sentido, y no supera ni el disponible ni el saldo
    (§10.3)."""
    if pago.estado != EstadoPago.CONFIRMADO:
        raise BusinessRuleError("pago anulado", user_message="El pago está anulado.")
    if factura.estado != EstadoFactura.EMITIDA:
        raise BusinessRuleError(
            "factura no emitida", user_message="Solo se aplican pagos a facturas emitidas."
        )
    if pago.tercero_id != factura.tercero_id:
        raise BusinessRuleError(
            "otro tercero", user_message="El pago y la factura son de terceros distintos."
        )
    if pago.moneda_codigo != factura.moneda_codigo:
        raise BusinessRuleError(
            "otra moneda", user_message="El pago y la factura están en monedas distintas."
        )
    if pago.sentido != sentido_pago_para(factura.sentido):
        raise BusinessRuleError("sentido", user_message="Un cobro se aplica a ventas y un pago a compras.")
    if monto > pago.disponible:
        raise BusinessRuleError(
            "supera el disponible", user_message=f"El pago solo tiene {pago.disponible} disponible."
        )
    if monto > factura.pendiente:
        raise BusinessRuleError("supera el saldo", user_message=f"La factura solo debe {factura.pendiente}.")


# --------------------------------------------------------------------------- pagos y compras
@dataclass(frozen=True)
class PagoDatos:
    tercero_id: uuid.UUID
    sentido: str
    fecha: datetime
    monto: Any
    moneda_codigo: str
    medio: str
    referencia: str | None = None

    def normalizado(self, ahora: datetime) -> PagoDatos:
        sentido = str(self.sentido or "").strip().upper()
        medio = str(self.medio or "").strip().upper()
        if sentido not in set(SentidoPago):
            raise ValidationError("sentido de pago", user_message="Indique si es un cobro o un pago.")
        if medio not in set(Medio):
            raise ValidationError("medio de pago", user_message="Medio de pago inválido.")
        exigir_fecha(self.fecha, ahora, "la fecha del pago")
        ref = (self.referencia or "").strip() or None
        if ref is not None and len(ref) > 150:
            raise ValidationError(
                "referencia larga", user_message="La referencia admite hasta 150 caracteres."
            )
        return PagoDatos(
            self.tercero_id,
            sentido,
            self.fecha,
            decimal_de(self.monto, "el monto", decimales=2),
            moneda_de(self.moneda_codigo),
            medio,
            ref,
        )


@dataclass(frozen=True)
class FacturaCompraDatos:
    """Factura que nos emite un operador o proveedor: su numeración es la de ellos."""

    tercero_id: uuid.UUID
    serie: str
    numero: str
    emitida_en: datetime
    vence_en: datetime
    moneda_codigo: str
    renglones: list[Renglon] = field(default_factory=list)
    tipo_comprobante: str = TIPO_FACTURA

    def normalizado(self, ahora: datetime) -> FacturaCompraDatos:
        exigir_fecha(self.emitida_en, ahora, "la fecha de emisión")
        if self.vence_en.tzinfo is None or self.vence_en < self.emitida_en:
            raise ValidationError(
                "vencimiento", user_message="El vencimiento no puede ser anterior a la emisión."
            )
        renglones = [r.normalizado() for r in self.renglones]
        total_de(renglones)
        return FacturaCompraDatos(
            self.tercero_id,
            texto_de(self.serie, "la serie", 30),
            texto_de(self.numero, "el número", 40),
            self.emitida_en,
            self.vence_en,
            moneda_de(self.moneda_codigo),
            renglones,
            texto_de(self.tipo_comprobante, "el tipo de comprobante", 30).upper(),
        )


def exigir_fecha(fecha: datetime, ahora: datetime, nombre: str) -> None:
    if not isinstance(fecha, datetime) or fecha.tzinfo is None:
        raise ValidationError(f"{nombre} sin zona", user_message=f"Indique {nombre} con zona horaria.")
    if fecha > ahora + TOLERANCIA_RELOJ:
        raise ValidationError(f"{nombre} futura", user_message=f"{nombre.capitalize()} no puede ser futura.")


def exigir_motivo(motivo: str | None) -> str:
    m = (motivo or "").strip()
    if not m:
        raise ValidationError("sin motivo", user_message="Indique el motivo.")
    return m


# --------------------------------------------------------------------------- cierres (SUPUESTO C6)
def periodo_de(valor: date | datetime) -> date:
    """Primer día del mes (en UTC si es una fecha con hora)."""
    if isinstance(valor, datetime):
        valor = valor.astimezone(timezone.utc).date()
    return valor.replace(day=1)


def inicio_periodo(periodo: date) -> datetime:
    return datetime(periodo.year, periodo.month, 1, tzinfo=timezone.utc)


def fin_periodo(periodo: date) -> datetime:
    """Instante en que empieza el mes siguiente (exclusivo)."""
    anio, mes = (periodo.year + 1, 1) if periodo.month == 12 else (periodo.year, periodo.month + 1)
    return datetime(anio, mes, 1, tzinfo=timezone.utc)


def exigir_cerrable(periodo: date, ahora: datetime, ultimo_cerrado: date | None) -> None:
    """Solo se cierra un mes TERMINADO y nunca uno anterior a otro ya cerrado (los cierres avanzan)."""
    if periodo.day != 1:
        raise ValidationError("periodo sin normalizar", user_message="El período es un mes (día 1).")
    if fin_periodo(periodo) > ahora:
        raise BusinessRuleError("periodo abierto", user_message="Solo se cierran meses terminados.")
    if ultimo_cerrado is not None and periodo < ultimo_cerrado:
        raise BusinessRuleError(
            "cierre fuera de orden", user_message="Ya hay un cierre posterior: no se cierra un mes anterior."
        )


@dataclass(frozen=True)
class Cierre:
    saldo_inicial: Decimal
    total_aumentos: Decimal
    total_disminuciones: Decimal

    @property
    def saldo_final(self) -> Decimal:
        return self.saldo_inicial + self.total_aumentos - self.total_disminuciones
