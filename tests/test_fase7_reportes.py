"""Fase 7 — reportes y predicción contra PostgreSQL real (transacción revertida), con resultados calculados a
mano.

Cada consulta se filtra por el cliente que crea la prueba: el resultado no depende de otros datos de la base.
Envío de prueba (helper `entregado`): sale a h(0,5), se entrega a h(81) y su compromiso es h(80) (la llegada
estimada del último tramo, fijada al iniciar). Duración = 80,5 h; atraso = 1 h. Costos: terrestre 1500 USD,
marítimo 9000 USD.
"""

from __future__ import annotations

import csv
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from logistica.core.errors import BusinessRuleError, NotFoundError, PermissionDeniedError, SessionExpiredError
from logistica.domain.analitica import SIN_DATOS, Parametros
from logistica.repositories import archivos
from logistica.services.prediccion_service import PrediccionService
from logistica.services.reporte_service import ReporteService
from tests.test_fase6_comercial import Comercial

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y PostgreSQL levantado)",
)


class Reportes(Comercial):
    def __init__(self, conns, carpeta):
        super().__init__(conns)
        self.rep = ReporteService(conns, self.aut, carpeta)
        self.pred = PrediccionService(conns, self.aut)
        self.ops_s = self.ops.sesion_id

    def mio(self, **kw):
        return Parametros(cliente_id=self.cliente_sa.id, **kw)


@pytest.fixture()
def r(tx_connections, tmp_path):
    return Reportes(tx_connections, tmp_path)


def valores(tabla):
    return dict(tabla.filas)


# --------------------------------------------------------------------------- resultados contra el cálculo a
# mano
def test_tiempos_y_demoras_coinciden_con_el_calculo_a_mano(r):
    r.entregado(cargos=())
    v = valores(r.rep.consultar(r.ops_s, "TIEMPOS_PROMEDIO_ENTREGA", r.mio()))
    assert v["Entregas consideradas"] == 1 and v["Duración promedio (h)"] == Decimal("80.5")
    assert v["A tiempo (%)"] == Decimal("0.0") and v["Atraso promedio (h)"] == Decimal("1.0")
    demorados = r.rep.consultar(r.ops_s, "ENVIOS_DEMORADOS", r.mio()).filas
    assert [(f[2], f[5]) for f in demorados] == [("Entregado con atraso", Decimal("1.0"))]
    [fila] = r.rep.consultar(r.ops_s, "TIEMPOS_POR_CLIENTE", r.mio()).filas
    assert fila[1:] == (1, Decimal("80.5"), Decimal("80.5"), Decimal("80.5"))


def test_cantidades_por_estado_pais_y_volumen(r):
    r.entregado(cargos=())
    r.nuevo_envio()  # BORRADOR
    assert valores(r.rep.consultar(r.ops_s, "ENVIOS_POR_ESTADO", r.mio())) == {"BORRADOR": 1, "ENTREGADO": 1}
    assert valores(r.rep.consultar(r.ops_s, "ENVIOS_POR_PAIS", r.mio())) == {"BR": 2}  # destino: Santos
    assert valores(r.rep.consultar(r.ops_s, "ENVIOS_POR_PAIS", r.mio(segun="ORIGEN"))) == {"AR": 2}
    ranking = r.rep.consultar(r.ops_s, "RANKING_CLIENTES_VOLUMEN", Parametros()).filas
    [fila] = [f for f in ranking if f[1] == r.cliente_sa.razon_social]
    assert fila[2:] == (Decimal("20.000"), Decimal("8000.000"), 1)  # solo el entregado; una vez por envío


def test_costos_por_transporte_y_ruta_con_incidentes(r):
    p = r.entregado(cargos=())
    filas = r.rep.consultar(r.comercial, "COSTOS_POR_TRANSPORTE", r.mio()).filas
    assert [(f[0], f[1], f[2], f[3]) for f in filas] == [
        ("MARITIMO", "USD", 1, Decimal("9000.00")),
        ("TERRESTRE", "USD", 1, Decimal("1500.00")),
    ]
    with r.conns.pg_session() as s:
        s.execute(
            text(
                "INSERT INTO incidente (id, envio_id, tipo, severidad, descripcion, ocurrido_en)"
                " VALUES (:i, :e, 'OTRO', 'ALTA', 'Prueba', clock_timestamp())"
            ),
            {"i": uuid.uuid4(), "e": p.id},
        )
    [ruta] = r.rep.consultar(r.ops_s, "RANKING_RUTAS_INCIDENTES", r.mio()).filas
    assert ruta[2:] == (1, 1, Decimal("1.00")) and ruta[1].endswith(r.C.nombre)
    [abierto] = r.rep.consultar(r.ops_s, "INCIDENTES_ABIERTOS", r.mio()).filas
    assert abierto[0] == p.codigo and abierto[3] == "ALTA"


def test_sin_datos_no_es_cero(r):
    v = valores(r.rep.consultar(r.ops_s, "TIEMPOS_PROMEDIO_ENTREGA", r.mio()))
    assert v["Entregas consideradas"] == 0 and v["Duración promedio (h)"] == SIN_DATOS


# --------------------------------------------------------------------------- permisos y alcance
def test_un_cliente_solo_ve_sus_datos_y_no_los_reportes_internos(r):
    r.entregado(cargos=())
    cliente = r.login(r.emails["CLIENTE"]).sesion_id
    codigos = {t.codigo for t in r.rep.tipos(cliente)}
    assert "TIEMPOS_PROMEDIO_ENTREGA" in codigos and "PREDICCION_ATRASOS" in codigos
    assert not codigos & {
        "ENVIOS_POR_CLIENTE",
        "RANKING_CLIENTES_VOLUMEN",
        "COSTOS_POR_TRANSPORTE",
        "TIEMPOS_POR_CLIENTE",
    }
    otro = uuid.uuid4()  # aunque pida OTRO cliente, se fuerza su empresa
    v = valores(r.rep.consultar(cliente, "TIEMPOS_PROMEDIO_ENTREGA", Parametros(cliente_id=otro)))
    assert v["Entregas consideradas"] == 1
    for codigo in ("COSTOS_POR_TRANSPORTE", "ENVIOS_POR_CLIENTE"):
        with pytest.raises(PermissionDeniedError):
            r.rep.consultar(cliente, codigo, Parametros())


def test_permisos_por_grupo_de_reporte(r):
    with pytest.raises(PermissionDeniedError):  # Operaciones no tiene reportes comerciales
        r.rep.consultar(r.ops_s, "COSTOS_POR_TRANSPORTE", Parametros())
    with pytest.raises(PermissionDeniedError):  # Comercial no tiene reportes operativos
        r.rep.consultar(r.comercial, "ENVIOS_POR_ESTADO", Parametros())
    operador = r.login(r.emails["OPERADOR_LOGISTICO"]).sesion_id
    assert [t.codigo for t in r.rep.tipos(operador)] == ["PREDICCION_ATRASOS"]


# --------------------------------------------------------------------------- generación registrada y apertura
def test_generar_registrar_y_abrir_csv_y_pdf(r):
    r.entregado(cargos=())
    rep = r.rep.generar(r.ops_s, "ENVIOS_POR_ESTADO", r.mio(), "CSV")
    assert rep.estado == "COMPLETADO" and rep.archivo_clave.endswith(".csv")
    ruta = r.rep.abrir(r.ops_s, rep.id)
    with ruta.open(encoding="utf-8-sig") as f:
        filas = list(csv.reader(f, delimiter=";"))
    assert filas[0] == ["Envíos por estado"] and ["ENTREGADO", "1"] in filas
    pdf = r.rep.generar(r.ops_s, "TIEMPOS_PROMEDIO_ENTREGA", r.mio(), "pdf")
    assert r.rep.abrir(r.ops_s, pdf.id).read_bytes().startswith(b"%PDF")
    assert {x.id for x in r.rep.reportes(r.ops_s)} >= {rep.id, pdf.id}
    with r.conns.pg_session() as s:
        acciones = (
            s.execute(
                text("SELECT accion FROM auditoria WHERE entidad_id = :r ORDER BY fecha"), {"r": rep.id}
            )
            .scalars()
            .all()
        )
    assert acciones == ["solicitar_reporte", "abrir_reporte"]


def test_abrir_reporte_ajeno_o_sin_archivo(r):
    rep = r.rep.generar(r.ops_s, "ENVIOS_POR_ESTADO", r.mio(), "CSV")
    cliente = r.login(r.emails["CLIENTE"]).sesion_id
    with pytest.raises(NotFoundError):  # no es suyo
        r.rep.abrir(cliente, rep.id)
    r.rep.abrir(r.ops_s, rep.id).unlink()  # el archivo no está "en este equipo"
    with pytest.raises(NotFoundError, match="archivo"):
        r.rep.abrir(r.ops_s, rep.id)
    otra = r.rep.repetir(r.ops_s, rep.id)  # se vuelve a generar con los mismos parámetros
    assert otra.id != rep.id and otra.parametros == rep.parametros and r.rep.abrir(r.ops_s, otra.id).is_file()


def test_un_fallo_al_escribir_deja_el_reporte_en_error(r, monkeypatch):
    def romper(*a, **k):
        raise OSError("disco lleno")

    monkeypatch.setattr(archivos.EscritorCSV, "escribir", romper)
    with pytest.raises(BusinessRuleError):
        r.rep.generar(r.ops_s, "ENVIOS_POR_ESTADO", r.mio(), "CSV")
    [rep] = r.rep.reportes(r.ops_s)
    assert (rep.estado, rep.error_codigo, rep.archivo_clave) == ("ERROR", "OSError", None)


def test_revocar_la_sesion_durante_la_generacion_descarta_el_resultado(r, monkeypatch):
    original = archivos.ArchivosReportes.guardar

    def revocar_y_guardar(self, *a, **k):
        r.acceso.logout(r.ops_s)  # la sesión se cierra mientras se escribía el archivo
        return original(self, *a, **k)

    monkeypatch.setattr(archivos.ArchivosReportes, "guardar", revocar_y_guardar)
    with pytest.raises(SessionExpiredError):
        r.rep.generar(r.ops_s, "ENVIOS_POR_ESTADO", r.mio(), "CSV")
    with r.conns.pg_session() as s:
        estado = s.execute(
            text("SELECT estado FROM reporte WHERE parametros->>'cliente_id' = :c"),
            {"c": str(r.cliente_sa.id)},
        ).scalar_one()
    assert estado == "ERROR"  # nunca COMPLETADO para una sesión revocada


# --------------------------------------------------------------------------- predicción
def test_prediccion_diaria_idempotente_y_sin_datos_con_poca_historia(r):
    p = r.planificado()
    primera = r.pred.calcular()
    segunda = r.pred.calcular()
    assert (
        primera.calculadas >= 1
        and segunda.calculadas == 0
        and segunda.existentes == primera.calculadas + primera.existentes
    )
    [x] = [x for x in r.pred.predicciones(r.ops_s) if x.envio_id == p.id]
    if x.datos_entrada["comparables_todas"] < 20:  # base de desarrollo con poca historia
        assert (x.probabilidad, x.riesgo_operativo) == (None, SIN_DATOS)
    assert x.riesgo_climatico == SIN_DATOS and x.metodo == "FRECUENCIA_HISTORICA 1.0"
    assert "frecuencia" in r.rep.consultar(r.ops_s, "PREDICCION_ATRASOS", Parametros()).notas[0].lower()


def test_predicciones_por_alcance_y_calculo_solo_interno(r):
    p = r.planificado()
    r.pred.calcular()
    cliente = r.login(r.emails["CLIENTE"]).sesion_id
    assert {x.envio_id for x in r.pred.predicciones(cliente)} == {p.id}
    with pytest.raises(PermissionDeniedError):
        r.pred.calcular_ahora(cliente)
    assert r.pred.calcular_ahora(r.ops_s).existentes >= 1


# --------------------------------------------------------------------------- anomalías (datos del monitoreo)
def test_anomalias_de_temperatura_desde_las_alertas(tx_connections, tmp_path):
    from tests.test_fase4_monitoreo import Monitoreo

    m = Monitoreo(tx_connections)
    try:
        for seg in (0, 60, 120, 180, 240, 300):
            m.leer(seg, temperatura_c=12)
        rep = ReporteService(tx_connections, m.aut, tmp_path)
        [fila] = rep.consultar(
            m.ops.sesion_id, "ANOMALIAS_TEMPERATURA", Parametros(cliente_id=m.cliente_sa.id)
        ).filas
        assert fila[0] == m.envio.codigo and fila[5] == "ALTA" and fila[6] == "Sí"
    finally:
        tx_connections.mongo_db["lecturas_sensor"].delete_many({"sensor_id": {"$in": m.sensores}})
        tx_connections.mongo_db["eventos_logisticos"].delete_many({"envio_id": m.envio.id})


# --------------------------------------------------------------------------- hallazgos de la revisión
def test_el_cliente_no_ve_el_email_del_responsable_interno(r):
    p = r.entregado(cargos=())
    with r.conns.pg_session() as s:
        s.execute(
            text(
                "INSERT INTO incidente (id, envio_id, tipo, severidad, descripcion, ocurrido_en, estado,"
                " responsable_id) VALUES (:i, :e, 'OTRO', 'ALTA', 'Prueba', clock_timestamp(), 'EN_GESTION', "
                ":u)"
            ),
            {"i": uuid.uuid4(), "e": p.id, "u": r.ops.usuario_id},
        )
    cliente = r.login(r.emails["CLIENTE"]).sesion_id
    t = r.rep.consultar(cliente, "INCIDENTES_ABIERTOS", Parametros())
    assert not any("@" in str(v) for f in t.filas for v in f)
    assert any(
        "@" in str(v) for f in r.rep.consultar(r.ops_s, "INCIDENTES_ABIERTOS", r.mio()).filas for v in f
    )


def test_un_reporte_fallido_no_deja_archivos(r, monkeypatch, tmp_path):
    original = archivos.ArchivosReportes.guardar

    def revocar_y_guardar(self, *a, **k):
        r.acceso.logout(r.ops_s)
        return original(self, *a, **k)

    monkeypatch.setattr(archivos.ArchivosReportes, "guardar", revocar_y_guardar)
    with pytest.raises(SessionExpiredError):
        r.rep.generar(r.ops_s, "ENVIOS_POR_ESTADO", r.mio(), "CSV")
    assert list(tmp_path.iterdir()) == []


def test_un_escritor_que_falla_a_mitad_no_deja_temporales(r, monkeypatch, tmp_path):
    def a_medias(self, tabla, ruta, subtitulo):
        ruta.write_text("parcial", encoding="utf-8")
        raise OSError("disco lleno")

    monkeypatch.setattr(archivos.EscritorCSV, "escribir", a_medias)
    with pytest.raises(BusinessRuleError):
        r.rep.generar(r.ops_s, "ENVIOS_POR_ESTADO", r.mio(), "CSV")
    assert list(tmp_path.iterdir()) == []


def test_prediccion_sin_datos_con_minimos_inalcanzables(r):
    from logistica.domain.analitica import MetodoPrediccion

    p = r.planificado()
    pred = PrediccionService(r.conns, r.aut, MetodoPrediccion(minimo_ruta=10**6, minimo_global=10**6))
    pred.calcular()
    [x] = [x for x in pred.predicciones(r.ops_s) if x.envio_id == p.id]
    assert (x.probabilidad, x.riesgo_operativo, x.datos_entrada["poblacion"]) == (
        None,
        SIN_DATOS,
        "INSUFICIENTE",
    )
