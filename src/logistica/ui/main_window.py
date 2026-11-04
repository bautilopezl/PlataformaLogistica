"""Ventana principal después del login: pestañas según los permisos del usuario.

Las pestañas visibles dependen de los permisos solo para comodidad: cada servicio vuelve a validar sesión,
permiso y alcance en cada operación.
"""

from __future__ import annotations

import logging
from threading import Event

from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from logistica.core.errors import ValidationError
from logistica.domain.acceso import PERMISOS_CONSULTA_TERCEROS, Permiso, Principal, validar_password
from logistica.services.contenedor import Servicios
from logistica.services.reporte_service import PERMISOS_REPORTES
from logistica.ui.estado_view import EstadoView
from logistica.ui.sesiones_view import SesionesView
from logistica.ui.terceros_view import TercerosView
from logistica.ui.usuarios_view import UsuariosView
from logistica.ui.auditoria_view import AuditoriaView
from logistica.ui.catalogos_view import CatalogosView
from logistica.ui.chat_view import ChatView
from logistica.ui.comercial_view import ComercialView
from logistica.ui.reportes_view import ReportesView
from logistica.ui.envios_view import EnviosView
from logistica.ui.monitoreo_view import MonitoreoView
from logistica.ui.trazabilidad_view import TrazabilidadView
from logistica.ui.worker import WorkerPool

log = logging.getLogger(__name__)
SHUTDOWN_WAIT_MS = 10_000
SYNC_MS = 10_000  # SUPUESTO (AGENTS.md §16.6): frecuencia de sincronización, a medir
MONITOREO_MS = 60_000  # evaluación de lecturas pendientes
PROCESOS_MS = 60_000  # planificador de procesos periódicos (fase 8): la tarea más frecuente es cada minuto


class MainWindow(QMainWindow):
    # Se emite cuando la sesión terminó (logout, revocación o vencimiento); el texto explica el motivo.
    sesion_terminada = Signal(str)
    # Se emite cuando el usuario cierra la ventana: la aplicación termina.
    salir = Signal()

    def __init__(self, servicios: Servicios, principal: Principal):
        super().__init__()
        self.setWindowTitle("Plataforma de logística internacional")
        self._servicios = servicios
        self.principal = principal
        self._pool = WorkerPool(self)
        self._sincronizando = False
        self._procesando_monitoreo = False
        self._detener_monitoreo = Event()
        self._terminada = False
        self._mensaje_terminacion: str | None = None

        usuario = QLabel(f"{principal.nombre_completo} · {', '.join(sorted(principal.roles))}")
        self.boton_password = QPushButton("Cambiar contraseña")
        self.boton_salir = QPushButton("Cerrar sesión")
        self.boton_password.clicked.connect(self.cambiar_password)
        self.boton_salir.clicked.connect(self.cerrar_sesion)
        cabecera = QHBoxLayout()
        cabecera.addWidget(usuario)
        cabecera.addStretch()
        cabecera.addWidget(self.boton_password)
        cabecera.addWidget(self.boton_salir)

        self.tabs = QTabWidget()
        self.estado = EstadoView(servicios.salud)
        self.tabs.addTab(self.estado, "Estado")
        self.vistas: list = []
        self.chat = ChatView(servicios.comunicacion, principal)
        self._agregar(self.chat, "Chat")
        if principal.tiene_permiso(Permiso.ENVIOS_CONSULTAR):
            self._agregar(
                EnviosView(
                    servicios.envios,
                    servicios.catalogo,
                    servicios.terceros,
                    principal.sesion_id,
                    principal.permisos,
                ),
                "Envíos",
            )
            self._agregar(
                TrazabilidadView(servicios.trazabilidad, servicios.envios, principal.sesion_id),
                "Trazabilidad",
            )
        if principal.es_interno and principal.tiene_permiso(Permiso.ENVIOS_PLANIFICAR, Permiso.ENVIOS_CREAR):
            self._agregar(
                CatalogosView(servicios.catalogo, servicios.terceros, principal.sesion_id), "Catálogos"
            )
        self.monitoreo: MonitoreoView | None = None
        if principal.tiene_permiso(Permiso.MONITOREO_CONSULTAR):
            self.monitoreo = MonitoreoView(servicios.monitoreo, servicios.envios, principal)
            self._agregar(self.monitoreo, "Monitoreo")
        if principal.tiene_permiso(Permiso.CUENTAS_CONSULTAR, Permiso.FACTURAS_EMITIR):
            self._agregar(
                ComercialView(servicios.comercial, servicios.envios, servicios.terceros, principal),
                "Comercial",
            )
        if principal.tiene_permiso(*PERMISOS_REPORTES):
            self._agregar(
                ReportesView(servicios.reportes, servicios.predicciones, servicios.terceros, principal),
                "Reportes",
            )
        if principal.tiene_permiso(*PERMISOS_CONSULTA_TERCEROS):
            self._agregar(
                TercerosView(
                    servicios.terceros,
                    principal.sesion_id,
                    principal.tiene_permiso(Permiso.TERCEROS_GESTIONAR),
                ),
                "Terceros",
            )
        if principal.tiene_permiso(Permiso.USUARIOS_ADMINISTRAR):
            self._agregar(
                UsuariosView(servicios.usuarios, servicios.terceros, principal.sesion_id), "Usuarios"
            )
        self._agregar(
            SesionesView(
                servicios.acceso, principal.sesion_id, principal.tiene_permiso(Permiso.SESIONES_ADMINISTRAR)
            ),
            "Sesiones",
        )
        if principal.tiene_permiso(Permiso.AUDITORIA_CONSULTAR):
            self._agregar(AuditoriaView(servicios.auditoria, principal.sesion_id), "Auditoría")
        self.tabs.currentChanged.connect(self._al_cambiar_pestania)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(cabecera)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central)
        self.resize(1180, 720)

        # Fase 3: mientras un usuario INTERNO tiene la app abierta, esta instancia procesa la outbox
        # (Mongo y Neo4j) cada SYNC_MS, fuera del hilo gráfico. Varias instancias a la vez no se pisan:
        # el reclamo de entregas es atómico (FOR UPDATE SKIP LOCKED).
        self._timer_sync = QTimer(self)
        self._timer_sync.timeout.connect(self.sincronizar)
        if principal.es_interno:
            self._timer_sync.start(SYNC_MS)
        self.estado_monitoreo = QLabel("Monitoreo: esta instancia no procesa")
        self.statusBar().addPermanentWidget(self.estado_monitoreo)
        self._timer_monitoreo = QTimer(self)
        self._timer_monitoreo.timeout.connect(self.procesar_monitoreo)
        if principal.es_interno and principal.tiene_permiso(Permiso.ENVIOS_PLANIFICAR):
            self._timer_monitoreo.start(MONITOREO_MS)
            QTimer.singleShot(0, self.procesar_monitoreo)
        # Fase 8: procesos periódicos (facturación, sensores inactivos, cierre mensual, predicción diaria) en
        # cada instancia INTERNA. Varias instancias no se pisan: advisory lock por tarea y período (SUPUESTO
        # P1).
        self._procesando = False
        self.estado_procesos = QLabel("Procesos: esta instancia no procesa")
        self.statusBar().addPermanentWidget(self.estado_procesos)
        self._timer_procesos = QTimer(self)
        self._timer_procesos.timeout.connect(self.procesar_periodicos)
        if principal.es_interno:
            self._timer_procesos.start(PROCESOS_MS)
            QTimer.singleShot(0, self.procesar_periodicos)  # al iniciar se revisa lo pendiente

    def titulos_pestanias(self) -> list[str]:
        return [self.tabs.tabText(i) for i in range(self.tabs.count())]

    def _agregar(self, vista, titulo: str) -> None:
        vista.sesion_vencida.connect(self.terminar)
        self.vistas.append(vista)
        self.tabs.addTab(vista, titulo)

    @Slot(int)
    def _al_cambiar_pestania(self, indice: int) -> None:
        vista = self.tabs.widget(indice)
        if hasattr(vista, "recargar") and not vista.ocupado():
            vista.recargar()  # cada pestaña muestra datos frescos al entrar

    # --- sesión -------------------------------------------------------------
    @Slot()
    def cerrar_sesion(self) -> None:
        self.boton_salir.setEnabled(False)
        self._pool.submit(
            self._servicios.acceso.logout,
            self._tras_logout,
            self._logout_fallido,
            self.principal.sesion_id,
            on_session_expired=self.terminar,
        )

    @Slot(object)
    def _tras_logout(self, _resultado: object) -> None:
        self.terminar("Sesión cerrada.")

    @Slot(str)
    def _logout_fallido(self, mensaje: str) -> None:
        # Si no se pudo cerrar en la base, no se finge: la sesión sigue abierta y se avisa
        # (en la barra de estado, sin diálogo modal).
        self.boton_salir.setEnabled(True)
        self.statusBar().showMessage(f"No se pudo cerrar la sesión: {mensaje}")

    @Slot(str)
    def terminar(self, mensaje: str) -> None:
        """La sesión ya no sirve: se cierra la ventana y se avisa al controlador para volver al login."""
        if self._terminada:
            return
        self._terminada = True
        # Un formulario modal abierto (por ejemplo, a mitad de una carga) bloquearía el login: se cierra.
        # Quien lo abrió recibe 'cancelado' y no envía nada.
        modal = QApplication.activeModalWidget()
        while modal is not None and modal is not self:
            modal.reject() if isinstance(modal, QDialog) else modal.close()
            siguiente = QApplication.activeModalWidget()
            if siguiente is modal:
                break
            modal = siguiente
        self._mensaje_terminacion = mensaje
        self.close()

    def cambiar_password(self) -> None:
        dialogo = CambiarPasswordDialog(self)
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        dialogo.deleteLater()  # los valores ya se leen abajo; se libera al volver al bucle
        if aceptado:
            actual, nueva = dialogo.valores()
            self._pool.submit(
                self._servicios.acceso.cambiar_password,
                self._password_ok,
                self._password_error,
                self.principal.sesion_id,
                actual,
                nueva,
                on_session_expired=self.terminar,
            )

    @Slot(object)
    def _password_ok(self, _r: object) -> None:
        QMessageBox.information(
            self, "Contraseña", "La contraseña se cambió. Se cerraron sus otras sesiones."
        )

    @Slot(str)
    def _password_error(self, mensaje: str) -> None:
        QMessageBox.warning(self, "Contraseña", mensaje)

    # --- cierre ordenado ----------------------------------------------------
    # --- sincronización (fase 3) --------------------------------------------
    def sincronizacion_activa(self) -> bool:
        return self._timer_sync.isActive()

    @Slot()
    def sincronizar(self) -> None:
        if self._sincronizando or self._terminada:
            return  # una vuelta por vez
        self._sincronizando = True
        servicio = self._servicios.sincronizacion

        def vuelta():  # corre en el trabajador: solo llama al servicio
            servicio.procesar_todo()
            return servicio.resumen()

        self._pool.submit(vuelta, self._sync_ok, self._sync_error)

    @Slot(object)
    def _sync_ok(self, resumen: dict) -> None:
        self._sincronizando = False
        pendientes = sum(r.get("PENDIENTE", 0) + r.get("EN_PROCESO", 0) for r in resumen.values())
        fallidas = sum(r.get("FALLIDA", 0) for r in resumen.values())
        texto = f"Sincronización: {pendientes} pendientes"
        self.statusBar().showMessage(texto + (f", {fallidas} fallidas (ver logs)" if fallidas else ""))

    @Slot(str)
    def _sync_error(self, mensaje: str) -> None:
        self._sincronizando = False
        self.statusBar().showMessage(f"Sincronización detenida: {mensaje}")

    @Slot()
    def procesar_periodicos(self) -> None:
        if self._procesando or self._terminada or not self._timer_procesos.isActive():
            return
        self._procesando = True
        self._pool.submit(
            self._servicios.procesos.ejecutar_con_sesion,
            self._procesos_ok,
            self._procesos_error,
            self.principal.sesion_id,
            on_session_expired=self.terminar,
        )

    @Slot(object)
    def _procesos_ok(self, resultados: list) -> None:
        self._procesando = False
        if self._terminada:
            return
        errores = [r for r in resultados if r.estado == "ERROR"]
        hechas = sum(r.estado == "EJECUTADA" for r in resultados)
        texto = f"Procesos: {hechas} ejecutados"
        self.estado_procesos.setText(texto + (f" · {len(errores)} con error (ver logs)" if errores else ""))
        self.estado_procesos.setToolTip("\n".join(f"{r.tarea.value} ({r.periodo}): {r.estado} {r.detalle}"
                                                   for r in resultados))

    @Slot(str)
    def _procesos_error(self, mensaje: str) -> None:
        self._procesando = False
        if not self._terminada:
            self.estado_procesos.setText(f"Procesos pendientes (se reintentará): {mensaje}")

    @Slot()
    def procesar_monitoreo(self) -> None:
        if (
            self._procesando_monitoreo
            or self._terminada
            or self._detener_monitoreo.is_set()
            or not self._timer_monitoreo.isActive()
        ):
            return
        self._procesando_monitoreo = True
        self._pool.submit(
            self._servicios.monitoreo.procesar_ciclo,
            self._monitoreo_ok,
            self._monitoreo_error,
            self.principal.sesion_id,
            self._detener_monitoreo,
            on_session_expired=self.terminar,
        )

    @Slot(object)
    def _monitoreo_ok(self, resultado: object) -> None:
        self._procesando_monitoreo = False
        if self._terminada:
            return
        lecturas = resultado
        self.estado_monitoreo.setText(
            f"Monitoreo: {lecturas.evaluadas} evaluadas · {lecturas.revision} revisión"
            f" · {lecturas.reintentos} reintentos"
        )
        if self.monitoreo is not None and self.tabs.currentWidget() is self.monitoreo:
            self.monitoreo.refrescar_datos()

    @Slot(str)
    def _monitoreo_error(self, mensaje: str) -> None:
        self._procesando_monitoreo = False
        if not self._terminada:
            self.estado_monitoreo.setText(f"Monitoreo pendiente (se reintentará): {mensaje}")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._timer_sync.stop()
        self._timer_monitoreo.stop()
        self._timer_procesos.stop()
        self._detener_monitoreo.set()
        self.chat.cerrar()
        if self.chat.trabajadores_activos():
            self.setEnabled(False)
            event.ignore()
            QTimer.singleShot(50, self.close)
            return
        for vista in (self.estado, *self.vistas):
            vista.cerrar()  # no aceptan trabajo nuevo y esperan lo que está en curso
        cerrada_por_el_usuario = not self._terminada
        if cerrada_por_el_usuario:
            # Cerrar la ventana también cierra la sesión en la base.
            self._terminada = True
            self._pool.submit(self._servicios.acceso.logout, _nada, _nada, self.principal.sesion_id)
        if not self._pool.wait_for_done(SHUTDOWN_WAIT_MS):
            log.warning("Se cerró la ventana con trabajadores todavía en curso")
        super().closeEvent(event)
        if cerrada_por_el_usuario:
            self.salir.emit()
        elif self._mensaje_terminacion is not None:
            mensaje, self._mensaje_terminacion = self._mensaje_terminacion, None
            self.sesion_terminada.emit(mensaje)


def _nada(_valor: object) -> None:
    """Callback vacío: al cerrar la app no hay nada que mostrar."""


class CambiarPasswordDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Cambiar contraseña")
        self.actual, self.nueva, self.nueva2 = QLineEdit(), QLineEdit(), QLineEdit()
        for campo in (self.actual, self.nueva, self.nueva2):
            campo.setEchoMode(QLineEdit.EchoMode.Password)
        self.error = QLabel("")
        self.error.setProperty("estado", "error")
        form = QFormLayout()
        form.addRow("Contraseña actual", self.actual)
        form.addRow("Nueva contraseña", self.nueva)
        form.addRow("Repetir nueva", self.nueva2)
        botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        botones.accepted.connect(self._aceptar)
        botones.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(botones)

    def valores(self) -> tuple[str, str]:
        return self.actual.text(), self.nueva.text()

    def _aceptar(self) -> None:
        try:
            validar_password(self.nueva.text())
        except ValidationError as exc:
            self.error.setText(exc.user_message)
            return
        if self.nueva.text() != self.nueva2.text():
            self.error.setText("Las contraseñas nuevas no coinciden.")
            return
        self.accept()
