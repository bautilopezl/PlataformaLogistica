"""Fase 6 — reglas comerciales puras (sin base): importes, facturas, aplicación de pagos y cierres."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from logistica.core.errors import BusinessRuleError, ValidationError
from logistica.domain import comercial as dc

AHORA = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def test_importes_exactos_sin_float_ni_redondeo_silencioso():
    assert dc.decimal_de("12,50", "monto", decimales=2) == Decimal("12.50")
    with pytest.raises(ValidationError):
        dc.decimal_de(0.1, "monto", decimales=2)  # float: rechazado
    with pytest.raises(ValidationError):
        dc.decimal_de("1.005", "monto", decimales=2)  # más decimales de los permitidos
    with pytest.raises(ValidationError):
        dc.decimal_de("0", "monto", decimales=2)
    assert dc.decimal_de("0", "precio", decimales=4, positivo=False) == 0


def test_subtotal_redondeo_comercial_y_total_de_subtotales():
    assert dc.subtotal(Decimal("3"), Decimal("0.3350")) == Decimal("1.01")  # 1.005 → 1.01 (HALF_UP)
    renglones = [
        dc.Renglon("a", Decimal("3"), Decimal("0.3350")),
        dc.Renglon("b", Decimal("1"), Decimal("0.0050")),
    ]
    assert dc.total_de(renglones) == Decimal("1.02")  # suma de subtotales redondeados (1.01 + 0.01)
    with pytest.raises(BusinessRuleError):
        dc.total_de([dc.Renglon("cero", Decimal("1"), Decimal("0"))])
    with pytest.raises(BusinessRuleError):
        dc.total_de([])


def test_cargo_normalizado():
    c = dc.CargoDatos(" Flete ", "1", "12000", "usd").normalizado()
    assert (c.concepto, c.cantidad, c.precio_unitario, c.moneda_codigo) == (
        "Flete",
        1,
        Decimal("12000"),
        "USD",
    )
    for malo in (
        dc.CargoDatos("", "1", "1", "USD"),
        dc.CargoDatos("x", "1", "0", "USD"),
        dc.CargoDatos("x", "1", "1", "US"),
    ):
        with pytest.raises(ValidationError):
            malo.normalizado()


def test_una_factura_por_moneda():
    e = uuid.uuid4()
    cargos = [
        dc.CargoPendiente(uuid.uuid4(), e, "Flete", Decimal(1), Decimal(100), "USD"),
        dc.CargoPendiente(uuid.uuid4(), e, "Seguro", Decimal(1), Decimal(50), "ARS"),
        dc.CargoPendiente(uuid.uuid4(), e, "Estiba", Decimal(2), Decimal(10), "USD"),
    ]
    grupos = dc.agrupar_por_moneda(cargos)
    assert list(grupos) == ["ARS", "USD"]
    assert [r.concepto for r in grupos["USD"]] == ["Flete", "Estiba"]
    assert dc.total_de(grupos["USD"]) == Decimal("120.00")


def test_numeracion_correlativa_por_anio():
    assert (
        dc.serie_venta(datetime(2026, 12, 31, 23, tzinfo=timezone(timedelta(hours=-3)))) == "FV-2027"
    )  # UTC
    assert dc.siguiente_numero(None) == "000001" and dc.siguiente_numero("000041") == "000042"
    assert dc.codigo_factura("FV-2026", "000001") == "FV-2026-000001"


def test_naturalezas_y_sentidos():
    assert dc.naturaleza_factura("VENTA") == "COBRAR" and dc.naturaleza_factura("COMPRA") == "PAGAR"
    assert dc.naturaleza_pago("COBRO") == "COBRAR" and dc.naturaleza_pago("PAGO") == "PAGAR"
    assert dc.opuesto(dc.AUMENTA) == dc.DISMINUYE
    assert dc.saldo([(dc.AUMENTA, Decimal("100")), (dc.DISMINUYE, Decimal("30"))]) == Decimal("70")


def _par(**cambios):
    t = uuid.uuid4()
    pago = dc.PagoSaldo(uuid.uuid4(), t, "COBRO", "USD", "CONFIRMADO", Decimal("100"), Decimal("40"))
    factura = dc.FacturaSaldo(uuid.uuid4(), t, "VENTA", "USD", "EMITIDA", Decimal("80"), Decimal("0"))
    for k, v in cambios.items():
        objeto, campo = k.split("__")
        if objeto == "pago":
            pago = dc.PagoSaldo(**{**pago.__dict__, campo: v})
        else:
            factura = dc.FacturaSaldo(**{**factura.__dict__, campo: v})
    return pago, factura


def test_aplicacion_valida_y_sus_limites():
    pago, factura = _par()
    dc.exigir_aplicable(pago, factura, Decimal("60"))  # disponible 60, saldo 80
    with pytest.raises(BusinessRuleError, match="disponible"):
        dc.exigir_aplicable(pago, factura, Decimal("60.01"))
    pago, factura = _par(factura__aplicado=Decimal("30"))
    with pytest.raises(BusinessRuleError, match="saldo"):
        dc.exigir_aplicable(pago, factura, Decimal("50.01"))


@pytest.mark.parametrize(
    "cambio, error",
    [
        ({"factura__tercero_id": uuid.uuid4()}, "otro tercero"),
        ({"factura__moneda_codigo": "ARS"}, "otra moneda"),
        ({"factura__sentido": "COMPRA"}, "sentido"),
        ({"pago__estado": "ANULADO"}, "pago anulado"),
        ({"factura__estado": "ANULADA"}, "no emitida"),
    ],
)
def test_aplicacion_exige_mismo_tercero_moneda_y_sentido(cambio, error):
    pago, factura = _par(**cambio)
    with pytest.raises(BusinessRuleError, match=error):
        dc.exigir_aplicable(pago, factura, Decimal("1"))


def test_pago_normalizado():
    p = dc.PagoDatos(uuid.uuid4(), "cobro", AHORA, "50", "usd", "transferencia", "  ").normalizado(AHORA)
    assert (p.sentido, p.medio, p.monto, p.moneda_codigo, p.referencia) == (
        "COBRO",
        "TRANSFERENCIA",
        Decimal("50"),
        "USD",
        None,
    )
    with pytest.raises(ValidationError):
        dc.PagoDatos(uuid.uuid4(), "COBRO", AHORA + timedelta(hours=1), "50", "USD", "OTRO").normalizado(
            AHORA
        )
    # Un reloj local unos segundos adelantado respecto de PostgreSQL no rechaza un pago de "ahora".
    dc.PagoDatos(uuid.uuid4(), "COBRO", AHORA + timedelta(seconds=90), "50", "USD", "OTRO").normalizado(AHORA)
    with pytest.raises(ValidationError):
        dc.PagoDatos(uuid.uuid4(), "COBRO", AHORA.replace(tzinfo=None), "50", "USD", "OTRO").normalizado(
            AHORA
        )


def test_factura_de_compra_normalizada():
    r = [dc.Renglon("Tramo marítimo", "1", "9000")]
    f = dc.FacturaCompraDatos(uuid.uuid4(), "A-0001", "123", AHORA, AHORA + timedelta(days=10), "usd", r)
    n = f.normalizado(AHORA)
    assert n.moneda_codigo == "USD" and n.renglones[0].cantidad == Decimal("1")
    with pytest.raises(ValidationError):
        dc.FacturaCompraDatos(uuid.uuid4(), "A", "1", AHORA, AHORA - timedelta(days=1), "USD", r).normalizado(
            AHORA
        )


def test_periodos_y_cierres():
    assert dc.periodo_de(datetime(2026, 10, 31, 23, tzinfo=timezone(timedelta(hours=-3)))) == date(
        2026, 11, 1
    )
    assert dc.fin_periodo(date(2026, 12, 1)) == datetime(2027, 1, 1, tzinfo=timezone.utc)
    dc.exigir_cerrable(date(2026, 10, 1), AHORA, None)
    with pytest.raises(BusinessRuleError, match="abierto"):
        dc.exigir_cerrable(date(2026, 11, 1), AHORA, None)  # el mes en curso no se cierra
    with pytest.raises(BusinessRuleError, match="orden"):
        dc.exigir_cerrable(date(2026, 9, 1), AHORA, date(2026, 10, 1))
    dc.exigir_cerrable(
        date(2026, 10, 1), AHORA, date(2026, 10, 1)
    )  # repetir el mismo: lo resuelve el servicio
    assert dc.Cierre(Decimal("10"), Decimal("5"), Decimal("12")).saldo_final == Decimal("3")


def test_motivo_obligatorio():
    with pytest.raises(ValidationError):
        dc.exigir_motivo("   ")
    assert dc.exigir_motivo(" error de precio ") == "error de precio"


# --------------------------------------------------------------------------- hallazgos de la revisión
def test_un_cargo_cuyo_importe_redondea_a_cero_se_rechaza_al_cargarlo():
    """Si entrara, su factura daría total 0 y la facturación automática de TODO el envío fallaría."""
    with pytest.raises(ValidationError, match="importe"):
        dc.CargoDatos("Mínimo", "0.001", "0.0001", "ARS").normalizado()
    assert dc.CargoDatos("Mínimo", "1", "0.005", "ARS").normalizado().cantidad == 1  # 0.005 → 0.01: válido


@pytest.mark.parametrize("valor", ["1e30", "1E+1000", "-1e30"])
def test_numeros_enormes_dan_un_error_de_validacion_y_no_una_excepcion_cruda(valor):
    with pytest.raises(ValidationError):
        dc.decimal_de(valor, "el monto", decimales=2)


def test_cantidad_y_precio_respetan_el_tamanio_de_sus_columnas():
    with pytest.raises(ValidationError):
        dc.CargoDatos("x", "100000000000", "1", "USD").normalizado()  # NUMERIC(14,3)
    with pytest.raises(ValidationError):
        dc.CargoDatos("x", "1", "100000000000000", "USD").normalizado()  # NUMERIC(18,4)
