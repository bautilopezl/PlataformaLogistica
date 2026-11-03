"""Diálogo de inicio de sesión."""

from __future__ import annotations

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from logistica.domain.acceso import Principal
from logistica.services.acceso_service import AccesoService
from logistica.ui.worker import WorkerPool

SHUTDOWN_WAIT_MS = 10_000


class LoginDialog(QDialog):
    def __init__(
        self,
        acceso: AccesoService,
        cliente_descripcion: str,
        mensaje: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Iniciar sesión · Plataforma de logística")
        self._acceso = acceso
        self._cliente = cliente_descripcion
        self._pool = WorkerPool(self)
        self.principal: Principal | None = None

        titulo = QLabel("Plataforma de logística internacional")
        titulo.setObjectName("titulo")
        self.email = QLineEdit()
        self.email.setPlaceholderText("usuario@empresa.com")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.mensaje = QLabel(mensaje)
        self.mensaje.setWordWrap(True)
        self.mensaje.setProperty("estado", "error")
        self.boton = QPushButton("Ingresar")
        self.boton.setDefault(True)
        self.boton.clicked.connect(self.ingresar)
        self.password.returnPressed.connect(self.ingresar)

        form = QFormLayout()
        form.addRow("Email:", self.email)
        form.addRow("Contraseña:", self.password)
        layout = QVBoxLayout(self)
        layout.addWidget(titulo)
        layout.addLayout(form)
        layout.addWidget(self.mensaje)
        layout.addWidget(self.boton)
        self.resize(380, 200)

    @Slot()
    def ingresar(self) -> None:
        email, password = self.email.text().strip(), self.password.text()
        if not email or not password:  # validación del formulario antes de llamar al servicio
            self.mensaje.setText("Complete email y contraseña.")
            return
        self.boton.setEnabled(False)
        self.mensaje.setText("Verificando…")
        self._pool.submit(self._acceso.login, self._ok, self._error, email, password, self._cliente)

    @Slot(object)
    def _ok(self, principal: Principal) -> None:
        self.principal = principal
        self.password.clear()
        self.accept()

    @Slot(str)
    def _error(self, mensaje: str) -> None:
        self.mensaje.setText(mensaje)
        self.password.clear()
        self.password.setFocus()
        self.boton.setEnabled(True)

    def reject(self) -> None:
        # Mientras se verifica no se puede cancelar: el servicio ya pudo haber abierto la sesión en la base y
        # quedaría ABIERTA sin nadie que la use. Al terminar (bien o mal) se puede cancelar normalmente.
        if not self.boton.isEnabled() and self.principal is None:
            return
        super().reject()

    def done(self, result: int) -> None:
        self._pool.wait_for_done(SHUTDOWN_WAIT_MS)
        super().done(result)
