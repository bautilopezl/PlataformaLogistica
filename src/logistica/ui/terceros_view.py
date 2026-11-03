"""Pestaña "Terceros": ABM de clientes, proveedores y operadores con sus roles comerciales."""

from __future__ import annotations

import uuid
from functools import partial

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from logistica.core.errors import ValidationError
from logistica.domain.acceso import RolComercial, TerceroDatos, TerceroDTO
from logistica.services.tercero_service import TerceroService
from logistica.ui.widgets import VistaBase, clave_seleccionada, llenar_tabla, si_no, tabla

COLUMNAS = ("Razón social", "País", "Identificación", "Roles comerciales", "Email", "Activo")


class TercerosView(VistaBase):
    def __init__(
        self,
        servicio: TerceroService,
        sesion_id: uuid.UUID,
        puede_gestionar: bool,
        parent: QWidget | None = None,
    ):
        super().__init__(sesion_id, parent)
        self._servicio = servicio
        self._puede_gestionar = puede_gestionar  # solo cambia qué botones se ven: el servicio igual valida
        self._terceros: dict[uuid.UUID, TerceroDTO] = {}

        self.buscar = QLineEdit()
        self.buscar.setPlaceholderText("Buscar por razón social o identificación")
        self.buscar.returnPressed.connect(self.recargar)
        self.rol = QComboBox()
        self.rol.addItem("Todos los roles", None)
        for r in RolComercial:
            self.rol.addItem(r.value.title(), r.value)
        self.inactivos = QCheckBox("Incluir inactivos")
        self.boton_buscar = QPushButton("Buscar")
        self.boton_buscar.clicked.connect(self.recargar)
        self.tabla = tabla(COLUMNAS)
        self.tabla.itemSelectionChanged.connect(self.actualizar_botones)
        self.tabla.doubleClicked.connect(self.editar)

        self.boton_nuevo = QPushButton("Nuevo")
        self.boton_editar = QPushButton("Editar")
        self.boton_activo = QPushButton("Desactivar")
        self.boton_nuevo.clicked.connect(self.nuevo)
        self.boton_editar.clicked.connect(self.editar)
        self.boton_activo.clicked.connect(self.alternar_activo)

        filtros = QHBoxLayout()
        for w in (self.buscar, self.rol, self.inactivos, self.boton_buscar):
            filtros.addWidget(w)
        acciones = QHBoxLayout()
        for w in (self.boton_nuevo, self.boton_editar, self.boton_activo):
            w.setVisible(puede_gestionar)
            acciones.addWidget(w)
        acciones.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(filtros)
        layout.addWidget(self.tabla)
        layout.addLayout(acciones)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    # --- carga --------------------------------------------------------------
    def recargar(self) -> None:
        # Los widgets se leen acá, en el hilo gráfico; el trabajador solo recibe valores (nunca toca widgets).
        consulta = partial(
            self._servicio.listar,
            self.sesion_id,
            texto=self.buscar.text(),
            rol=self.rol.currentData(),
            incluir_inactivos=self.inactivos.isChecked(),
        )
        self.ejecutar(consulta, self._mostrar)

    def _mostrar(self, terceros: list[TerceroDTO]) -> None:
        self._terceros = {t.id: t for t in terceros}
        filas = [
            (
                t.razon_social,
                t.pais_codigo,
                f"{t.tipo_identificacion} {t.numero_identificacion}",
                ", ".join(sorted(t.roles_comerciales)),
                t.email,
                si_no(t.activo),
            )
            for t in terceros
        ]
        llenar_tabla(self.tabla, filas, [t.id for t in terceros])
        if not terceros:
            self._set_estado("No hay terceros que coincidan con la búsqueda.", "pendiente")

    # --- acciones -----------------------------------------------------------
    def seleccionado(self) -> TerceroDTO | None:
        clave = clave_seleccionada(self.tabla)
        return self._terceros.get(clave) if clave else None

    def nuevo(self) -> None:
        dialogo = TerceroDialog(self)
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        dialogo.deleteLater()  # los valores ya se leen abajo; se libera al volver al bucle
        if aceptado:
            datos, roles = dialogo.valores()
            self.ejecutar(self._servicio.crear, lambda _t: self.recargar(), self.sesion_id, datos, roles)

    def editar(self) -> None:
        t = self.seleccionado()
        if t is None or not self._puede_gestionar:
            return
        dialogo = TerceroDialog(self, t)
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        dialogo.deleteLater()  # los valores ya se leen abajo; se libera al volver al bucle
        if aceptado:
            datos, roles = dialogo.valores()
            self.ejecutar(
                self._servicio.actualizar, lambda _t: self.recargar(), self.sesion_id, t.id, datos, roles
            )

    def alternar_activo(self) -> None:
        t = self.seleccionado()
        if t is not None:
            self.ejecutar(
                self._servicio.set_activo, lambda _t: self.recargar(), self.sesion_id, t.id, not t.activo
            )

    def actualizar_botones(self) -> None:
        t = self.seleccionado()
        libre = not self.ocupado()
        self.boton_buscar.setEnabled(libre)
        self.boton_nuevo.setEnabled(libre)
        self.boton_editar.setEnabled(libre and t is not None)
        self.boton_activo.setEnabled(libre and t is not None)
        self.boton_activo.setText("Activar" if t is not None and not t.activo else "Desactivar")


class TerceroDialog(QDialog):
    """Formulario de alta/edición. Valida lo obligatorio antes de devolver los datos al servicio."""

    def __init__(self, parent: QWidget | None, tercero: TerceroDTO | None = None):
        super().__init__(parent)
        self.setWindowTitle("Editar tercero" if tercero else "Nuevo tercero")
        self.razon = QLineEdit(tercero.razon_social if tercero else "")
        self.pais = QLineEdit(tercero.pais_codigo if tercero else "AR")
        self.pais.setMaxLength(2)
        self.tipo = QComboBox()
        self.tipo.setEditable(True)  # se admiten identificaciones extranjeras además del CUIT
        self.tipo.addItems(["CUIT", "RUT", "NIT", "RFC", "EIN", "VAT"])
        if tercero:
            self.tipo.setCurrentText(tercero.tipo_identificacion)
        self.numero = QLineEdit(tercero.numero_identificacion if tercero else "")
        self.email = QLineEdit((tercero.email or "") if tercero else "")
        self.telefono = QLineEdit((tercero.telefono or "") if tercero else "")
        self.direccion = QLineEdit((tercero.direccion or "") if tercero else "")
        self.roles = {r.value: QCheckBox(r.value.title()) for r in RolComercial}
        for codigo, check in self.roles.items():
            check.setChecked(bool(tercero and codigo in tercero.roles_comerciales))
        self.error = QLabel("")
        self.error.setProperty("estado", "error")
        self.error.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Razón social *", self.razon)
        form.addRow("País (ISO) *", self.pais)
        form.addRow("Tipo de identificación *", self.tipo)
        form.addRow("Número *", self.numero)
        form.addRow("Email", self.email)
        form.addRow("Teléfono", self.telefono)
        form.addRow("Dirección", self.direccion)
        fila_roles = QHBoxLayout()
        for check in self.roles.values():
            fila_roles.addWidget(check)
        form.addRow("Roles comerciales *", fila_roles)
        botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        botones.accepted.connect(self._aceptar)
        botones.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(botones)

    def valores(self) -> tuple[TerceroDatos, set[str]]:
        datos = TerceroDatos(
            self.razon.text(),
            self.pais.text(),
            self.tipo.currentText(),
            self.numero.text(),
            self.email.text(),
            self.telefono.text(),
            self.direccion.text(),
        )
        return datos, {c for c, check in self.roles.items() if check.isChecked()}

    def _aceptar(self) -> None:
        datos, roles = self.valores()
        try:
            datos.normalizado()  # mismas reglas que el servicio, para avisar antes de enviar
        except ValidationError as exc:
            self.error.setText(exc.user_message)
            return
        if not roles:
            self.error.setText("Elija al menos un rol comercial.")
            return
        self.accept()
