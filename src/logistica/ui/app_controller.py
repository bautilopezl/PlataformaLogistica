"""Alterna entre el login y la ventana principal durante la vida de la aplicación."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QApplication, QDialog

from logistica.services.contenedor import Servicios
from logistica.ui.login_dialog import LoginDialog
from logistica.ui.main_window import MainWindow

log = logging.getLogger(__name__)


class AppController(QObject):
    def __init__(self, app: QApplication, servicios: Servicios, cliente_descripcion: str):
        super().__init__()
        self._app = app
        self._servicios = servicios
        self._cliente = cliente_descripcion
        self.ventana: MainWindow | None = None
        # La app no termina al cerrar la ventana por logout: se vuelve a mostrar el login.
        app.setQuitOnLastWindowClosed(False)

    def iniciar(self, mensaje: str = "") -> bool:
        """Muestra el login; si el usuario entra abre la ventana principal. False si canceló."""
        dialogo = LoginDialog(self._servicios.acceso, self._cliente, mensaje)
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        principal = dialogo.principal
        dialogo.deleteLater()  # se destruye en el hilo gráfico, no cuando el recolector lo encuentre
        if not aceptado or principal is None:
            return False
        self.ventana = MainWindow(self._servicios, principal)
        self.ventana.sesion_terminada.connect(self._al_terminar)
        self.ventana.salir.connect(self._app.quit)
        self.ventana.show()
        self.ventana.estado.refresh()
        return True

    @Slot(str)
    def _al_terminar(self, mensaje: str) -> None:
        ventana, self.ventana = self.ventana, None
        if ventana is not None:
            ventana.deleteLater()
        # Se vuelve al login fuera del manejador de la señal, con el bucle de eventos libre.
        QTimer.singleShot(0, lambda: self._relogin(mensaje))

    def _relogin(self, mensaje: str) -> None:
        # Con setQuitOnLastWindowClosed(False), si acá algo falla y no queda ninguna ventana, la app quedaría
        # viva sin interfaz: ante cualquier error se registra y se termina.
        try:
            continuar = self.iniciar(mensaje)
        except Exception:
            log.exception("No se pudo volver a mostrar el login")
            continuar = False
        if not continuar:
            self._app.quit()
