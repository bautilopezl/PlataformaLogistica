"""Fase 7 — métricas, reportes y predicción sobre datos chicos, con el resultado calculado a mano en cada
prueba."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from logistica.core.errors import ValidationError
from logistica.domain import analitica as da

T0 = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
NOMBRES = {A: "Rosario", B: "Buenos Aires", C: "Santos"}
CLIENTES = {"Café SA": uuid.uuid4(), "Soja SA": uuid.uuid4()}


def tramo(
    envio_id, orden, o, d, medio, *, salida=None, llegada=None, costo=None, moneda=None, estado="FINALIZADO"
):
    return da.TramoHecho(
        envio_id,
        orden,
        o,
        NOMBRES[o],
        d,
        NOMBRES[d],
        medio,
        estado,
        salida,
        llegada,
        Decimal(costo) if costo else None,
        moneda,
    )


def envio(
    codigo,
    *,
    cliente="Café SA",
    estado="ENTREGADO",
    salida_h=0,
    entrega_h=None,
    compromiso_h=None,
    creado=T0,
    volumen="10",
    peso="1000",
    incidentes=0,
    abiertos=0,
    ruta="AC",
    costos=(None, None),
    destino="BR",
):
    eid = uuid.uuid4()
    base = creado
    salida = base + timedelta(hours=salida_h) if estado in ("EN_TRANSITO", "ENTREGADO") else None
    entrega = base + timedelta(hours=float(entrega_h)) if entrega_h is not None else None
    if ruta == "AC":  # A -terrestre-> B -marítimo-> C
        tramos = (
            tramo(eid, 1, A, B, "TERRESTRE", salida=salida, llegada=salida, costo=costos[0], moneda="USD"),
            tramo(
                eid,
                2,
                B,
                C,
                "MARITIMO",
                salida=salida,
                llegada=entrega,
                costo=costos[1],
                moneda="USD",
                estado="FINALIZADO" if entrega else "EN_CURSO",
            ),
        )
    else:  # A -aéreo-> C
        tramos = (
            tramo(
                eid,
                1,
                A,
                C,
                "AEREO",
                salida=salida,
                llegada=entrega,
                costo=costos[0],
                moneda="ARS",
                estado="FINALIZADO" if entrega else "EN_CURSO",
            ),
        )
    return da.EnvioHecho(
        eid,
        codigo,
        CLIENTES[cliente],
        cliente,
        estado,
        creado,
        entrega,
        base + timedelta(hours=compromiso_h) if compromiso_h is not None else None,
        Decimal(volumen),
        Decimal(peso),
        "AR",
        destino,
        incidentes,
        abiertos,
        tramos,
    )


TODO = da.Parametros()


# --------------------------------------------------------------------------- métricas de §10.6
def test_duracion_a_tiempo_y_atraso():
    e = envio("E1", salida_h=2, entrega_h=50, compromiso_h=48)
    assert da.duracion_horas(e) == Decimal("48.0")  # 50 − 2
    assert da.a_tiempo(e) is False and da.atraso_horas(e) == Decimal("2.0")
    temprano = envio("E2", entrega_h=40, compromiso_h=48)
    assert da.a_tiempo(temprano) is True and da.atraso_horas(temprano) == 0
    sin_compromiso = envio("E3", entrega_h=40)
    assert da.a_tiempo(sin_compromiso) is None and da.atraso_horas(sin_compromiso) is None
    incoherente = envio("E4", salida_h=10, entrega_h=5)  # entrega antes de salir: no es un caso válido
    assert da.duracion_horas(incoherente) is None
    assert da.duracion_horas(envio("E5", estado="EN_TRANSITO")) is None


def test_sin_casos_es_sin_datos_y_no_cero():
    t = da.tiempos_promedio_entrega([envio("X", estado="EN_TRANSITO")], TODO)
    valores = dict(t.filas)
    assert valores["Entregas consideradas"] == 0 and valores["Duración promedio (h)"] == da.SIN_DATOS
    assert valores["A tiempo (%)"] == da.SIN_DATOS


def test_tiempos_promedio_calculados_a_mano():
    envios = [
        envio("E1", entrega_h=10, compromiso_h=12),  # 10 h, a tiempo
        envio("E2", entrega_h=20, compromiso_h=12),  # 20 h, 8 h de atraso
        envio("E3", entrega_h=30),
    ]  # 30 h, sin compromiso: no cuenta para a tiempo
    valores = dict(da.tiempos_promedio_entrega(envios, TODO).filas)
    assert valores["Entregas consideradas"] == 3
    assert valores["Duración promedio (h)"] == Decimal("20.0")  # (10 + 20 + 30) / 3
    assert valores["Entregas con compromiso"] == 2
    assert valores["A tiempo (%)"] == Decimal("50.0")  # 1 de 2
    assert valores["Atraso promedio (h)"] == Decimal("4.0")  # (0 + 8) / 2


def test_tiempos_maximo_minimo_por_cliente():
    envios = [
        envio("E1", entrega_h=10),
        envio("E2", entrega_h=30),
        envio("E3", cliente="Soja SA", entrega_h=5),
        envio("E4", estado="EN_TRANSITO"),
    ]
    t = da.tiempos_por_cliente(envios, TODO)
    assert t.filas == [
        ("Café SA", 2, Decimal("10.0"), Decimal("30.0"), Decimal("20.0")),
        ("Soja SA", 1, Decimal("5.0"), Decimal("5.0"), Decimal("5.0")),
    ]


# --------------------------------------------------------------------------- reportes operativos
def test_envios_por_pais_estado_y_cliente():
    envios = [
        envio("E1"),
        envio("E2", destino="CL"),
        envio("E3", estado="EN_TRANSITO"),
        envio("E4", cliente="Soja SA", estado="CANCELADO"),
    ]
    assert da.envios_por_pais(envios, TODO).filas == [("BR", 3), ("CL", 1)]
    assert da.envios_por_pais(envios, da.Parametros(segun="ORIGEN")).filas == [("AR", 4)]
    assert da.envios_por_estado(envios, TODO).filas == [
        ("EN_TRANSITO", 1),
        ("ENTREGADO", 2),
        ("CANCELADO", 1),
    ]
    assert da.envios_por_cliente(envios, TODO).filas == [("Café SA", 3, 2, 1), ("Soja SA", 1, 0, 0)]


def test_cohorte_por_fecha_de_creacion():
    viejo = envio("V", creado=T0 - timedelta(days=40))
    nuevo = envio("N")
    p = da.Parametros(desde=T0.date() - timedelta(days=1), hasta=T0.date())
    assert da.envios_por_estado([viejo, nuevo], p).filas == [("ENTREGADO", 1)]


def test_envios_demorados_entregados_y_en_curso():
    ahora = T0 + timedelta(hours=100)
    envios = [
        envio("TARDE", entrega_h=60, compromiso_h=48),
        envio("BIEN", entrega_h=40, compromiso_h=48),
        envio("VENCIDO", estado="EN_TRANSITO", compromiso_h=90),
        envio("EN_HORA", estado="EN_TRANSITO", compromiso_h=200),
    ]
    filas = da.envios_demorados(envios, TODO, ahora).filas
    assert [(f[0], f[2], f[5]) for f in filas] == [
        ("TARDE", "Entregado con atraso", Decimal("12.0")),
        ("VENCIDO", "En curso, compromiso vencido", Decimal("10.0")),
    ]


def test_incidentes_abiertos_por_severidad():
    ahora = T0 + timedelta(hours=5)
    i = [
        da.IncidenteHecho(
            uuid.uuid4(), "E1", CLIENTES["Café SA"], "Café SA", "OTRO", "MEDIA", "ABIERTO", T0, None
        ),
        da.IncidenteHecho(
            uuid.uuid4(), "E2", CLIENTES["Café SA"], "Café SA", "OTRO", "ALTA", "EN_GESTION", T0, "x"
        ),
        da.IncidenteHecho(
            uuid.uuid4(), "E3", CLIENTES["Café SA"], "Café SA", "OTRO", "ALTA", "RESUELTO", T0, None
        ),
    ]
    filas = da.incidentes_abiertos(i, ahora).filas
    assert [(f[0], f[3]) for f in filas] == [("E2", "ALTA"), ("E1", "MEDIA")]
    assert filas[1][6] == Decimal("5.0") and filas[1][7] == "(sin asignar)"


# --------------------------------------------------------------------------- analíticos
def test_promedio_de_incidentes_por_dimension():
    envios = [envio("E1", incidentes=2), envio("E2"), envio("E3", destino="CL", incidentes=1)]
    assert da.incidentes_promedio(envios, TODO).filas == [
        ("BR", 2, 2, Decimal("1.00")),
        ("CL", 1, 1, Decimal("1.00")),
    ]
    por_cliente = da.incidentes_promedio(envios, da.Parametros(dimension="CLIENTE")).filas
    assert por_cliente == [("Café SA", 3, 3, Decimal("1.00"))]


def test_ranking_de_rutas_con_mas_incidentes():
    envios = [envio("E1", incidentes=1), envio("E2", incidentes=2), envio("E3", ruta="AEREO", incidentes=1)]
    filas = da.ranking_rutas_incidentes(envios, TODO).filas
    assert filas[0][:5] == (
        1,
        "Rosario → (terrestre) Buenos Aires → (maritimo) Santos",
        3,
        2,
        Decimal("1.50"),
    )
    assert filas[1][:5] == (2, "Rosario → (aereo) Santos", 1, 1, Decimal("1.00"))


def test_ranking_de_clientes_por_volumen_una_vez_por_envio():
    envios = [
        envio("E1", volumen="10"),
        envio("E2", volumen="5"),
        envio("E3", cliente="Soja SA", volumen="12"),
        envio("E4", cliente="Soja SA", estado="EN_TRANSITO", volumen="100"),
    ]  # no entregado: no cuenta
    filas = da.ranking_clientes_volumen(envios, TODO).filas
    assert [(f[0], f[1], f[2], f[4]) for f in filas] == [
        (1, "Café SA", Decimal("15"), 2),
        (2, "Soja SA", Decimal("12"), 1),
    ]


def test_costos_por_transporte_sin_mezclar_monedas():
    envios = [
        envio("E1", entrega_h=50, costos=("100", "900")),
        envio("E2", estado="EN_TRANSITO", costos=("300", "700")),  # marítimo en curso: no cuenta
        envio("E3", ruta="AEREO", entrega_h=8, costos=("5000", None)),
    ]
    filas = da.costos_por_transporte(envios, TODO).filas
    assert filas == [
        ("AEREO", "ARS", 1, Decimal("5000"), Decimal("5000.00")),
        ("MARITIMO", "USD", 1, Decimal("900"), Decimal("900.00")),
        ("TERRESTRE", "USD", 2, Decimal("400"), Decimal("200.00")),
    ]


def test_desempeno_historico_mes_a_mes():
    julio = T0 + timedelta(days=35)
    envios = [
        envio("E1", entrega_h=10, compromiso_h=12),
        envio("E2", creado=julio, entrega_h=20, compromiso_h=12),
    ]
    filas = da.desempeno_historico(envios, TODO).filas
    assert [(f[0], f[1], f[2], f[3]) for f in filas] == [
        ("2026-06", 1, Decimal("10.0"), Decimal("100.0")),
        ("2026-07", 1, Decimal("20.0"), Decimal("0.0")),
    ]


def test_anomalias_de_temperatura():
    ahora = T0 + timedelta(hours=10)
    alertas = [
        da.AlertaHecho(
            "E1",
            CLIENTES["Café SA"],
            "S1",
            "ALTA",
            "CERRADA",
            T0,
            T0 + timedelta(minutes=50),
            T0 + timedelta(hours=1),
            True,
        ),
        da.AlertaHecho(
            "E2",
            CLIENTES["Café SA"],
            "S2",
            "MEDIA",
            "ABIERTA",
            T0 + timedelta(hours=8),
            T0 + timedelta(hours=9),
            None,
            False,
        ),
    ]
    filas = da.anomalias_temperatura(alertas, TODO, ahora).filas
    assert [(f[0], f[4], f[6]) for f in filas] == [("E1", Decimal("1.0"), "Sí"), ("E2", Decimal("2.0"), "No")]


def test_parametros_validados_y_serializables():
    with pytest.raises(ValidationError):
        da.Parametros(desde=date(2026, 6, 2), hasta=date(2026, 6, 1)).normalizado()
    with pytest.raises(ValidationError):
        da.Parametros(dimension="TRAMO").normalizado()
    p = da.Parametros(date(2026, 1, 1), date(2026, 1, 31), uuid.uuid4(), "cliente", "origen").normalizado()
    assert da.Parametros.desde_json(p.a_json()) == p


# --------------------------------------------------------------------------- predicción
M = da.MetodoPrediccion()
CORTE = T0 + timedelta(days=100)


def historial(n, demorados, *, ruta="AC", dias_atras=10):
    """n entregas de la ruta con `demorados` fuera del compromiso, todas dentro de la ventana."""
    creado = CORTE - timedelta(days=dias_atras)
    return [
        envio(f"H{i}", creado=creado, entrega_h=30 if i < demorados else 10, compromiso_h=20, ruta=ruta)
        for i in range(n)
    ]


def test_misma_ruta_con_muestra_suficiente():
    h = historial(10, 3)
    p = da.predecir(h[0].ruta_clave, h, CORTE, M)
    assert (p.poblacion, p.casos, p.demorados, p.probabilidad, p.riesgo) == (
        "MISMA_RUTA",
        10,
        3,
        Decimal("0.30000"),
        "MEDIO",
    )
    assert p.datos_entrada["casos"] == 10 and "3 de 10" in p.explicacion


def test_respaldo_todas_las_rutas_y_umbrales():
    h = historial(9, 9) + historial(11, 1, ruta="AEREO")  # 9 de la ruta (no alcanza 10), 20 en total
    p = da.predecir(h[0].ruta_clave, h, CORTE, M)
    assert (p.poblacion, p.casos, p.probabilidad, p.riesgo) == (
        "TODAS_LAS_RUTAS",
        20,
        Decimal("0.50000"),
        "ALTO",
    )
    assert da.clasificar(Decimal("0.19999"), M) == "BAJO" and da.clasificar(Decimal("0.2"), M) == "MEDIO"


def test_muestra_insuficiente_es_sin_datos():
    h = historial(9, 1) + historial(10, 1, ruta="AEREO")  # 19 en total
    p = da.predecir(h[0].ruta_clave, h, CORTE, M)
    assert (p.probabilidad, p.riesgo, p.poblacion) == (None, da.SIN_DATOS, "INSUFICIENTE")
    assert "insuficiente" in p.explicacion.lower()


def test_solo_resultados_conocidos_y_validos_en_la_ventana():
    h = historial(10, 0)
    fuera = historial(5, 5, dias_atras=200)  # fuera de los 180 días
    futuro = [envio("F", creado=CORTE, entrega_h=30, compromiso_h=20)]  # entregado DESPUÉS del corte
    sin_compromiso = [envio("S", creado=CORTE - timedelta(days=5), entrega_h=30)]
    en_curso = [envio("C", creado=CORTE - timedelta(days=5), estado="EN_TRANSITO", compromiso_h=1)]
    p = da.predecir(h[0].ruta_clave, h + fuera + futuro + sin_compromiso + en_curso, CORTE, M)
    assert (p.casos, p.demorados, p.riesgo) == (10, 0, "BAJO")


def test_los_minimos_y_umbrales_son_parametros():
    h = historial(4, 2)
    estricto = da.MetodoPrediccion(minimo_ruta=4, umbral_alto=Decimal("0.40"))
    assert da.predecir(h[0].ruta_clave, h, CORTE, estricto).riesgo == "ALTO"


# --------------------------------------------------------------------------- hallazgos de la revisión
def test_dos_clientes_con_la_misma_razon_social_no_se_mezclan():
    otro = da.EnvioHecho(**{**envio("O", entrega_h=10).__dict__, "cliente_id": uuid.uuid4()})
    envios = [envio("E1", entrega_h=10), otro]
    assert len(da.envios_por_cliente(envios, TODO).filas) == 2
    assert len(da.ranking_clientes_volumen(envios, TODO).filas) == 2
    assert len(da.tiempos_por_cliente(envios, TODO).filas) == 2


def test_los_dias_del_filtro_son_utc_aunque_la_fecha_venga_con_otra_zona():
    menos3 = timezone(timedelta(hours=-3))
    e = envio("E1", creado=datetime(2026, 6, 1, 22, tzinfo=menos3))  # = 2 de junio 01:00 UTC
    assert da.envios_por_estado([e], da.Parametros(desde=date(2026, 6, 2), hasta=date(2026, 6, 2))).filas
    assert not da.envios_por_estado([e], da.Parametros(desde=date(2026, 6, 1), hasta=date(2026, 6, 1))).filas
    tarde = envio("E2", entrega_h=0.5, creado=datetime(2026, 6, 30, 22, tzinfo=menos3))  # entrega 1/7 UTC
    assert da.desempeno_historico([tarde], TODO).filas[0][0] == "2026-07"


def test_promedios_y_extremos_con_valores_exactos():
    # Exactos: 10,04 + 10,04 + 10,12 = 30,2 / 3 = 10,07 → 10,1. Redondeando ANTES: (10,0 + 10,0 + 10,1) / 3 →
    # 10,0.
    envios = [envio(f"E{i}", entrega_h=h) for i, h in enumerate(("10.04", "10.04", "10.12"))]
    [fila] = da.tiempos_por_cliente(envios, TODO).filas
    assert fila[2:] == (Decimal("10.0"), Decimal("10.1"), Decimal("10.1"))


def test_costos_de_un_mismo_medio_en_dos_monedas_quedan_separados():
    e = envio("E1", entrega_h=50, costos=("100", "900"))
    pesos = da.TramoHecho(**{**e.tramos[0].__dict__, "moneda": "ARS", "orden": 3})
    e = da.EnvioHecho(**{**e.__dict__, "tramos": e.tramos + (pesos,)})
    filas = da.costos_por_transporte([e], TODO).filas
    assert [(f[0], f[1]) for f in filas] == [("MARITIMO", "USD"), ("TERRESTRE", "ARS"), ("TERRESTRE", "USD")]


def test_umbrales_exactos_a_traves_de_predecir():
    h = historial(10, 2)  # 2/10 = 0,20 → MEDIO (el 20 % ya es medio)
    assert da.predecir(h[0].ruta_clave, h, CORTE, M).riesgo == "MEDIO"
    h = historial(10, 5)  # 0,50 → ALTO
    assert da.predecir(h[0].ruta_clave, h, CORTE, M).riesgo == "ALTO"


def test_un_envio_en_curso_nunca_entra_en_su_propia_muestra():
    h = historial(10, 0)
    propio = envio("P", estado="EN_TRANSITO", creado=CORTE - timedelta(days=1), compromiso_h=1)
    p = da.predecir(propio.ruta_clave, h + [propio], CORTE, M)
    assert p.casos == 10


def test_responsable_oculto_para_externos_y_duracion_de_alerta_cerrada_a_mano():
    i = [
        da.IncidenteHecho(
            uuid.uuid4(),
            "E1",
            CLIENTES["Café SA"],
            "Café SA",
            "OTRO",
            "ALTA",
            "ABIERTO",
            T0,
            "ops@empresa.local",
        )
    ]
    t = da.incidentes_abiertos(i, T0 + timedelta(hours=1), mostrar_responsable=False)
    assert "Responsable" not in t.columnas and not any("ops@" in str(v) for f in t.filas for v in f)
    cerrada = da.AlertaHecho(
        "E1",
        CLIENTES["Café SA"],
        "S1",
        "MEDIA",
        "CERRADA",
        T0,
        T0 + timedelta(minutes=5),
        None,
        False,
        cerrada_en=T0 + timedelta(hours=3),
    )
    [fila] = da.anomalias_temperatura([cerrada], TODO, T0 + timedelta(hours=9)).filas
    assert fila[4] == Decimal("3.0")  # hasta el cierre manual, no hasta la última detección


def test_csv_no_permite_inyectar_formulas():
    assert da.celda_csv('=HYPERLINK("x")').startswith("'") and da.celda_csv("@SUMA").startswith("'")
    assert da.celda_csv(Decimal("-5")) == "-5" and da.celda_csv("Café SA") == "Café SA"
