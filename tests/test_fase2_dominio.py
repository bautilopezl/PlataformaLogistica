"""Fase 2 — reglas puras de logística (sin servidores). AGENTS.md §10.2."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "db" / "mongo"))

from logistica.core.errors import BusinessRuleError, ValidationError  # noqa: E402
from logistica.domain import logistica as d  # noqa: E402

T0 = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)
A, B, C, D = (uuid.uuid4() for _ in range(4))


def h(n: float) -> datetime:
    return T0 + timedelta(hours=n)


def tramo(orden, origen, destino, salida, llegada):
    return d.TramoRef(orden, origen, destino, h(salida), h(llegada))


# --------------------------------------------------------------------------- continuidad
def test_recorrido_continuo_valido():
    d.validar_continuidad(A, D, [tramo(1, A, B, 0, 5), tramo(2, B, C, 6, 30), tramo(3, C, D, 31, 33)])


def test_el_orden_de_la_lista_no_importa_importa_el_numero():
    d.validar_continuidad(A, C, [tramo(2, B, C, 6, 9), tramo(1, A, B, 0, 5)])


@pytest.mark.parametrize(
    "tramos, mensaje",
    [
        ([], "al menos un tramo"),
        ([tramo(1, B, C, 0, 1)], "origen del envío"),
        ([tramo(1, A, B, 0, 1)], "destino del envío"),
        (
            [tramo(1, A, B, 0, 1), tramo(2, C, D, 2, 3)],
            "debe salir de donde llega",
        ),  # destino ≠ origen siguiente
        ([tramo(1, A, B, 0, 5), tramo(2, B, D, 4, 6)], "no puede salir antes"),  # se superponen en el tiempo
        ([tramo(1, A, B, 0, 1), tramo(3, B, D, 2, 3)], "sin huecos"),
    ],
)
def test_recorridos_invalidos(tramos, mensaje):
    with pytest.raises(BusinessRuleError) as info:
        d.validar_continuidad(A, D, tramos)
    assert mensaje in info.value.user_message


# --------------------------------------------------------------------------- vehículo, capacidad
def test_vehiculo_compatible_con_medio_y_del_operador():
    op = uuid.uuid4()
    d.validar_vehiculo_para_tramo("MARITIMO", op, "BARCO", op, "HABILITADO")
    with pytest.raises(BusinessRuleError, match="incompatible"):
        d.validar_vehiculo_para_tramo("AEREO", op, "CAMION", op, "HABILITADO")
    with pytest.raises(BusinessRuleError, match="otro operador"):
        d.validar_vehiculo_para_tramo("TERRESTRE", op, "CAMION", uuid.uuid4(), "HABILITADO")
    with pytest.raises(BusinessRuleError, match="no habilitado"):
        d.validar_vehiculo_para_tramo("TERRESTRE", op, "CAMION", op, "MANTENIMIENTO")


def test_capacidad_del_contenedor():
    d.validar_capacidad(Decimal("1000"), Decimal("10"), Decimal("1000"), Decimal("10"))  # justo en el límite
    with pytest.raises(BusinessRuleError):
        d.validar_capacidad(Decimal("1000.001"), Decimal("1"), Decimal("1000"), Decimal("10"))
    with pytest.raises(BusinessRuleError):
        d.validar_capacidad(Decimal("1"), Decimal("10.5"), Decimal("1000"), Decimal("10"))


def test_cobertura_total_para_planificar():
    d.validar_cobertura_total(Decimal("500"), Decimal("5"), Decimal("500.000"), Decimal("5"))
    for peso, vol in (("499.999", "5"), ("500", "4.9"), ("0", "0")):
        with pytest.raises(BusinessRuleError, match="cobertura parcial"):
            d.validar_cobertura_total(Decimal("500"), Decimal("5"), Decimal(peso), Decimal(vol))


def test_no_se_asigna_mas_carga_que_la_del_envio():
    d.validar_total_asignado(Decimal("500"), Decimal("5"), Decimal("500"), Decimal("5"))
    with pytest.raises(BusinessRuleError):
        d.validar_total_asignado(Decimal("500"), Decimal("5"), Decimal("500.5"), Decimal("1"))


# --------------------------------------------------------------------------- estados
@pytest.mark.parametrize(
    "actual, nuevo, permitido",
    [
        ("BORRADOR", "PLANIFICADO", True),
        ("PLANIFICADO", "EN_TRANSITO", True),
        ("PLANIFICADO", "BORRADOR", True),  # replanificar
        ("EN_TRANSITO", "ENTREGADO", True),
        ("BORRADOR", "EN_TRANSITO", False),
        ("EN_TRANSITO", "CANCELADO", False),  # SUPUESTO S8
        ("ENTREGADO", "CANCELADO", False),
        ("CANCELADO", "PLANIFICADO", False),
    ],
)
def test_transiciones_de_estado_del_envio(actual, nuevo, permitido):
    if permitido:
        d.exigir_transicion(actual, d.EstadoEnvio(nuevo))
    else:
        with pytest.raises(BusinessRuleError):
            d.exigir_transicion(actual, d.EstadoEnvio(nuevo))


def test_solo_se_edita_la_planificacion_antes_de_salir():
    d.exigir_editable("BORRADOR")
    d.exigir_editable("PLANIFICADO")
    for estado in ("EN_TRANSITO", "ENTREGADO", "CANCELADO"):
        with pytest.raises(BusinessRuleError):
            d.exigir_editable(estado)


def test_inicio_de_tramo():
    v = uuid.uuid4()
    d.validar_inicio_tramo(1, "PENDIENTE", v, None)
    d.validar_inicio_tramo(2, "PENDIENTE", v, "FINALIZADO")
    with pytest.raises(BusinessRuleError, match="sin vehículo"):
        d.validar_inicio_tramo(1, "PENDIENTE", None, None)
    with pytest.raises(BusinessRuleError, match="anterior"):
        d.validar_inicio_tramo(2, "PENDIENTE", v, "EN_CURSO")
    with pytest.raises(BusinessRuleError):
        d.validar_inicio_tramo(1, "EN_CURSO", v, None)


def test_fin_de_tramo_llegada_no_anterior_a_la_salida():
    d.validar_fin_tramo("EN_CURSO", h(0), h(0))
    with pytest.raises(BusinessRuleError):
        d.validar_fin_tramo("EN_CURSO", h(1), h(0))
    with pytest.raises(BusinessRuleError):
        d.validar_fin_tramo("PENDIENTE", None, h(1))


def test_confirmacion_de_entrega():
    d.validar_confirmacion_entrega(["FINALIZADO", "FINALIZADO"], h(10), h(10))
    with pytest.raises(BusinessRuleError, match="sin finalizar"):
        d.validar_confirmacion_entrega(["FINALIZADO", "EN_CURSO"], h(10), h(11))
    with pytest.raises(BusinessRuleError, match="antes de llegar"):
        d.validar_confirmacion_entrega(["FINALIZADO"], h(10), h(9))


# --------------------------------------------------------------------------- magnitudes y datos
def test_magnitudes_son_decimal_y_nunca_float():
    assert d.magnitud("1250,5", "peso") == Decimal("1250.5")
    assert isinstance(d.magnitud(10, "peso"), Decimal)
    for malo in ("0", "-1", "abc", "1.0001", "NaN", "Infinity"):
        with pytest.raises(ValidationError):
            d.magnitud(malo, "peso")


def test_importe_con_dos_decimales():
    assert d.importe("1500.50") == Decimal("1500.50")
    for malo in ("-1", "1.001", "x"):
        with pytest.raises(ValidationError):
            d.importe(malo)


def test_fechas_sin_zona_horaria_se_rechazan():
    with pytest.raises(ValidationError):
        d.exigir_con_zona(datetime(2026, 1, 1))


def test_envio_valida_y_normaliza():
    e = d.EnvioDatos(uuid.uuid4(), A, B, "  Café  ", "100", "2.5", "alta").normalizado()
    assert (e.descripcion_carga, e.peso_kg, e.volumen_m3, e.prioridad) == (
        "Café",
        Decimal("100"),
        Decimal("2.5"),
        "ALTA",
    )
    with pytest.raises(ValidationError):
        d.EnvioDatos(uuid.uuid4(), A, A, "x", "1", "1").normalizado()


def test_tramo_plan_valida_costo_moneda_y_fechas():
    base = dict(
        origen_id=A,
        destino_id=B,
        medio="terrestre",
        operador_id=C,
        salida_estimada_en=h(0),
        llegada_estimada_en=h(2),
    )
    t = d.TramoPlan(**base, costo="100.5", moneda_codigo="usd").normalizado()
    assert (t.medio, t.costo, t.moneda_codigo) == ("TERRESTRE", Decimal("100.5"), "USD")
    with pytest.raises(ValidationError):
        d.TramoPlan(**base, costo="10").normalizado()  # costo sin moneda
    with pytest.raises(ValidationError):
        d.TramoPlan(**{**base, "llegada_estimada_en": h(-1)}).normalizado()
    with pytest.raises(ValidationError):
        d.TramoPlan(**{**base, "salida_estimada_en": datetime(2026, 12, 1)}).normalizado()


def test_ubicacion_coordenadas_juntas_y_en_rango():
    u = d.UbicacionDatos(
        "Puerto BA", "puerto", "ar", "Buenos Aires", latitud="-34.6", longitud="-58.37"
    ).normalizado()
    assert u.latitud == Decimal("-34.600000") and u.tipo == "PUERTO"
    with pytest.raises(ValidationError):
        d.UbicacionDatos("X", "PUERTO", "AR", "Y", latitud="10").normalizado()
    with pytest.raises(ValidationError):
        d.UbicacionDatos("X", "PUERTO", "AR", "Y", latitud="95", longitud="0").normalizado()


def test_sensor_requiere_capacidades_e_intervalo_positivo():
    s = d.SensorDatos(
        "s-1", "multisensor", "Acme", capacidades=frozenset({"temperatura", "gps"})
    ).normalizado()
    assert s.codigo == "S-1" and s.capacidades == {"TEMPERATURA", "GPS"}
    with pytest.raises(ValidationError):
        d.SensorDatos("s", "GPS", "Acme").normalizado()
    with pytest.raises(ValidationError):
        d.SensorDatos(
            "s", "GPS", "Acme", intervalo_esperado_seg=0, capacidades=frozenset({"GPS"})
        ).normalizado()


def test_codigo_de_envio():
    codigo = d.generar_codigo_envio(T0)
    assert codigo.startswith("ENV-20261201-") and len(codigo) == 19
    assert d.generar_codigo_envio(T0) != codigo


# --------------------------------------------------------------------------- eventos
def test_detalles_de_eventos_como_el_validador_de_mongo():
    assert d.validar_detalles_evento("RETRASO", {"motivo": " paro ", "demora_estimada_min": "90"}) == {
        "motivo": "paro",
        "demora_estimada_min": 90,
    }
    with pytest.raises(ValidationError):
        d.validar_detalles_evento("RETRASO", {})
    with pytest.raises(ValidationError):
        d.validar_detalles_evento("RETRASO", {"motivo": "x", "demora_estimada_min": -5})
    assert d.validar_detalles_evento("aduana", {"etapa": "inspeccion"}) == {"etapa": "INSPECCION"}
    with pytest.raises(ValidationError):
        d.validar_detalles_evento("ADUANA", {"etapa": "OTRA"})
    with pytest.raises(ValidationError):
        d.validar_detalles_evento("INVENTADO", {})


def test_aduana_con_etapa_vacia_o_referencia_no_texto_queda_como_espera_mongo():
    assert d.validar_detalles_evento("ADUANA", {"etapa": "", "referencia": 77}) == {"referencia": "77"}
    assert d.validar_detalles_evento("ADUANA", {"etapa": None}) == {}


def test_demora_debe_ser_un_entero():
    for mala in ("2.7", 2.7, "abc", True):
        with pytest.raises(ValidationError):
            d.validar_detalles_evento("RETRASO", {"motivo": "x", "demora_estimada_min": mala})
    ok = d.validar_detalles_evento("RETRASO", {"motivo": "x", "demora_estimada_min": "15"})
    assert ok["demora_estimada_min"] == 15


def test_tipos_de_evento_coinciden_con_el_validador_de_mongo():
    import setup_mongo

    ramas = setup_mongo.EVENTOS_VALIDATOR["$jsonSchema"]["oneOf"]
    en_mongo = {t for r in ramas for t in r["properties"]["tipo"]["enum"]}
    assert en_mongo == {t.value for t in d.TipoEvento}
    assert d.ETAPAS_ADUANA == set(ramas[1]["properties"]["detalles"]["properties"]["etapa"]["enum"])
