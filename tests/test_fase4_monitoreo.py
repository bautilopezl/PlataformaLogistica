"""Fase 4 — monitoreo contra PostgreSQL y MongoDB reales, con reloj controlado.

Cubre el flujo "Lectura IoT" de AGENTS.md §9: ingesta idempotente en Mongo → evaluación en UNA transacción de
PostgreSQL → marca en Mongo, incluida la caída entre el commit y la marca. PostgreSQL se revierte solo
(`tx_connections`); las lecturas y eventos de Mongo se borran al terminar. Requiere LOGISTICA_TEST_CONNECTIONS=1.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from logistica.core.errors import BusinessRuleError, PermissionDeniedError, ValidationError
from logistica.domain.logistica import SensorDatos
from logistica.domain.monitoreo_dtos import CondicionDatos
from logistica.repositories.mongo.lecturas import LecturasRepository
from logistica.services.monitoreo_service import LecturaEntrada, MonitoreoService
from tests.test_fase2_logistica import Logistica, h

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env, PostgreSQL y MongoDB levantados)",
)

SALIDA = h(0.5)  # inicio del primer tramo: desde acá el contenedor está EN USO y el sensor tiene contexto


class Reloj:
    """Reloj manual: la prueba decide qué hora es (nada de sleep)."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Monitoreo(Logistica):
    """Envío A→B→C en tránsito con un sensor instalado en su contenedor y condiciones de 2 a 8 °C."""

    def __init__(self, conns):
        super().__init__(conns)
        s = self.ops.sesion_id
        self.reloj = Reloj(SALIDA)
        self.mon = MonitoreoService(conns, self.aut, reloj=self.reloj)
        self.sensor = self.cat.crear_sensor(s, SensorDatos(
            f"SEN-{self.sufijo}", "MULTISENSOR", "Acme",
            capacidades=frozenset({"TEMPERATURA", "HUMEDAD", "VIBRACION", "BATERIA", "GPS"})))
        self.sensores = [self.sensor.id]
        self.cat.instalar_sensor(s, self.sensor.id, self.cont1.id, h(0))
        self.envio = self.planificado()
        self.mon.definir_condiciones(s, self.envio.id, CondicionDatos(
            temperatura_min_c="2", temperatura_max_c="8", vibracion_max_g="3", bateria_min_pct="20"))
        self.t1, self.t2 = self.env.obtener(s, self.envio.id).tramos
        self.env.iniciar_tramo(s, self.t1.id, SALIDA)

    def leer(self, seg, demora=5, lectura_id=None, sensor_id=None, evaluar=True, **mediciones):
        """Lectura medida `seg` segundos después de la salida y recibida `demora` segundos más tarde."""
        medida = SALIDA + timedelta(seconds=seg)
        self.reloj.t = medida + timedelta(seconds=demora)
        entrada = LecturaEntrada(lectura_id or uuid.uuid4(), sensor_id or self.sensor.id, medida,
                                 mediciones or {"temperatura_c": 5}, latitud=-34.6, longitud=-58.4,
                                 origen="SIMULADOR")
        nueva = self.mon.registrar_lectura(entrada)
        resultado = self.mon.evaluar_pendientes(sensor_ids=[entrada.sensor_id]) if evaluar else None
        return entrada, nueva, resultado

    def alertas(self, tipo="TEMPERATURA", solo_activas=False):
        return [a for a in self.mon.alertas(self.ops.sesion_id, solo_activas=solo_activas, envio_id=self.envio.id)
                if a.tipo == tipo]

    def incidentes(self):
        return self.mon.incidentes(self.ops.sesion_id, solo_abiertos=False, envio_id=self.envio.id)

    def evaluacion(self, lectura_id):
        return self.conns.mongo_db["lecturas_sensor"].find_one({"_id": lectura_id})["evaluacion"]


@pytest.fixture()
def m(tx_connections):
    entorno = Monitoreo(tx_connections)
    yield entorno
    db = tx_connections.mongo_db
    db["lecturas_sensor"].delete_many({"sensor_id": {"$in": entorno.sensores}})
    db["eventos_logisticos"].delete_many({"envio_id": entorno.envio.id})


def outbox_incidentes(m):
    with m.conns.pg_session() as s:
        return s.execute(text(
            "SELECT e.evento_tipo, e.agregado_version, array_agg(x.destino) FROM outbox_evento e"
            " JOIN outbox_entrega x ON x.evento_id = e.id JOIN incidente i ON i.id = e.agregado_id"
            " WHERE e.agregado_tipo = 'INCIDENTE' AND i.envio_id = :e GROUP BY e.id ORDER BY e.agregado_version"),
            {"e": m.envio.id}).all()


# --------------------------------------------------------------------------- escenario de aceptación
def test_temperatura_sostenida_abre_escala_con_incidente_y_normaliza_sin_resolverlo(m):
    for seg in (0, 60):
        m.leer(seg, temperatura_c=11)
    assert m.alertas() == []  # 60 s fuera de rango todavía no alcanza
    m.leer(120, temperatura_c=11)
    [alerta] = m.alertas()
    assert (alerta.severidad, alerta.estado) == ("MEDIA", "ABIERTA")
    for seg in (180, 240):
        m.leer(seg, temperatura_c=12)
    assert m.alertas()[0].severidad == "MEDIA" and m.incidentes() == []
    m.leer(300, temperatura_c=12)
    [alerta] = m.alertas()
    assert alerta.severidad == "ALTA" and len(m.alertas()) == 1  # se actualiza la misma alerta, no se crea otra
    [incidente] = m.incidentes()
    assert (incidente.tipo, incidente.estado, incidente.alerta_origen_id) == (
        "DESVIACION_TEMPERATURA", "ABIERTO", alerta.id)
    assert incidente.alertas == [alerta.id]  # vínculo en incidente_alerta, en la misma transacción
    assert outbox_incidentes(m) == [("INCIDENTE_ABIERTO", 1, ["MONGO_EVENTOS"])]  # SUPUESTO M1: sin Neo4j
    for seg in (360, 420):
        m.leer(seg, temperatura_c=5)
    assert m.alertas()[0].estado == "ABIERTA"  # 60 s en rango todavía no normaliza
    m.leer(480, temperatura_c=5)
    [alerta] = m.alertas()
    assert (alerta.estado, alerta.cierre_tipo) == ("CERRADA", "AUTOMATICO")
    assert m.incidentes()[0].estado == "ABIERTO"  # cerrar la alerta NO resuelve el incidente


def test_un_hueco_mayor_a_120_s_reinicia_la_secuencia(m):
    m.leer(0, temperatura_c=11)
    m.leer(60, temperatura_c=11)
    m.leer(200, temperatura_c=11)  # 140 s sin lecturas: no se presume que siguió fuera de rango
    m.leer(260, temperatura_c=11)
    assert m.alertas() == []
    m.leer(320, temperatura_c=11)
    assert len(m.alertas()) == 1


# --------------------------------------------------------------------------- idempotencia y fallos
def test_lectura_reenviada_no_se_duplica_ni_se_reevalua(m):
    entrada, nueva, r = m.leer(0, temperatura_c=11)
    assert nueva and r.evaluadas == 1
    assert m.mon.registrar_lectura(entrada) is False  # mismo _id: no se toca
    assert m.mon.evaluar_pendientes(sensor_ids=m.sensores).evaluadas == 0
    assert m.conns.mongo_db["lecturas_sensor"].count_documents({"_id": entrada.id}) == 1
    with m.conns.pg_session() as s:
        assert s.execute(text("SELECT count(*) FROM lectura_procesada WHERE lectura_id = :l"),
                         {"l": entrada.id}).scalar_one() == 1


def test_caida_entre_el_commit_y_la_marca_en_mongo_no_duplica_efectos(m, monkeypatch):
    for seg in (0, 60):
        m.leer(seg, temperatura_c=11)
    original = LecturasRepository.marcar_procesada
    caidas = []

    def caer_una_vez(self, doc, **kwargs):
        if not caidas:
            caidas.append(doc["_id"])
            raise ConnectionError("proceso interrumpido antes de marcar en Mongo")
        return original(self, doc, **kwargs)

    monkeypatch.setattr(LecturasRepository, "marcar_procesada", caer_una_vez)
    entrada, _, r = m.leer(120, temperatura_c=11)  # abre la alerta, commitea y se "cae" al marcar
    assert r.reintentos == 1 and caidas == [entrada.id]
    [alerta] = m.alertas()
    assert m.evaluacion(entrada.id)["estado"] == "PENDIENTE"  # Mongo no se enteró
    m.reloj.t += timedelta(seconds=31)  # vence la espera del reintento
    r = m.mon.evaluar_pendientes(sensor_ids=m.sensores)
    assert (r.repetidas, r.evaluadas) == (1, 0)  # encontró lectura_procesada: solo marcó
    assert m.evaluacion(entrada.id)["estado"] == "PROCESADA"
    assert m.alertas() == [alerta]  # misma alerta, sin una detección extra


def test_postgres_caido_pospone_sin_gastar_intentos(m, monkeypatch):
    from logistica.core.errors import DatabaseUnavailableError
    from logistica.services import monitoreo_service

    entrada, _, _ = m.leer(0, evaluar=False)

    def caido(self, doc):
        raise DatabaseUnavailableError("sin conexión")

    monkeypatch.setattr(monitoreo_service.MonitoreoService, "_evaluar", caido)
    r = m.mon.evaluar_pendientes(sensor_ids=m.sensores)
    ev = m.evaluacion(entrada.id)
    assert r.reintentos == 1 and (ev["estado"], ev["intentos"]) == ("PENDIENTE", 0)


def test_lectura_tardia_queda_como_historico_sin_mover_el_estado(m):
    m.leer(0, temperatura_c=5)
    entrada, _, r = m.leer(60, demora=200, temperatura_c=30)  # llegó 200 s después de medirse
    assert (r.historicas, r.evaluadas) == (1, 0)
    assert m.evaluacion(entrada.id)["resultado"] == "TARDIA"
    assert m.alertas("TEMPERATURA") == []


def test_reloj_adelantado_y_sin_contexto_quedan_para_revision(m):
    adelantada, _, r = m.leer(0, demora=-300)  # medida 300 s "después" de recibida
    assert r.revision == 1
    assert (m.evaluacion(adelantada.id)["estado"], m.evaluacion(adelantada.id)["resultado"]) == (
        "ERROR", "RELOJ_ADELANTADO")
    suelto = m.cat.crear_sensor(m.ops.sesion_id, SensorDatos(f"SUE-{m.sufijo}", "MULTISENSOR", "Acme",
                                                              capacidades=frozenset({"TEMPERATURA"})))
    m.sensores.append(suelto.id)
    entrada, nueva, r = m.leer(60, sensor_id=suelto.id)  # sensor no instalado: se guarda igual
    doc = m.conns.mongo_db["lecturas_sensor"].find_one({"_id": entrada.id})
    assert nueva and "envio_id" not in doc and r.revision == 1
    assert m.evaluacion(entrada.id)["resultado"] == "SIN_CONTEXTO"


def test_mediciones_invalidas_se_rechazan_antes_de_guardar(m):
    with pytest.raises(ValidationError):
        m.leer(0, humedad_pct=140)
    with pytest.raises(ValidationError):
        m.mon.registrar_lectura(LecturaEntrada(uuid.uuid4(), m.sensor.id, SALIDA, {"temperatura_c": 5},
                                               latitud=95, longitud=0))


# --------------------------------------------------------------------------- sin comunicación
def test_sin_comunicacion_se_calcula_abre_a_180_escala_a_600_y_cierra_con_una_lectura(m):
    m.leer(0)
    m.reloj.t = SALIDA + timedelta(seconds=170)
    assert m.mon.evaluar_comunicacion(sensor_ids=m.sensores) == 0
    m.reloj.t = SALIDA + timedelta(seconds=181)
    assert m.mon.evaluar_comunicacion(sensor_ids=m.sensores) == 1
    assert m.mon.evaluar_comunicacion(sensor_ids=m.sensores) == 0  # repetir no crea otra alerta
    [alerta] = m.alertas("SIN_COMUNICACION")
    assert alerta.severidad == "MEDIA"
    m.reloj.t = SALIDA + timedelta(seconds=601)
    assert m.mon.evaluar_comunicacion(sensor_ids=m.sensores) == 1
    assert m.alertas("SIN_COMUNICACION")[0].severidad == "ALTA"
    m.leer(605)
    assert m.alertas("SIN_COMUNICACION")[0].estado == "CERRADA"


def test_sensor_que_nunca_envio_se_detecta_desde_el_inicio_del_monitoreo(m):
    m.reloj.t = SALIDA + timedelta(seconds=181)
    assert m.mon.evaluar_comunicacion(sensor_ids=m.sensores) == 1


# --------------------------------------------------------------------------- acciones de usuario
def test_reconocer_no_cierra_y_cerrar_a_mano_exige_motivo(m):
    m.leer(0, vibracion_g=5)  # vibración: abre con una sola lectura
    [alerta] = m.alertas("VIBRACION")
    s = m.ops.sesion_id
    assert m.mon.reconocer_alerta(s, alerta.id).estado == "RECONOCIDA"
    with pytest.raises(BusinessRuleError):
        m.mon.reconocer_alerta(s, alerta.id)
    with pytest.raises(ValidationError):
        m.mon.cerrar_alerta(s, alerta.id, "  ")
    cerrada = m.mon.cerrar_alerta(s, alerta.id, "Golpe en la grúa, carga revisada")
    assert (cerrada.estado, cerrada.cierre_tipo, cerrada.motivo_cierre) == (
        "CERRADA", "MANUAL", "Golpe en la grúa, carga revisada")
    with m.conns.pg_session() as tx:
        quien, cuando = tx.execute(text("SELECT cerrada_por_id, cerrada_en FROM alerta WHERE id = :id"),
                                   {"id": alerta.id}).one()
        acciones = tx.execute(text("SELECT accion FROM auditoria WHERE entidad_id = :id ORDER BY fecha"),
                              {"id": alerta.id}).scalars().all()
    assert quien is not None and cuando is not None
    assert acciones == ["reconocer_alerta", "cerrar_alerta"]


def test_bateria_baja_y_su_histeresis(m):
    m.leer(0, bateria_pct=15)
    [alerta] = m.alertas("BATERIA")
    assert alerta.severidad == "BAJA"
    m.leer(60, bateria_pct=23)  # sobre el mínimo, pero a menos de 5 puntos: sigue abierta
    assert m.alertas("BATERIA")[0].estado == "ABIERTA"
    m.leer(120, bateria_pct=25)
    assert m.alertas("BATERIA")[0].estado == "CERRADA"


def test_incidente_manual_desde_alerta_y_resolucion(m):
    m.leer(0, vibracion_g=5)
    [alerta] = m.alertas("VIBRACION")
    s = m.ops.sesion_id
    inc = m.mon.crear_incidente(s, m.envio.id, tipo="VIBRACION", severidad="MEDIA",
                                descripcion="Impacto durante la carga", alerta_id=alerta.id)
    assert inc.alertas == [alerta.id] and inc.tramo_id == m.t1.id
    with pytest.raises(BusinessRuleError):  # una alerta no origina dos incidentes
        m.mon.crear_incidente(s, m.envio.id, tipo="VIBRACION", severidad="MEDIA", descripcion="otra vez",
                              alerta_id=alerta.id)
    assert m.mon.tomar_incidente(s, inc.id).estado == "EN_GESTION"
    with pytest.raises(ValidationError):
        m.mon.resolver_incidente(s, inc.id, "")
    resuelto = m.mon.resolver_incidente(s, inc.id, "Carga inspeccionada sin daños")
    assert resuelto.estado == "RESUELTO" and resuelto.resuelto_en >= resuelto.abierto_en
    assert [r[0] for r in outbox_incidentes(m)] == ["INCIDENTE_ABIERTO", "INCIDENTE_RESUELTO"]


def test_permisos_y_alcance(m):
    m.leer(0, vibracion_g=5)
    [alerta] = m.alertas("VIBRACION")
    comercial = m.login(m.emails["COMERCIAL"]).sesion_id
    cliente = m.login(m.emails["CLIENTE"]).sesion_id
    operador = m.login(m.emails["OPERADOR_LOGISTICO"]).sesion_id
    with pytest.raises(PermissionDeniedError):
        m.mon.alertas(comercial)  # Comercial no ve monitoreo
    with pytest.raises(PermissionDeniedError):
        m.mon.reconocer_alerta(cliente, alerta.id)  # el cliente ve, pero no reconoce
    assert [a.id for a in m.mon.alertas(cliente)] == [alerta.id]
    with pytest.raises(PermissionDeniedError):  # externo: solo por envío de su alcance
        m.mon.lecturas(cliente, campo="sensor_id", valor=m.sensor.id)
    assert len(m.mon.lecturas(cliente, campo="envio_id", valor=m.envio.id).lecturas) == 1
    assert m.mon.reconocer_alerta(operador, alerta.id).estado == "RECONOCIDA"  # es SU tramo
    with pytest.raises(PermissionDeniedError):
        m.mon.cerrar_alerta(operador, alerta.id, "motivo")  # cerrar es de Operaciones


def test_operador_solo_ve_lecturas_de_sus_tramos(m):
    m.leer(0)
    m.env.finalizar_tramo(m.ops.sesion_id, m.t1.id, SALIDA + timedelta(seconds=100))
    # El tramo 2 pasa a otro operador antes de iniciarse: se reasigna en la base para no rehacer la planificación.
    with m.conns.pg_session() as s:
        s.execute(text("UPDATE tramo SET operador_id = :o WHERE id = :t"), {"o": m.operador2.id, "t": m.t2.id})
    m.leer(120)  # sin tramo en curso: no pertenece a ningún operador
    operador = m.login(m.emails["OPERADOR_LOGISTICO"]).sesion_id
    assert len(m.mon.lecturas(operador, campo="envio_id", valor=m.envio.id).lecturas) == 1
    assert len(m.mon.lecturas(m.ops.sesion_id, campo="envio_id", valor=m.envio.id).lecturas) == 2


# --------------------------------------------------------------------------- consultas
def test_historial_paginado_estable_y_con_indice(m):
    ids = [m.leer(seg, evaluar=False)[0].id for seg in range(0, 300, 60)]
    s = m.ops.sesion_id
    p1 = m.mon.lecturas(s, campo="sensor_id", valor=m.sensor.id, limite=2)
    p2 = m.mon.lecturas(s, campo="sensor_id", valor=m.sensor.id, limite=2, despues=p1.siguiente)
    p3 = m.mon.lecturas(s, campo="sensor_id", valor=m.sensor.id, limite=2, despues=p2.siguiente)
    assert [x.id for x in p1.lecturas + p2.lecturas + p3.lecturas] == ids and p3.siguiente is None
    rango = m.mon.lecturas(s, campo="sensor_id", valor=m.sensor.id, desde=SALIDA + timedelta(seconds=60),
                           hasta=SALIDA + timedelta(seconds=180))
    assert [x.id for x in rango.lecturas] == ids[1:3]
    repo = LecturasRepository(m.conns)
    for campo, valor in (("sensor_id", m.sensor.id), ("envio_id", m.envio.id), ("contenedor_id", m.cont1.id)):
        usados = repo.indice_usado(campo=campo, valor=valor, desde=SALIDA, hasta=None)
        assert f"ix_lectura_{campo.removesuffix('_id')}_fecha" in usados


def test_posiciones_en_orden_y_con_lat_lon(m):
    m.leer(0)
    m.leer(60)
    pos = m.mon.posiciones(m.ops.sesion_id, m.envio.id)
    assert [(p.latitud, p.longitud) for p in pos] == [(-34.6, -58.4)] * 2
    assert pos[0].fecha_medicion < pos[1].fecha_medicion


def test_condiciones_solo_antes_de_salir_y_validadas(m):
    s = m.ops.sesion_id
    assert m.mon.condiciones(s, m.envio.id).temperatura_max_c == 8
    with pytest.raises(BusinessRuleError):  # el envío ya está en tránsito (SUPUESTO M6)
        m.mon.definir_condiciones(s, m.envio.id, CondicionDatos(temperatura_min_c="0"))
    otro = m.nuevo_envio()
    with pytest.raises(ValidationError):
        m.mon.definir_condiciones(s, otro.id, CondicionDatos(temperatura_min_c="9", temperatura_max_c="1"))
