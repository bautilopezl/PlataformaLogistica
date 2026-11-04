"""Dos instancias Qt y motores reales en bases nuevas y exclusivas de esta prueba."""

from __future__ import annotations

import importlib.util
import os
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
import pytest
from psycopg import sql
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

from logistica.core.config import SecurityConfig, load_config
from logistica.core.errors import DatabaseUnavailableError, SessionExpiredError
from logistica.domain.comunicacion import MensajeDTO, VistaChatDTO, techo_milisegundo
from logistica.repositories.connections import Connections
from logistica.services.acceso_service import AccesoService
from logistica.services.autorizacion import Autorizador
from logistica.services.comunicacion_service import ComunicacionService
from logistica.ui.chat_view import ChatView, indice_dato
from logistica.ui.widgets import FormularioDialog
from tests.test_fase1_acceso import CLAVE, Entorno

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="requiere LOGISTICA_TEST_CONNECTIONS=1 y motores reales",
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def esperar(qapp, condicion: Callable[[], bool], segundos: float = 10) -> bool:
    limite = time.monotonic() + segundos
    while not condicion() and time.monotonic() < limite:
        qapp.processEvents()
        time.sleep(0.005)
    return condicion()


@pytest.fixture(scope="module")
def aislado():
    """No reutiliza identidades ni datos existentes; elimina sólo las bases UUID recién creadas."""
    cfg = load_config()
    nombre = "test_chat_" + uuid.uuid4().hex
    pg = cfg.postgres_admin
    admin = psycopg.connect(
        host=pg.host, port=pg.port, dbname=pg.database, user=pg.user, password=pg.password,
        autocommit=True,
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(nombre)))
    config = replace(
        cfg, postgres_app=replace(cfg.postgres_app, database=nombre),
        postgres_admin=replace(cfg.postgres_admin, database=nombre),
        mongo=replace(cfg.mongo, database=nombre),
    )
    c1, c2 = Connections(config), Connections(config)
    try:
        with psycopg.connect(
            host=pg.host, port=pg.port, dbname=nombre, user=pg.user, password=pg.password
        ) as ddl:
            for archivo in sorted((ROOT / "db" / "postgres").glob("*.sql")):
                ddl.execute(archivo.read_text(encoding="utf-8"))
        spec = importlib.util.spec_from_file_location(
            "mongo_chat_test", ROOT / "db" / "mongo" / "setup_mongo.py"
        )
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        modulo.apply(c1.mongo_db)
        entorno = Entorno(c1)
        aut2 = Autorizador(c2)
        acceso2 = AccesoService(c2, entorno.hasher, SecurityConfig(), uuid.uuid4(), aut2)
        p2 = acceso2.login(entorno.emails["OPERACIONES"], CLAVE, "segunda instancia aislada")
        yield entorno, c2, p2, ComunicacionService(c1, entorno.aut), ComunicacionService(c2, aut2)
    finally:
        c1.mongo_db.client.drop_database(nombre)
        c1.close()
        c2.close()
        admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(nombre)))
        admin.close()


def cerrar(qapp, *vistas):
    for vista in vistas:
        vista.cerrar()
    assert esperar(qapp, lambda: all(not v.trabajadores_activos() for v in vistas))
    for vista in vistas:
        vista.close()
        vista.deleteLater()
    qapp.processEvents()


def seleccionar(qapp, vista, ident):
    vista.recargar()
    assert esperar(qapp, lambda: not vista.ocupado())
    indice = indice_dato(vista.selector, ident)
    assert indice > 0
    vista.selector.setCurrentIndex(indice)
    assert esperar(qapp, lambda: not vista.ocupado())


def formulario_automatico(qapp, valores: dict[str, Any]):
    timer = QTimer()
    timer.setInterval(5)

    def completar():
        modal = qapp.activeModalWidget()
        if isinstance(modal, FormularioDialog):
            timer.stop()
            for clave, valor in valores.items():
                widget = modal.widget(clave)
                if hasattr(widget, "setText"):
                    widget.setText(valor)
                else:
                    widget.setCurrentIndex(indice_dato(widget, valor))
            modal.accept()

    timer.timeout.connect(completar)
    timer.start()
    return timer


@pytest.mark.parametrize("tipo", ["PRIVADA", "GRUPAL", "PUBLICA"])
def test_chat_desde_dos_ventanas_con_sesiones_y_conexiones_independientes(qapp, aislado, tipo):
    e, c2, p2, s1, s2 = aislado
    a, b = ChatView(s1, e.admin), ChatView(s2, p2)
    a.show()
    b.show()
    try:
        assert c2 is not e.conns and c2.engine is not e.conns.engine
        with e.conns.pg_session() as tx:
            from sqlalchemy import text

            instancias = tx.execute(text(
                "SELECT instancia_id FROM sesion WHERE id IN (:a,:b)"
            ), {"a": e.admin.sesion_id, "b": p2.sesion_id}).scalars().all()
        assert len(set(instancias)) == 2
        a.tipo.setCurrentText(tipo)
        valores = {"nombre": "DEMO-CHAT " + tipo}
        if tipo == "PRIVADA":
            valores["usuario"] = p2.usuario_id
        timer = formulario_automatico(qapp, valores)
        a.crear.click()
        assert esperar(qapp, lambda: not a.ocupado() and a.selector.count() > 1)
        timer.stop()
        ident = a.selector.itemData(1)
        seleccionar(qapp, a, ident)
        if tipo == "GRUPAL":
            timer = formulario_automatico(qapp, {"usuario": p2.usuario_id, "rol": "MIEMBRO"})
            a.agregar.click()
            assert esperar(qapp, lambda: not a.ocupado() and any(
                m.usuario_id == p2.usuario_id and m.vigente for m in a._miembros
            ))
            timer.stop()
        elif tipo == "PUBLICA":
            s1.crear(e.admin.sesion_id, "PUBLICA", "DEMO-CHAT Otro canal", ())
            b.recargar()
            assert esperar(qapp, lambda: not b.ocupado() and indice_dato(b.publicas, ident) >= 0)
            b.publicas.setCurrentIndex(indice_dato(b.publicas, ident))
            b.recargar()
            assert esperar(qapp, lambda: not b.ocupado())
            assert b.publicas.currentData() == ident
            b.unirse.click()
            assert esperar(qapp, lambda: not b.ocupado() and indice_dato(b.selector, ident) >= 0)
        seleccionar(qapp, b, ident)
        a.texto.setPlainText("Hola desde ventana A")
        a.enviar.click()
        assert esperar(qapp, lambda: not a.ocupado() and a._pendiente is None)
        b._timer.setInterval(20)
        assert esperar(qapp, lambda: b.mensajes.rowCount() == 1 and b.enviar.isEnabled())
        b.texto.setPlainText("Respuesta de ventana B")
        b.enviar.click()
        assert esperar(qapp, lambda: not b.ocupado() and b._pendiente is None)
        a._timer.setInterval(20)
        assert esperar(qapp, lambda: a.mensajes.rowCount() == 2), (
            a.estado.text(), a.selector.currentData(), b.estado_envio.text()
        )
        assert "Persistencia confirmada" in a.estado_envio.text()
        assert b.mensajes.rowCount() == 2
        assert [a.mensajes.item(i, 2).text() for i in range(2)] == [
            "Hola desde ventana A", "Respuesta de ventana B"
        ]
        assert len({a.mensajes.item(i, 3).text() for i in range(2)}) == 2
    finally:
        cerrar(qapp, a, b)


def test_polling_sin_solapamiento_ni_bloqueo_y_descarte_al_cerrar(qapp, aislado):
    e, _, _, _, _ = aislado
    inicio = threading.Event()
    liberar = threading.Event()

    class Lento:
        llamadas = 0
        simultaneas = 0
        maximo = 0

        def vista(self, *args, **kwargs):
            self.llamadas += 1
            self.simultaneas += 1
            self.maximo = max(self.maximo, self.simultaneas)
            inicio.set()
            assert liberar.wait(5)
            self.simultaneas -= 1
            raise SessionExpiredError("respuesta tardía después del cierre")

    servicio = Lento()
    vista = ChatView(servicio, e.admin)
    vencidas = []
    vista.sesion_vencida.connect(vencidas.append)
    vista.show()
    vista._timer.setInterval(5)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    try:
        vista.recargar()
        assert esperar(qapp, inicio.is_set)
        assert esperar(qapp, lambda: len(ticks) >= 8)
        assert servicio.llamadas == 1
        t0 = time.monotonic()
        vista.cerrar()
        assert time.monotonic() - t0 < 0.1
        assert not vista._timer.isActive()
        liberar.set()
        assert esperar(qapp, lambda: not vista.trabajadores_activos())
        assert servicio.maximo == 1
        assert vista.mensajes.rowCount() == 0
        assert vencidas == []
    finally:
        liberar.set()
        timer.stop()
        cerrar(qapp, vista)


def test_mainwindow_no_destruye_chat_pendiente_antes_de_volver_al_login(qapp, aislado):
    from logistica.ui.main_window import MainWindow
    from tests.test_fase1_ui import servicios

    e, _, _, _, _ = aislado
    inicio, liberar = threading.Event(), threading.Event()

    class Lento:
        def vista(self, *args, **kwargs):
            inicio.set()
            assert liberar.wait(5)
            return VistaChatDTO((), (), None)

    ventana = MainWindow(replace(servicios(), comunicacion=Lento()), e.admin)
    terminadas = []
    ventana.sesion_terminada.connect(terminadas.append)
    ventana.show()
    ventana.tabs.setCurrentWidget(ventana.chat)
    try:
        assert esperar(qapp, inicio.is_set)
        t0 = time.monotonic()
        ventana.terminar("Sesión revocada")
        assert time.monotonic() - t0 < 0.1
        assert terminadas == []
        assert ventana.isVisible()
        ticks = []
        QTimer.singleShot(10, lambda: ticks.append(1))
        assert esperar(qapp, lambda: bool(ticks))
        liberar.set()
        assert esperar(qapp, lambda: terminadas == ["Sesión revocada"])
        assert not ventana.isVisible()
        assert not ventana.chat.trabajadores_activos()
    finally:
        liberar.set()
        assert esperar(qapp, lambda: not ventana.chat.trabajadores_activos())
        ventana.close()
        ventana.deleteLater()


def test_ui_no_confirma_fallo_y_reintenta_mismo_uuid(qapp, aislado, monkeypatch):
    e, _, p2, s1, _ = aislado
    ident = s1.crear(e.admin.sesion_id, "PRIVADA", "", (p2.usuario_id,))
    vista = ChatView(s1, e.admin)
    vista.show()
    guardar = s1._mensajes.guardar

    def fallo(mensaje):
        guardar(mensaje)
        raise DatabaseUnavailableError("PostgreSQL", "confirmación posterior perdida")

    try:
        seleccionar(qapp, vista, ident)
        monkeypatch.setattr(s1._mensajes, "guardar", fallo)
        vista.texto.setPlainText("UUID conservado")
        vista.enviar.click()
        assert esperar(qapp, lambda: not vista.ocupado())
        pendiente = vista._pendiente
        assert pendiente is not None
        assert "NO confirmado" in vista.estado_envio.text()
        assert vista.texto.toPlainText() == "UUID conservado"
        monkeypatch.setattr(s1._mensajes, "guardar", guardar)
        vista.enviar.click()
        assert esperar(qapp, lambda: not vista.ocupado() and vista._pendiente is None)
        assert str(pendiente[1]) in vista.estado_envio.text()
        assert e.conns.mongo_db["mensajes"].count_documents({"conversacion_id": ident}) == 1
    finally:
        cerrar(qapp, vista)


def test_ui_pagina_sin_duplicar_y_no_mueve_corte_de_paginas_antiguas(qapp, aislado):
    e, _, p2, s1, _ = aislado
    ident = s1.crear(e.admin.sesion_id, "PRIVADA", "DEMO-CHAT paginado", (p2.usuario_id,))
    fecha = techo_milisegundo(datetime.now(timezone.utc))
    ids = sorted(uuid.uuid4() for _ in range(101))
    for mensaje_id in ids:
        s1._mensajes.guardar(MensajeDTO(mensaje_id, ident, p2.usuario_id, fecha, "Página estable"))
    time.sleep(0.003)
    vista = ChatView(s1, e.admin)
    vista.show()
    vista._timer.stop()
    try:
        seleccionar(qapp, vista, ident)
        assert vista.mensajes.rowCount() == 50
        recibidos = {vista.mensajes.item(i, 3).text() for i in range(50)}
        corte = vista._pagina.hasta
        vista.siguiente.click()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista._pagina.hasta == corte
        recibidos.update(vista.mensajes.item(i, 3).text() for i in range(vista.mensajes.rowCount()))
        nuevo = s1.enviar(e.admin.sesion_id, ident, uuid.uuid4(), "Nuevo durante consulta")
        vista.siguiente.click()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista.mensajes.rowCount() == 1
        assert vista._pagina.hasta == corte
        recibidos.add(vista.mensajes.item(0, 3).text())
        assert recibidos == {str(m) for m in ids}
        assert str(nuevo.id) not in recibidos
        vista.recargar()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert vista._pagina.hasta == corte
        vista.anterior.click()
        assert esperar(qapp, lambda: not vista.ocupado())
        vista.anterior.click()
        assert esperar(qapp, lambda: not vista.ocupado())
        assert str(nuevo.id) in {
            vista.mensajes.item(i, 3).text() for i in range(vista.mensajes.rowCount())
        }
    finally:
        cerrar(qapp, vista)


@pytest.mark.parametrize("revocacion", ["membresia", "sesion", "usuario", "permiso"])
def test_revocacion_real_desde_otra_instancia_descarta_consulta_pendiente(
    qapp, aislado, monkeypatch, revocacion,
):
    e, c2, p2_original, s1, s2 = aislado
    # Sesión independiente nueva para que una revocación no afecte a las otras pruebas.
    from sqlalchemy import text

    aut = Autorizador(c2)
    acceso = AccesoService(c2, e.hasher, SecurityConfig(), uuid.uuid4(), aut)
    p2 = acceso.login(e.emails["OPERACIONES"], CLAVE, "revocación aislada")
    ident = s1.crear(e.admin.sesion_id, "GRUPAL", "DEMO-CHAT revocación", (p2.usuario_id,))
    s1.enviar(e.admin.sesion_id, ident, uuid.uuid4(), "No entregar después de revocar")
    inicio, liberar = threading.Event(), threading.Event()
    original = s2._mensajes.historial

    def pendiente(*args, **kwargs):
        resultado = original(*args, **kwargs)
        inicio.set()
        assert liberar.wait(5)
        return resultado

    vista = ChatView(s2, p2)
    vista.show()
    try:
        seleccionar(qapp, vista, ident)
        monkeypatch.setattr(s2._mensajes, "historial", pendiente)
        vista.recargar()
        assert esperar(qapp, inicio.is_set)
        if revocacion == "membresia":
            s1.retirar(e.admin.sesion_id, ident, p2.usuario_id)
        else:
            with e.conns.pg_session() as tx:
                if revocacion == "sesion":
                    tx.execute(text(
                        "UPDATE sesion SET estado='REVOCADA',fin_en=clock_timestamp() WHERE id=:id"
                    ), {"id": p2.sesion_id})
                elif revocacion == "usuario":
                    tx.execute(text("UPDATE usuario SET activo=false WHERE id=:id"), {"id": p2.usuario_id})
                else:
                    tx.execute(text("DELETE FROM usuario_rol WHERE usuario_id=:id"), {"id": p2.usuario_id})
        liberar.set()
        assert esperar(qapp, lambda: not vista.trabajadores_activos())
        assert vista.mensajes.rowCount() == 0
        assert vista.participantes.rowCount() == 0
        assert "confirmada" not in vista.estado.text()
    finally:
        liberar.set()
        with e.conns.pg_session() as tx:
            tx.execute(text("UPDATE usuario SET activo=true WHERE id=:id"), {"id": p2.usuario_id})
            tx.execute(text("""
                INSERT INTO usuario_rol(usuario_id,rol_id)
                SELECT :id,id FROM rol WHERE codigo='OPERACIONES' ON CONFLICT DO NOTHING
            """), {"id": p2.usuario_id})
        cerrar(qapp, vista)


def test_dos_procesos_de_aplicacion_intercambian_mensajes_con_sesiones_propias(aislado):
    import json
    import subprocess
    import sys

    e, _, p2, s1, _ = aislado
    ident = s1.crear(e.admin.sesion_id, "PRIVADA", "DEMO-CHAT dos procesos", (p2.usuario_id,))
    codigo = r'''
import json, os, uuid
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from logistica.core.config import load_config
from logistica.repositories.connections import Connections
from logistica.services.contenedor import crear_servicios
from logistica.ui.main_window import MainWindow
from logistica.ui.chat_view import indice_dato
from logistica.ui.gc_guard import GarbageCollectorGuard

app = QApplication([])
guard = GarbageCollectorGuard(app)
cfg = load_config()
conns = Connections(cfg)
instancia = uuid.uuid4()
servicios = crear_servicios(cfg, conns, instancia)
p = servicios.acceso.login(os.environ['CHAT_TEST_EMAIL'], 'clave-de-prueba-123', 'proceso Qt aislado')
ventana = MainWindow(servicios, p)
ventana._timer_sync.stop()
ventana._timer_monitoreo.stop()
ventana.show()
vista = ventana.chat
ventana.tabs.setCurrentWidget(vista)
vista._timer.setInterval(30)
ident = uuid.UUID(os.environ['CHAT_TEST_CONV'])
rol = os.environ['CHAT_TEST_ROL']
enviado = False
terminado = False
timer = QTimer()
def paso():
    global enviado, terminado
    if terminado or vista.ocupado():
        return
    indice = indice_dato(vista.selector, ident)
    if indice < 0:
        vista.recargar()
        return
    if vista.selector.currentData() != ident:
        vista.selector.setCurrentIndex(indice)
        return
    textos = [vista.mensajes.item(i,2).text() for i in range(vista.mensajes.rowCount())]
    if not enviado and (rol == 'A' or 'Proceso A' in textos):
        enviado = True
        vista.texto.setPlainText('Proceso ' + rol)
        vista.enviar.click()
    if enviado and set(textos) == {'Proceso A', 'Proceso B'} and vista._pendiente is None:
        terminado = True
        timer.stop()
        print(json.dumps({'usuario': str(p.usuario_id), 'sesion': str(p.sesion_id),
                          'instancia': str(instancia), 'mensajes': textos}), flush=True)
        ventana.close()
def timeout():
    if not terminado:
        print('ERROR: intercambio entre procesos no completado', flush=True)
        ventana.close()
        app.exit(2)
timer.timeout.connect(paso)
timer.start(20)
QTimer.singleShot(20000, timeout)
try:
    resultado = app.exec()
finally:
    conns.close()
raise SystemExit(resultado)
'''
    procesos = []
    try:
        for rol, email in (("A", e.admin_email), ("B", e.emails["OPERACIONES"])):
            env = dict(os.environ)
            env.update({
                "PG_DB": e.conns._config.postgres_app.database,
                "MONGO_DB": e.conns._config.mongo.database,
                "PYTHONPATH": str(ROOT / "src"),
                "QT_QPA_PLATFORM": "offscreen",
                "CHAT_TEST_EMAIL": email, "CHAT_TEST_CONV": str(ident), "CHAT_TEST_ROL": rol,
            })
            procesos.append(subprocess.Popen(
                [sys.executable, "-c", codigo], cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        resultados = []
        for proceso in procesos:
            salida, error = proceso.communicate(timeout=35)
            assert proceso.returncode == 0, salida + error
            resultados.append(json.loads(salida.strip().splitlines()[-1]))
        assert len({r["usuario"] for r in resultados}) == 2
        assert len({r["sesion"] for r in resultados}) == 2
        assert len({r["instancia"] for r in resultados}) == 2
        assert all(r["mensajes"] == ["Proceso A", "Proceso B"] for r in resultados)
        assert e.conns.mongo_db["mensajes"].count_documents({"conversacion_id": ident}) == 2
    finally:
        for proceso in procesos:
            if proceso.poll() is None:
                proceso.kill()
                proceso.communicate()
