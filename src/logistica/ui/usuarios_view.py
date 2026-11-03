"""Pestaña "Usuarios": alta, edición, roles, activación y reseteo de contraseña (solo administradores)."""

from __future__ import annotations

import uuid

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from logistica.core.errors import ValidationError
from logistica.domain.acceso import (
    ROLES_EXTERNOS,
    ROLES_INTERNOS,
    Rol,
    TerceroDTO,
    UsuarioDatos,
    UsuarioDTO,
    validar_password,
)
from logistica.services.tercero_service import TerceroService
from logistica.services.usuario_service import UsuarioService
from logistica.ui.widgets import VistaBase, clave_seleccionada, llenar_tabla, si_no, tabla

COLUMNAS = ("Apellido", "Nombre", "Email", "Empresa", "Roles", "Activo")


class UsuariosView(VistaBase):
    def __init__(
        self,
        usuarios: UsuarioService,
        terceros: TerceroService,
        sesion_id: uuid.UUID,
        parent: QWidget | None = None,
    ):
        super().__init__(sesion_id, parent)
        self._usuarios = usuarios
        self._terceros = terceros
        self._lista: dict[uuid.UUID, UsuarioDTO] = {}

        self.tabla = tabla(COLUMNAS)
        self.tabla.itemSelectionChanged.connect(self.actualizar_botones)
        self.boton_recargar = QPushButton("Recargar")
        self.boton_nuevo = QPushButton("Nuevo")
        self.boton_editar = QPushButton("Editar datos y roles")
        self.boton_activo = QPushButton("Desactivar")
        self.boton_password = QPushButton("Resetear contraseña")
        self.boton_recargar.clicked.connect(self.recargar)
        self.boton_nuevo.clicked.connect(self.nuevo)
        self.boton_editar.clicked.connect(self.editar)
        self.boton_activo.clicked.connect(self.alternar_activo)
        self.boton_password.clicked.connect(self.resetear_password)

        acciones = QHBoxLayout()
        for b in (
            self.boton_recargar,
            self.boton_nuevo,
            self.boton_editar,
            self.boton_activo,
            self.boton_password,
        ):
            acciones.addWidget(b)
        acciones.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabla)
        layout.addLayout(acciones)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    def recargar(self) -> None:
        self.ejecutar(self._usuarios.listar, self._mostrar, self.sesion_id)

    def _mostrar(self, usuarios: list[UsuarioDTO]) -> None:
        self._lista = {u.id: u for u in usuarios}
        filas = [
            (
                u.apellido,
                u.nombre,
                u.email,
                u.tercero_razon_social or "(interno)",
                ", ".join(sorted(u.roles)),
                si_no(u.activo),
            )
            for u in usuarios
        ]
        llenar_tabla(self.tabla, filas, [u.id for u in usuarios])

    def seleccionado(self) -> UsuarioDTO | None:
        clave = clave_seleccionada(self.tabla)
        return self._lista.get(clave) if clave else None

    def nuevo(self) -> None:
        # Primero se traen las empresas (para usuarios externos) y después se abre el formulario.
        self.ejecutar(self._terceros.listar, self._abrir_alta, self.sesion_id)

    def _abrir_alta(self, terceros: list[TerceroDTO]) -> None:
        dialogo = UsuarioDialog(self, terceros)
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        dialogo.deleteLater()  # los valores ya se leen abajo; se libera al volver al bucle
        if aceptado:
            datos, roles, password = dialogo.valores()
            self.ejecutar(
                self._usuarios.crear, lambda _u: self.recargar(), self.sesion_id, datos, roles, password
            )

    def editar(self) -> None:
        u = self.seleccionado()
        if u is None:
            return
        dialogo = UsuarioDialog(self, [], usuario=u)
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        dialogo.deleteLater()  # los valores ya se leen abajo; se libera al volver al bucle
        if aceptado:
            datos, roles, _ = dialogo.valores()
            self.ejecutar(
                self._usuarios.actualizar, lambda _u: self.recargar(), self.sesion_id, u.id, datos, roles
            )

    def alternar_activo(self) -> None:
        u = self.seleccionado()
        if u is not None:
            self.ejecutar(
                self._usuarios.set_activo, lambda _u: self.recargar(), self.sesion_id, u.id, not u.activo
            )

    def resetear_password(self) -> None:
        u = self.seleccionado()
        if u is None:
            return
        nueva, ok = QInputDialog.getText(
            self, "Resetear contraseña", f"Nueva contraseña para {u.email}:", QLineEdit.EchoMode.Password
        )
        if ok and nueva:
            self.ejecutar(
                self._usuarios.resetear_password,
                lambda _r: self._set_estado(
                    "Contraseña reseteada; las sesiones del usuario se cerraron.", "ok"
                ),
                self.sesion_id,
                u.id,
                nueva,
            )

    def actualizar_botones(self) -> None:
        u = self.seleccionado()
        libre = not self.ocupado()
        self.boton_recargar.setEnabled(libre)
        self.boton_nuevo.setEnabled(libre)
        for b in (self.boton_editar, self.boton_activo, self.boton_password):
            b.setEnabled(libre and u is not None)
        self.boton_activo.setText("Activar" if u is not None and not u.activo else "Desactivar")


class UsuarioDialog(QDialog):
    """Alta (con empresa y contraseña) o edición (datos y roles; la empresa no se cambia)."""

    def __init__(self, parent: QWidget | None, terceros: list[TerceroDTO], usuario: UsuarioDTO | None = None):
        super().__init__(parent)
        self._usuario = usuario
        self.setWindowTitle("Editar usuario" if usuario else "Nuevo usuario")
        self.nombre = QLineEdit(usuario.nombre if usuario else "")
        self.apellido = QLineEdit(usuario.apellido if usuario else "")
        self.email = QLineEdit(usuario.email if usuario else "")
        self.empresa = QComboBox()
        if usuario:
            self.empresa.addItem(usuario.tercero_razon_social or "(usuario interno)", usuario.tercero_id)
            self.empresa.setEnabled(False)
        else:
            self.empresa.addItem("(usuario interno)", None)
            for t in terceros:
                self.empresa.addItem(f"{t.razon_social} [{', '.join(sorted(t.roles_comerciales))}]", t.id)
        self.empresa.currentIndexChanged.connect(self._ajustar_roles)
        self.roles = {r.value: QCheckBox(r.value) for r in Rol}
        for codigo, check in self.roles.items():
            check.setChecked(bool(usuario and codigo in usuario.roles))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password2 = QLineEdit()
        self.password2.setEchoMode(QLineEdit.EchoMode.Password)
        self.error = QLabel("")
        self.error.setProperty("estado", "error")
        self.error.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Nombre *", self.nombre)
        form.addRow("Apellido *", self.apellido)
        form.addRow("Email *", self.email)
        form.addRow("Empresa", self.empresa)
        caja_roles = QVBoxLayout()
        for check in self.roles.values():
            caja_roles.addWidget(check)
        form.addRow("Roles *", caja_roles)
        if usuario is None:
            form.addRow("Contraseña *", self.password)
            form.addRow("Repetir contraseña *", self.password2)
        botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        botones.accepted.connect(self._aceptar)
        botones.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(botones)
        self._ajustar_roles()

    def _ajustar_roles(self) -> None:
        """Ayuda visual: un interno solo puede elegir roles internos y un externo solo externos.
        La regla completa (rol comercial de la empresa) la valida el servicio."""
        externo = self.empresa.currentData() is not None
        permitidos = ROLES_EXTERNOS if externo else ROLES_INTERNOS
        for codigo, check in self.roles.items():
            check.setEnabled(codigo in permitidos)
            if codigo not in permitidos:
                check.setChecked(False)

    def valores(self) -> tuple[UsuarioDatos, set[str], str]:
        datos = UsuarioDatos(
            self.nombre.text(), self.apellido.text(), self.email.text(), self.empresa.currentData()
        )
        return datos, {c for c, check in self.roles.items() if check.isChecked()}, self.password.text()

    def _aceptar(self) -> None:
        datos, roles, password = self.valores()
        try:
            datos.normalizado()
            if self._usuario is None:
                validar_password(password)
        except ValidationError as exc:
            self.error.setText(exc.user_message)
            return
        if not roles:
            self.error.setText("Elija al menos un rol.")
            return
        if self._usuario is None and password != self.password2.text():
            self.error.setText("Las contraseñas no coinciden.")
            return
        self.accept()
