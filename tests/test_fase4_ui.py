"""Circuito Qt offscreen: filtros, paginación, permisos, acciones y mapa vectorial."""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication

from logistica.core.errors import DatabaseUnavailableError, SessionExpiredError
from logistica.domain.monitoreo_dtos import CondicionDatos, LecturaDTO, PaginaLecturas, PosicionDTO
from logistica.services.monitoreo_service import ResultadoEvaluacion
from logistica.ui.main_window import MainWindow
from logistica.ui.mapa_gps import MapaGPS
from logistica.ui.monitoreo_view import MonitoreoView
from tests.test_fase1_ui import _permisos_sembrados, esperar, principal, qapp, servicios  # noqa: F401

T0 = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


class Monitor:
    def __init__(self):
        self.eid, self.sid, self.aid, self.iid = [uuid.uuid4() for _ in range(4)]
        self.llamadas = []
        self.hilos = []
        self.estado_alerta, self.estado_incidente = "ABIERTA", "ABIERTO"
        self.datos = [LecturaDTO(
            uuid.uuid4(), self.sid, T0 + timedelta(seconds=i), T0 + timedelta(seconds=i),
            Decimal("5"), None, None, None, -34.6, -58.4, "SIMULADOR", "PROCESADA") for i in range(115)]

    def _hilo(self):
        self.hilos.append(QThread.currentThread() == QApplication.instance().thread())

    def listar(self, sesion_id):
        self._hilo()
        return [SimpleNamespace(id=self.eid, codigo="ENV-DEMO", estado="BORRADOR", descripcion_carga="Demo")]

    def estado_sensores(self, sesion_id, **kwargs):
        self._hilo()
        return []

    def alertas(self, sesion_id, **kwargs):
        return [SimpleNamespace(
            id=self.aid, envio_id=self.eid, envio_codigo="ENV-DEMO", sensor_codigo="SENSOR",
            tipo="TEMPERATURA", severidad="MEDIA", estado=self.estado_alerta,
            primera_deteccion_en=T0, ultima_deteccion_en=T0, cierre_tipo=None,
            motivo_cierre=None, incidente_id=None)]

    def incidentes(self, sesion_id, **kwargs):
        return [SimpleNamespace(
            id=self.iid, envio_id=self.eid, envio_codigo="ENV-DEMO", tipo="OTRO", severidad="MEDIA",
            estado=self.estado_incidente, descripcion="Demo", responsable_email=None, ocurrido_en=T0, resolucion=None)]

    def ultimas_lecturas(self, sesion_id, envio_id):
        return self.datos[-20:][::-1]

    def posiciones(self, sesion_id, envio_id):
        return [PosicionDTO(T0, -34.60, -58.38, self.sid),
                PosicionDTO(T0 + timedelta(minutes=1), -32.95, -60.64, self.sid)]

    def lecturas(self, sesion_id, **kwargs):
        self._hilo()
        self.llamadas.append(("lecturas", kwargs))
        datos = self.datos
        if kwargs["despues"]:
            datos = [l for l in datos if l.fecha_medicion > kwargs["despues"][0]]
        if kwargs["desde"]:
            datos = [l for l in datos if l.fecha_medicion >= kwargs["desde"]]
        if kwargs["hasta"]:
            datos = [l for l in datos if l.fecha_medicion < kwargs["hasta"]]
        pagina = datos[:kwargs["limite"]]
        return PaginaLecturas(pagina, (pagina[-1].fecha_medicion, pagina[-1].id) if len(datos) > 50 else None)

    def condiciones(self, sesion_id, envio_id):
        return CondicionDatos(temperatura_min_c="2", temperatura_max_c="8")

    def definir_condiciones(self, sesion_id, envio_id, datos):
        self._hilo()
        self.llamadas.append(("condiciones", envio_id, datos))

    def reconocer_alerta(self, sesion_id, alerta_id):
        self._hilo()
        self.llamadas.append(("reconocer", alerta_id))
        self.estado_alerta = "RECONOCIDA"

    def cerrar_alerta(self, sesion_id, alerta_id, motivo):
        self.llamadas.append(("cerrar", alerta_id, motivo))
        self.estado_alerta = "CERRADA"

    def crear_incidente(self, sesion_id, envio_id, **kwargs):
        self.llamadas.append(("crear_incidente", envio_id, kwargs))

    def tomar_incidente(self, sesion_id, incidente_id):
        self.llamadas.append(("tomar", incidente_id))
        self.estado_incidente = "EN_GESTION"

    def resolver_incidente(self, sesion_id, incidente_id, resolucion):
        self.llamadas.append(("resolver", incidente_id, resolucion))
        self.estado_incidente = "RESUELTO"

    def procesar_ciclo(self, sesion_id, detener):
        self._hilo()
        return ResultadoEvaluacion(evaluadas=1)


def cargar(qapp, rol="ADMINISTRADOR"):
    mon = Monitor()
    p = principal({rol}, _permisos_sembrados(rol), uuid.uuid4() if rol in ("CLIENTE", "OPERADOR_LOGISTICO") else None)
    vista = MonitoreoView(mon, mon, p)
    vista.recargar()
    assert esperar(qapp, lambda: not vista.ocupado())
    vista.envio.setCurrentIndex(1)
    assert esperar(qapp, lambda: not vista.ocupado())
    return mon, vista


def test_historial_filtrable_paginado_y_ultimas_mediciones(qapp):
    mon, vista = cargar(qapp)
    try:
        assert vista.ultimas.rowCount() == 20
        assert vista.ultimas.item(0, 0).text()
        vista.filtrar()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista.historial.rowCount() == 50 and vista.siguiente.isEnabled()
        vista.pagina_siguiente()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista.pagina.text().startswith("Página 2")
        vista.pagina_siguiente()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista.historial.rowCount() == 15 and not vista.siguiente.isEnabled()
        vista.pagina_anterior()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista.historial.rowCount() == 50
        vista.desde.setText(T0.isoformat())
        vista.hasta.setText((T0 + timedelta(seconds=2)).isoformat())
        vista.filtrar()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista.historial.rowCount() == 2
        # UUID deserializado en otra consulta: no depender de identidad de objetos QVariant.
        vista.seleccionar_envio(uuid.UUID(str(mon.eid)))
        assert vista.envio.currentData() == mon.eid
        vista.recargar()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista.envio.currentData() == mon.eid
        assert not any(mon.hilos)  # ni consultas ni escritura en el hilo gráfico
        llamadas = len(mon.llamadas)
        vista.desde.setText("fecha inválida")
        vista.filtrar()
        assert "Filtro inválido" in vista.estado.text() and len(mon.llamadas) == llamadas
    finally:
        vista.cerrar()


@pytest.mark.parametrize("rol,gestiona,reconoce", [
    ("ADMINISTRADOR", True, True), ("OPERACIONES", True, True),
    ("CLIENTE", False, False), ("OPERADOR_LOGISTICO", False, True),
])
def test_botones_y_filtros_por_perfil(qapp, rol, gestiona, reconoce):
    mon, vista = cargar(qapp, rol)
    try:
        vista.alertas.selectRow(0)
        assert vista.boton_condiciones.isHidden() != gestiona
        assert vista.cerrar_alerta.isHidden() != gestiona
        assert vista.reconocer.isHidden() != reconoce
        assert vista.campo.count() == (3 if gestiona else 1)
        assert vista.boton_demo.isHidden() == (rol != "ADMINISTRADOR")
        assert vista.mapa.trayectos[mon.sid][-1].latitud == -32.95
    finally:
        vista.cerrar()


def test_acciones_de_alerta_incidente_y_condiciones_llaman_servicios(qapp, monkeypatch):
    mon, vista = cargar(qapp)
    respuestas = iter([
        CondicionDatos(temperatura_min_c="1", temperatura_max_c="9").__dict__,
        {"motivo": "Inspección"},
        {"tipo": "VIBRACION", "severidad": "MEDIA", "descripcion": "Impacto"},
        {"resolucion": "Carga inspeccionada"},
    ])
    monkeypatch.setattr("logistica.ui.monitoreo_view.pedir", lambda *args: next(respuestas))
    try:
        vista.condiciones()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert ("condiciones", mon.eid, CondicionDatos(temperatura_min_c="1", temperatura_max_c="9")) in mon.llamadas
        vista.alertas.selectRow(0)
        vista._reconocer()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert ("reconocer", mon.aid) in mon.llamadas
        vista.alertas.selectRow(0)
        vista._cerrar_alerta()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert ("cerrar", mon.aid, "Inspección") in mon.llamadas
        vista.alertas.selectRow(0)
        vista._incidente_desde_alerta()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert any(l[0] == "crear_incidente" and l[2]["alerta_id"] == mon.aid for l in mon.llamadas)
        vista.incidentes.selectRow(0)
        vista._tomar()
        assert esperar(qapp, lambda: not vista.ocupado())
        vista.incidentes.selectRow(0)
        vista._resolver()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert ("resolver", mon.iid, "Carga inspeccionada") in mon.llamadas
        assert not any(mon.hilos)
    finally:
        vista.cerrar()


def test_mapa_nativo_con_base_local_recorridos_separados_y_render(qapp):
    mapa = MapaGPS()
    sid, otro = uuid.uuid4(), uuid.uuid4()
    puntos = [PosicionDTO(T0, -34.60, -58.38, sid),
              PosicionDTO(T0 + timedelta(minutes=1), -32.95, -60.64, sid),
              PosicionDTO(T0, 40.42, -3.70, otro)]
    mapa.resize(900, 400)
    mapa.mostrar(puntos)
    mapa.show()
    qapp.processEvents()
    assert mapa.error_recurso == ""
    assert len(mapa.trayectos[sid]) == 2 and len(mapa.trayectos[otro]) == 1
    assert mapa.proyectar(-34.60, -58.38).x() == -5838
    assert not mapa.grab().isNull()
    mapa.mostrar([])
    assert mapa.trayectos == {}
    mapa.close()


def test_procesamiento_en_segundo_plano_no_bloquea_y_cierra_ordenado(qapp):
    entro, salio = threading.Event(), threading.Event()
    mon = Monitor()

    def lento(sesion_id, detener):
        assert QThread.currentThread() != qapp.thread()
        entro.set()
        detener.wait(5)
        salio.set()
        return ResultadoEvaluacion()

    mon.procesar_ciclo = lento
    ss = servicios()
    object.__setattr__(ss, "monitoreo", mon)
    ventana = MainWindow(ss, principal({"OPERACIONES"}, _permisos_sembrados("OPERACIONES")))
    ventana.show()
    ventana.procesar_monitoreo()
    assert esperar(qapp, entro.is_set)
    latidos = []
    QTimer.singleShot(0, lambda: latidos.append(True))
    assert esperar(qapp, lambda: latidos)
    ventana.procesar_monitoreo()  # no se solapan vueltas en una instancia
    assert ventana._procesando_monitoreo
    ventana.close()
    assert salio.is_set()
    assert not ventana._timer_monitoreo.isActive()
    assert ventana._pool.wait_for_done(100)


@pytest.mark.parametrize("error,expira", [(DatabaseUnavailableError("MongoDB", "caído"), False),
                                        (SessionExpiredError(), True)])
def test_error_del_proceso_visible_y_revocacion_cierra(qapp, error, expira):
    mon = Monitor()

    def fallar(*args):
        raise error

    mon.procesar_ciclo = fallar
    ss = servicios()
    object.__setattr__(ss, "monitoreo", mon)
    ventana = MainWindow(ss, principal({"OPERACIONES"}, _permisos_sembrados("OPERACIONES")))
    motivos = []
    ventana.sesion_terminada.connect(motivos.append)
    ventana.procesar_monitoreo()
    assert esperar(qapp, lambda: bool(motivos) if expira else "pendiente" in ventana.estado_monitoreo.text())
    if expira:
        assert not ventana._timer_monitoreo.isActive()
    ventana.close()


@pytest.mark.skipif(os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1", reason="requiere los motores locales")
def test_pantalla_monitoreo_con_servicios_reales_y_motor_mongo(qapp, tx_connections):
    from tests.test_fase4_monitoreo import Monitoreo

    entorno = Monitoreo(tx_connections)
    try:
        for segundo in (0, 60, 120, 180, 240, 300):
            entorno.leer(segundo, temperatura_c=12)
        vista = MonitoreoView(entorno.mon, entorno.env, entorno.ops)
        vista.recargar()
        assert esperar(qapp, lambda: not vista.ocupado())
        vista.seleccionar_envio(entorno.envio.id)
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista.envio.currentData() == entorno.envio.id, vista.estado.text()
        vista.filtrar()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista.historial.rowCount() == 6 and vista.ultimas.rowCount() == 6, vista.estado.text()
        assert vista.alertas.item(0, 3).text() == "ALTA" and vista.incidentes.rowCount() == 1
        assert len(vista.mapa.trayectos[entorno.sensor.id]) == 6
        assert not vista.boton_condiciones.isEnabled()
        vista.cerrar()
    finally:
        tx_connections.mongo_db["lecturas_sensor"].delete_many({"sensor_id": {"$in": entorno.sensores}})
        tx_connections.mongo_db["eventos_logisticos"].delete_many({"envio_id": entorno.envio.id})


@pytest.mark.skipif(os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1", reason="requiere los motores locales")
def test_boton_demo_crea_escenario_real_aislado_y_lo_selecciona(qapp, tx_connections, monkeypatch):
    from tests.test_fase4_monitoreo import Monitoreo

    entorno = Monitoreo(tx_connections)
    vista = MonitoreoView(entorno.mon, entorno.env, entorno.admin)
    demo_id = None
    monkeypatch.setattr("logistica.ui.monitoreo_view.pedir",
                        lambda *args: {"confirmacion": "DEMO", "escenario": "completo"})
    try:
        vista.preparar_demo()
        assert esperar(qapp, lambda: not vista.ocupado(), segundos=20)
        demo_id = vista.envio.currentData()
        assert demo_id is not None and demo_id != entorno.envio.id, vista.estado.text()
        assert vista.ultimas.rowCount() == 12 and vista.incidentes.rowCount() == 1
        assert vista.alertas.rowCount() == 2  # cerradas, visibles al preparar la demo
        assert "SIMULADOR" in vista.envio.currentText()
        assert not vista.solo_activas.isChecked()
        assert sum(len(p) for p in vista.mapa.trayectos.values()) == 12
    finally:
        vista.cerrar()
        if demo_id is not None:
            tx_connections.mongo_db["lecturas_sensor"].delete_many({"envio_id": demo_id})
            tx_connections.mongo_db["eventos_logisticos"].delete_many({"envio_id": demo_id})
