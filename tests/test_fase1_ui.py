"""Fase 1 — UI de acceso (offscreen). Las primeras usan servicios simulados; la última, servicios reales."""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from logistica.core.errors import (  # noqa: E402
    AuthenticationError,
    PermissionDeniedError,
    SessionExpiredError,
)
from logistica.domain.acceso import Principal, TerceroDatos, TerceroDTO  # noqa: E402
from logistica.services.contenedor import Servicios  # noqa: E402
from logistica.services.health_service import EngineStatus  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def esperar(qapp, condicion, segundos=10.0):
    limite = time.monotonic() + segundos
    while not condicion() and time.monotonic() < limite:
        qapp.processEvents()
        time.sleep(0.01)
    return condicion()


def principal(roles, permisos, tercero_id=None):
    return Principal(
        uuid.uuid4(),
        uuid.uuid4(),
        "u@test.local",
        "Usuario Prueba",
        tercero_id,
        frozenset(roles),
        frozenset(permisos),
    )


class FakeSalud:
    def check_all(self):
        return [EngineStatus(e, True, "", 1) for e in ("PostgreSQL", "MongoDB", "Neo4j")]


class FakeAcceso:
    def __init__(self, resultado_login=None, error=None):
        self.resultado_login, self.error, self.llamadas = resultado_login, error, []

    def login(self, email, password, cliente):
        self.llamadas.append(email)
        if self.error:
            raise self.error
        return self.resultado_login

    def logout(self, sesion_id):
        self.llamadas.append(("logout", sesion_id))

    def listar_sesiones(self, sesion_id, todas=False, solo_abiertas=True):
        return []


class FakeTerceros:
    def __init__(self, error=None):
        self.error = error

    def listar(self, sesion_id, **filtros):
        if self.error:
            raise self.error
        return [
            TerceroDTO(
                uuid.uuid4(), "ACME SA", "AR", "CUIT", "30-1", None, None, None, True, frozenset({"CLIENTE"})
            )
        ]


class FakeSincronizacion:
    def __init__(self):
        self.vueltas = 0

    def procesar_todo(self):
        self.vueltas += 1

    def resumen(self):
        return {"NEO4J_LOGISTICA": {"PENDIENTE": 2, "FALLIDA": 1}, "MONGO_EVENTOS": {"PENDIENTE": 0}}


class FakeEnvios:
    def listar(self, sesion_id, **filtros):
        return []


class FakeTrazabilidad:
    pass


class FakeComunicacion:
    def vista(self, sesion, ident=None, **kwargs):
        from logistica.domain.comunicacion import VistaChatDTO

        return VistaChatDTO((), (), None)


class FakeMonitoreo:
    def procesar_ciclo(self, sesion_id, detener):
        from logistica.services.monitoreo_service import ResultadoEvaluacion

        return ResultadoEvaluacion(), 0


class FakeAuditoria:
    """Devuelve `total` registros de a páginas, como el servicio real (más recientes primero)."""

    def __init__(self, total=0):
        from datetime import datetime, timedelta, timezone

        from logistica.domain.auditoria import AuditoriaDTO

        base = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        self.registros = [
            AuditoriaDTO(
                uuid.uuid4(),
                base - timedelta(minutes=i),
                "crear_tercero",
                "EXITO",
                uuid.uuid4(),
                "admin@test.local",
                "tercero",
                uuid.uuid4(),
                {"roles": ["CLIENTE"]},
            )
            for i in range(total)
        ]
        self.consultas = []

    def acciones(self, sesion_id):
        return ["crear_tercero", "login"]

    def listar(self, sesion_id, **filtros):
        from logistica.domain.auditoria import PaginaAuditoria

        self.consultas.append(filtros)
        o, n = filtros["offset"], filtros["limite"]
        return PaginaAuditoria(self.registros[o : o + n], hay_mas=len(self.registros) > o + n)


def servicios(acceso=None, terceros=None, auditoria=None) -> Servicios:
    return Servicios(
        salud=FakeSalud(),
        acceso=acceso or FakeAcceso(),
        usuarios=object(),
        terceros=terceros or FakeTerceros(),
        auditoria=auditoria or FakeAuditoria(),
        catalogo=object(),
        envios=FakeEnvios(),
        sincronizacion=FakeSincronizacion(),
        monitoreo=FakeMonitoreo(),
        trazabilidad=FakeTrazabilidad(),
        comunicacion=FakeComunicacion(),
    )


# --------------------------------------------------------------------------- login
def test_login_valida_campos_vacios_sin_llamar_al_servicio(qapp):
    from logistica.ui.login_dialog import LoginDialog

    acceso = FakeAcceso()
    dialogo = LoginDialog(acceso, "pruebas")
    dialogo.ingresar()
    assert acceso.llamadas == []
    assert "Complete" in dialogo.mensaje.text()


def test_login_fallido_muestra_el_mensaje_del_servicio(qapp):
    from logistica.ui.login_dialog import LoginDialog

    dialogo = LoginDialog(
        FakeAcceso(error=AuthenticationError(user_message="Email o contraseña incorrectos.")), "pruebas"
    )
    dialogo.email.setText("a@b.com")
    dialogo.password.setText("x")
    dialogo.ingresar()
    assert esperar(qapp, lambda: dialogo.boton.isEnabled())
    assert dialogo.mensaje.text() == "Email o contraseña incorrectos."
    assert dialogo.password.text() == ""  # la contraseña no queda en el formulario


def test_login_exitoso_acepta_el_dialogo(qapp):
    from PySide6.QtWidgets import QDialog

    from logistica.ui.login_dialog import LoginDialog

    p = principal({"ADMINISTRADOR"}, {"usuarios.administrar"})
    dialogo = LoginDialog(FakeAcceso(resultado_login=p), "pruebas")
    dialogo.email.setText("a@b.com")
    dialogo.password.setText("x")
    dialogo.ingresar()
    assert esperar(qapp, lambda: dialogo.result() == QDialog.DialogCode.Accepted)
    assert dialogo.principal == p


# --------------------------------------------------------------------------- pestañas por perfil
def _permisos_sembrados(rol: str) -> frozenset[str]:
    """Permisos reales del rol según db/postgres/090_seed_roles_permisos.sql (ADMINISTRADOR: todos)."""
    import re

    sql = (ROOT / "db" / "postgres" / "090_seed_roles_permisos.sql").read_text(encoding="utf-8")
    if rol == "ADMINISTRADOR":
        return frozenset(re.findall(r"gen_random_uuid\(\), '([a-z]+\.[a-z]+)'", sql))
    return frozenset(p for r, p in re.findall(r"\('([A-Z_]+)', '([a-z]+\.[a-z]+)'\)", sql) if r == rol)


@pytest.mark.parametrize(
    "rol, externo, esperadas",
    [
        (
            "ADMINISTRADOR",
            False,
            [
                "Estado",
                "Envíos",
                "Trazabilidad",
                "Catálogos",
                "Monitoreo",
                "Terceros",
                "Usuarios",
                "Sesiones",
                "Auditoría",
            ],
        ),
        (
            "OPERACIONES",
            False,
            ["Estado", "Envíos", "Trazabilidad", "Catálogos", "Monitoreo", "Terceros", "Sesiones"],
        ),
        ("COMERCIAL", False, ["Estado", "Envíos", "Trazabilidad", "Terceros", "Sesiones"]),
        ("CLIENTE", True, ["Estado", "Envíos", "Trazabilidad", "Monitoreo", "Sesiones"]),
        ("OPERADOR_LOGISTICO", True, ["Estado", "Envíos", "Trazabilidad", "Monitoreo", "Sesiones"]),
    ],
)
def test_cada_perfil_ve_solo_sus_pestanias(qapp, rol, externo, esperadas):
    from logistica.ui.main_window import MainWindow

    p = principal({rol}, _permisos_sembrados(rol), tercero_id=uuid.uuid4() if externo else None)
    ventana = MainWindow(servicios(), p)
    assert ventana.titulos_pestanias() == [esperadas[0], "Chat", *esperadas[1:]]
    ventana.close()


def test_operaciones_consulta_terceros_pero_no_ve_botones_de_gestion(qapp):
    from logistica.ui.main_window import MainWindow
    from logistica.ui.terceros_view import TercerosView

    ventana = MainWindow(servicios(), principal({"OPERACIONES"}, {"envios.crear"}))
    vista = next(v for v in ventana.vistas if isinstance(v, TercerosView))
    assert vista.boton_nuevo.isHidden() and vista.boton_editar.isHidden()
    ventana.close()


# --------------------------------------------------------------------------- sesión vencida y errores
def test_sesion_vencida_en_una_pantalla_vuelve_al_login(qapp):
    from logistica.ui.main_window import MainWindow
    from logistica.ui.terceros_view import TercerosView

    ventana = MainWindow(
        servicios(terceros=FakeTerceros(error=SessionExpiredError())),
        principal({"COMERCIAL"}, {"terceros.gestionar"}),
    )
    motivos = []
    ventana.sesion_terminada.connect(motivos.append)
    ventana.show()
    vista = next(v for v in ventana.vistas if isinstance(v, TercerosView))
    vista.recargar()
    assert esperar(qapp, lambda: motivos)
    assert "sesión" in motivos[0].lower()
    assert ventana.isHidden()


def test_permiso_denegado_se_muestra_y_la_pantalla_sigue_usable(qapp):
    from logistica.ui.terceros_view import TercerosView

    vista = TercerosView(FakeTerceros(error=PermissionDeniedError()), uuid.uuid4(), True)
    vencidas = []
    vista.sesion_vencida.connect(vencidas.append)
    vista.recargar()
    assert esperar(qapp, lambda: not vista.ocupado())
    assert "permiso" in vista.estado.text().lower()
    assert vista.boton_buscar.isEnabled() and not vencidas


def test_cerrar_la_ventana_cierra_la_sesion_y_pide_salir(qapp):
    from logistica.ui.main_window import MainWindow

    acceso = FakeAcceso()
    p = principal({"COMERCIAL"}, {"terceros.gestionar"})
    ventana = MainWindow(servicios(acceso=acceso), p)
    salidas = []
    ventana.salir.connect(lambda: salidas.append(True))
    ventana.show()
    ventana.close()
    assert ("logout", p.sesion_id) in acceso.llamadas
    assert salidas == [True]


def test_los_filtros_se_leen_en_el_hilo_grafico(qapp):
    """Los valores de los widgets se capturan al hacer clic, no cuando corre el trabajador."""
    import threading

    from logistica.ui.terceros_view import TercerosView

    vistos = []

    class Espia(FakeTerceros):
        def listar(self, sesion_id, **filtros):
            vistos.append((threading.current_thread() is threading.main_thread(), filtros["texto"]))
            return []

    vista = TercerosView(Espia(), uuid.uuid4(), True)
    vista.buscar.setText("acme")
    vista.recargar()
    vista.buscar.setText("cambiado después")  # no debe afectar a la búsqueda ya lanzada
    assert esperar(qapp, lambda: not vista.ocupado())
    assert vistos == [(False, "acme")]


def test_cancelar_el_login_en_curso_no_deja_una_sesion_abierta(qapp):
    from PySide6.QtWidgets import QDialog

    from logistica.ui.login_dialog import LoginDialog

    class Lento(FakeAcceso):
        def login(self, email, password, cliente):
            time.sleep(0.4)
            return super().login(email, password, cliente)

    p = principal({"COMERCIAL"}, {"terceros.gestionar"})
    acceso = Lento(resultado_login=p)
    dialogo = LoginDialog(acceso, "pruebas")
    dialogo.show()
    dialogo.email.setText("a@b.com")
    dialogo.password.setText("x")
    dialogo.ingresar()
    dialogo.reject()  # Esc mientras verifica: se ignora hasta que termine
    assert dialogo.isVisible()
    assert esperar(qapp, lambda: dialogo.result() == QDialog.DialogCode.Accepted)
    assert dialogo.principal == p


def test_si_falla_el_logout_la_ventana_sigue_y_avisa(qapp):
    from logistica.core.errors import DatabaseUnavailableError
    from logistica.ui.main_window import MainWindow

    class SinBase(FakeAcceso):
        def logout(self, sesion_id):
            raise DatabaseUnavailableError("PostgreSQL")

    ventana = MainWindow(servicios(acceso=SinBase()), principal({"COMERCIAL"}, {"terceros.gestionar"}))
    motivos = []
    ventana.sesion_terminada.connect(motivos.append)
    ventana.show()
    ventana.cerrar_sesion()
    assert esperar(qapp, lambda: "PostgreSQL" in ventana.statusBar().currentMessage())
    assert motivos == [] and ventana.isVisible()
    ventana.hide()


def test_si_el_relogin_falla_la_aplicacion_termina(qapp):
    from logistica.ui.app_controller import AppController

    class AppFalsa:
        def __init__(self):
            self.salio = False

        def setQuitOnLastWindowClosed(self, _valor):
            pass

        def quit(self):
            self.salio = True

    app = AppFalsa()
    controller = AppController(app, servicios(), "pruebas")

    def explota(_mensaje=""):
        raise RuntimeError("falla al construir la ventana")

    controller.iniciar = explota
    controller._relogin("Sesión cerrada.")
    assert app.salio  # sin ventana visible, la app no puede quedar viva


def test_la_basura_ciclica_se_recolecta_en_el_hilo_grafico(qapp):
    import gc

    from logistica.ui.gc_guard import GarbageCollectorGuard

    estaba = gc.isenabled()
    guard = GarbageCollectorGuard(intervalo_ms=10)
    try:
        assert not gc.isenabled()  # nada de recolección automática en hilos trabajadores

        class Nodo:
            pass

        for _ in range(gc.get_threshold()[0] * 2):  # basura cíclica suficiente para superar el umbral
            a, b = Nodo(), Nodo()
            a.otro, b.otro = b, a
        antes = gc.get_count()[0]
        assert esperar(qapp, lambda: gc.get_count()[0] < antes)  # el timer recolectó
    finally:
        guard.detener()
        if not estaba:
            gc.disable()


def test_formulario_de_tercero_valida_antes_de_enviar(qapp):
    from logistica.ui.terceros_view import TerceroDialog

    dialogo = TerceroDialog(None)
    dialogo.razon.setText("ACME")
    dialogo.numero.setText("30-1")
    dialogo.pais.setText("A1")  # el campo admite 2 caracteres; "A1" no es un código ISO
    dialogo._aceptar()
    assert "ISO" in dialogo.error.text()
    dialogo.pais.setText("AR")
    dialogo._aceptar()
    assert "rol comercial" in dialogo.error.text()


# ------------------------------------------------------------- de punta a punta (servicios reales)
@pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y PostgreSQL levantado)",
)
def test_de_punta_a_punta_login_y_terceros_desde_la_ui(qapp, tx_connections):
    from PySide6.QtWidgets import QDialog

    from logistica.core.config import SecurityConfig
    from logistica.domain.acceso import UsuarioDatos
    from logistica.domain.passwords import Hasher
    from logistica.repositories.postgres import unit_of_work
    from logistica.repositories.postgres.usuarios import UsuarioRepository
    from logistica.services.acceso_service import AccesoService
    from logistica.services.auditoria_service import AuditoriaService
    from logistica.services.catalogo_service import CatalogoService
    from logistica.services.comunicacion_service import ComunicacionService
    from logistica.services.envio_service import EnvioService
    from logistica.services.monitoreo_service import MonitoreoService
    from logistica.services.sincronizacion_service import SincronizacionService
    from logistica.services.trazabilidad_service import TrazabilidadService
    from logistica.services.autorizacion import Autorizador
    from logistica.services.health_service import HealthService
    from logistica.services.tercero_service import TerceroService
    from logistica.services.usuario_service import UsuarioService
    from logistica.ui.login_dialog import LoginDialog
    from logistica.ui.main_window import MainWindow
    from logistica.ui.terceros_view import TercerosView

    hasher = Hasher(time_cost=1, memory_cost_kib=8 * 1024, parallelism=1)
    aut = Autorizador(tx_connections)
    reales = Servicios(
        HealthService(tx_connections),
        AccesoService(tx_connections, hasher, SecurityConfig(), uuid.uuid4(), aut),
        UsuarioService(tx_connections, hasher, aut),
        TerceroService(tx_connections, aut),
        AuditoriaService(tx_connections, aut),
        CatalogoService(tx_connections, aut),
        EnvioService(tx_connections, aut),
        SincronizacionService(tx_connections),
        MonitoreoService(tx_connections, aut),
        TrazabilidadService(tx_connections, aut),
        ComunicacionService(tx_connections, aut),
    )
    sufijo = uuid.uuid4().hex[:8]
    with unit_of_work(tx_connections) as tx:
        uid = uuid.uuid4()
        UsuarioRepository().crear(
            tx,
            uid,
            UsuarioDatos("Com", "Ercial", f"com-{sufijo}@test.local"),
            hasher.hash("clave-de-prueba-123"),
        )
        UsuarioRepository().reemplazar_roles(tx, uid, {"COMERCIAL"})

    dialogo = LoginDialog(reales.acceso, "pruebas")
    dialogo.email.setText(f"com-{sufijo}@test.local")
    dialogo.password.setText("clave-de-prueba-123")
    dialogo.ingresar()
    assert esperar(qapp, lambda: dialogo.result() == QDialog.DialogCode.Accepted)

    ventana = MainWindow(reales, dialogo.principal)
    assert ventana.titulos_pestanias() == [
        "Estado",
        "Chat",
        "Envíos",
        "Trazabilidad",
        "Terceros",
        "Sesiones",
    ]  # COMERCIAL
    reales.terceros.crear(
        dialogo.principal.sesion_id,
        TerceroDatos(f"Cliente UI {sufijo}", "AR", "CUIT", f"30-{sufijo}"),
        {"CLIENTE"},
    )
    vista = next(v for v in ventana.vistas if isinstance(v, TercerosView))
    vista.buscar.setText(sufijo)
    vista.recargar()
    assert esperar(qapp, lambda: not vista.ocupado())
    assert vista.tabla.rowCount() == 1
    assert vista.tabla.item(0, 0).text() == f"Cliente UI {sufijo}"
    ventana.close()  # cierra la sesión en la base
    with pytest.raises(SessionExpiredError):
        reales.acceso.principal(dialogo.principal.sesion_id)


# --------------------------------------------------------------------------- auditoría
def test_la_pantalla_de_auditoria_pagina(qapp):
    from logistica.ui.auditoria_view import TAMANIO_PAGINA, AuditoriaView

    fake = FakeAuditoria(total=TAMANIO_PAGINA + 30)
    vista = AuditoriaView(fake, uuid.uuid4())
    vista.recargar()
    assert esperar(qapp, lambda: not vista.ocupado())
    assert vista.tabla.rowCount() == TAMANIO_PAGINA
    assert vista.boton_siguiente.isEnabled() and not vista.boton_anterior.isEnabled()
    assert vista.accion.count() == 3  # "Todas" + las acciones registradas
    vista.pagina_siguiente()
    assert esperar(qapp, lambda: not vista.ocupado())
    assert vista.tabla.rowCount() == 30
    assert fake.consultas[-1]["offset"] == TAMANIO_PAGINA
    assert not vista.boton_siguiente.isEnabled() and vista.boton_anterior.isEnabled()


def test_el_filtro_de_fechas_envia_fechas_con_zona_e_incluye_el_ultimo_dia(qapp):
    from PySide6.QtCore import QDate

    from logistica.ui.auditoria_view import AuditoriaView

    fake = FakeAuditoria(total=1)
    vista = AuditoriaView(fake, uuid.uuid4())
    vista.filtrar_fechas.setChecked(True)
    vista.desde.setDate(QDate(2026, 11, 1))
    vista.hasta.setDate(QDate(2026, 11, 3))
    vista.recargar()
    assert esperar(qapp, lambda: not vista.ocupado())
    filtros = fake.consultas[-1]
    assert filtros["desde"].tzinfo is not None and filtros["hasta"].tzinfo is not None
    assert (filtros["hasta"] - filtros["desde"]).days == 3  # del 1 al 3 inclusive


def test_sesion_vencida_cierra_el_formulario_modal_abierto(qapp):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog

    from logistica.ui.main_window import MainWindow

    ventana = MainWindow(servicios(), principal({"COMERCIAL"}, {"terceros.gestionar"}))
    ventana.show()
    dialogo = QDialog(ventana)
    QTimer.singleShot(50, lambda: ventana.terminar("Sesión vencida."))
    resultado = dialogo.exec()  # sin el arreglo, quedaría abierto bloqueando el login
    assert resultado == QDialog.DialogCode.Rejected
    assert ventana.isHidden()


def test_la_sincronizacion_corre_solo_para_internos_y_muestra_el_estado(qapp):
    from logistica.ui.main_window import MainWindow

    interno = MainWindow(servicios(), principal({"OPERACIONES"}, {"envios.crear"}))
    externo = MainWindow(servicios(), principal({"CLIENTE"}, {"envios.consultar"}, tercero_id=uuid.uuid4()))
    assert interno.sincronizacion_activa() and not externo.sincronizacion_activa()
    interno.sincronizar()
    assert esperar(qapp, lambda: "pendientes" in interno.statusBar().currentMessage())
    assert interno.statusBar().currentMessage() == "Sincronización: 2 pendientes, 1 fallidas (ver logs)"
    interno.close()
    assert not interno.sincronizacion_activa()  # al cerrar se detiene
    externo.close()


def test_trazabilidad_consulta_fuera_del_hilo_grafico_y_muestra_errores(qapp):
    from datetime import datetime, timezone

    from PySide6.QtCore import QCoreApplication, QEvent, QThread

    from logistica.core.errors import DatabaseUnavailableError
    from logistica.domain.trazabilidad_dtos import EventoTrazabilidadDTO, PaginaEventosDTO
    from logistica.ui.trazabilidad_view import TrazabilidadView

    class FakeTrazabilidad:
        def __init__(self):
            self.hilo_consulta = None
            self.fallar = False

        def eventos(self, *args, **kwargs):
            assert len(args) == 2 and kwargs["limite"] == 50
            self.hilo_consulta = QThread.currentThread()
            if self.fallar:
                raise DatabaseUnavailableError("MongoDB")
            ahora = datetime.now(timezone.utc)
            evento = EventoTrazabilidadDTO(
                uuid.uuid4(),
                ahora,
                ahora,
                "RETRASO",
                "Demora informada",
                None,
                None,
                (),
                {"motivo": "prueba"},
                "EN_MONGO_SIN_OUTBOX",
            )
            return PaginaEventosDTO([evento], None, 1, 0)

    class FakeEnvios:
        pass

    servicio = FakeTrazabilidad()
    vista = TrazabilidadView(servicio, FakeEnvios(), uuid.uuid4())
    vista.selector_envio.blockSignals(True)
    vista.selector_envio.addItem("ENV-PRUEBA", uuid.uuid4())
    vista.selector_envio.blockSignals(False)
    vista._buscar_eventos()

    assert esperar(qapp, lambda: not vista.ocupado())
    assert servicio.hilo_consulta != qapp.thread()
    assert vista.tabla_eventos.rowCount() == 1
    assert "Outbox Mongo: 1" in vista.estado_eventos.text()

    servicio.fallar = True
    vista._buscar_eventos()
    assert esperar(qapp, lambda: not vista.ocupado())
    assert "No se pudo acceder a MongoDB" in vista.estado.text()
    vista.cerrar()
    vista.close()
    vista.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
