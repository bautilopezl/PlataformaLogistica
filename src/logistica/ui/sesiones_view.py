"""Pestaña "Sesiones": sesiones abiertas propias (o de todos, con sesiones.administrar) y revocación."""

from __future__ import annotations

import uuid
from functools import partial

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from logistica.domain.acceso import SesionDTO
from logistica.services.acceso_service import AccesoService
from logistica.ui.widgets import VistaBase, clave_seleccionada, llenar_tabla, tabla

COLUMNAS = ("Usuario", "Inicio", "Vence", "Estado", "Equipo", "Actual")


def _local(dt) -> str:
    # Se guarda en UTC; se convierte a hora local solo al mostrar.
    return dt.astimezone().strftime("%d/%m/%Y %H:%M") if dt else ""


class SesionesView(VistaBase):
    def __init__(
        self,
        acceso: AccesoService,
        sesion_id: uuid.UUID,
        puede_ver_todas: bool,
        parent: QWidget | None = None,
    ):
        super().__init__(sesion_id, parent)
        self._acceso = acceso
        self._lista: dict[uuid.UUID, SesionDTO] = {}
        self.todas = QCheckBox("Ver las de todos los usuarios")
        self.todas.setVisible(puede_ver_todas)
        self.todas.toggled.connect(self.recargar)
        self.tabla = tabla(COLUMNAS)
        self.tabla.itemSelectionChanged.connect(self.actualizar_botones)
        self.boton_recargar = QPushButton("Recargar")
        self.boton_revocar = QPushButton("Revocar sesión")
        self.boton_recargar.clicked.connect(self.recargar)
        self.boton_revocar.clicked.connect(self.revocar)

        acciones = QHBoxLayout()
        for w in (self.todas, self.boton_recargar, self.boton_revocar):
            acciones.addWidget(w)
        acciones.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabla)
        layout.addLayout(acciones)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    def recargar(self) -> None:
        # El checkbox se lee en el hilo gráfico; el trabajador recibe solo el valor.
        self.ejecutar(
            partial(self._acceso.listar_sesiones, self.sesion_id, todas=self.todas.isChecked()), self._mostrar
        )

    def _mostrar(self, sesiones: list[SesionDTO]) -> None:
        self._lista = {s.id: s for s in sesiones}
        filas = [
            (
                s.usuario_email,
                _local(s.inicio_en),
                _local(s.vence_en),
                s.estado,
                s.cliente_descripcion,
                "Esta sesión" if s.es_actual else "",
            )
            for s in sesiones
        ]
        llenar_tabla(self.tabla, filas, [s.id for s in sesiones])

    def revocar(self) -> None:
        clave = clave_seleccionada(self.tabla)
        if clave is not None:
            self.ejecutar(self._acceso.revocar_sesion, lambda _r: self.recargar(), self.sesion_id, clave)

    def actualizar_botones(self) -> None:
        libre = not self.ocupado()
        self.boton_recargar.setEnabled(libre)
        self.boton_revocar.setEnabled(libre and clave_seleccionada(self.tabla) is not None)
