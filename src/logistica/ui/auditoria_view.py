"""Pestaña "Auditoría": registro de operaciones, filtrado y paginado (solo lectura)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timedelta
from functools import partial

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from logistica.domain.auditoria import AuditoriaDTO, PaginaAuditoria
from logistica.services.auditoria_service import AuditoriaService
from logistica.ui.widgets import VistaBase, llenar_tabla, tabla

COLUMNAS = ("Fecha", "Usuario", "Acción", "Resultado", "Sobre", "Detalle")
TAMANIO_PAGINA = 100


def _fecha_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%d/%m/%Y %H:%M:%S")  # se guarda en UTC; local solo al mostrar


def _inicio_del_dia(fecha: QDate) -> datetime:
    """Medianoche local del día elegido, con zona horaria (el servicio exige fechas con zona)."""
    return datetime.combine(fecha.toPython(), time.min).astimezone()


class AuditoriaView(VistaBase):
    def __init__(self, servicio: AuditoriaService, sesion_id: uuid.UUID, parent: QWidget | None = None):
        super().__init__(sesion_id, parent)
        self._servicio = servicio
        self._offset = 0
        self._hay_mas = False

        self.accion = QComboBox()
        self.accion.addItem("Todas las acciones", None)
        self.filtrar_fechas = QCheckBox("Entre")
        self.desde = QDateEdit(QDate.currentDate().addDays(-7))
        self.hasta = QDateEdit(QDate.currentDate())
        for campo in (self.desde, self.hasta):
            campo.setCalendarPopup(True)
            campo.setDisplayFormat("dd/MM/yyyy")
        self.boton_buscar = QPushButton("Buscar")
        self.boton_buscar.clicked.connect(self.recargar)
        self.tabla = tabla(COLUMNAS)
        self.boton_anterior = QPushButton("◀ Más recientes")
        self.boton_siguiente = QPushButton("Más antiguos ▶")
        self.pagina = QLabel("")
        self.boton_anterior.clicked.connect(self.pagina_anterior)
        self.boton_siguiente.clicked.connect(self.pagina_siguiente)

        filtros = QHBoxLayout()
        for w in (self.accion, self.filtrar_fechas, self.desde, QLabel("y"), self.hasta, self.boton_buscar):
            filtros.addWidget(w)
        filtros.addStretch()
        navegacion = QHBoxLayout()
        navegacion.addWidget(self.boton_anterior)
        navegacion.addWidget(self.pagina)
        navegacion.addWidget(self.boton_siguiente)
        navegacion.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(filtros)
        layout.addWidget(self.tabla)
        layout.addLayout(navegacion)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    # --- carga --------------------------------------------------------------
    def recargar(self) -> None:
        """Nueva búsqueda desde la primera página (y refresca la lista de acciones)."""
        self._offset = 0
        self.ejecutar(self._servicio.acciones, self._cargar_acciones, self.sesion_id)
        self._buscar()

    def pagina_anterior(self) -> None:
        self._offset = max(0, self._offset - TAMANIO_PAGINA)
        self._buscar()

    def pagina_siguiente(self) -> None:
        if self._hay_mas:
            self._offset += TAMANIO_PAGINA
            self._buscar()

    def _buscar(self) -> None:
        # Los filtros se leen acá (hilo gráfico); el trabajador recibe solo valores.
        desde = hasta = None
        if self.filtrar_fechas.isChecked():
            desde = _inicio_del_dia(self.desde.date())
            hasta = _inicio_del_dia(self.hasta.date()) + timedelta(days=1)  # incluye todo el día "hasta"
        consulta = partial(
            self._servicio.listar,
            self.sesion_id,
            accion=self.accion.currentData(),
            desde=desde,
            hasta=hasta,
            limite=TAMANIO_PAGINA,
            offset=self._offset,
        )
        self.ejecutar(consulta, self._mostrar)

    def _cargar_acciones(self, acciones: list[str]) -> None:
        actual = self.accion.currentData()
        self.accion.blockSignals(True)
        self.accion.clear()
        self.accion.addItem("Todas las acciones", None)
        for a in acciones:
            self.accion.addItem(a, a)
        indice = self.accion.findData(actual)
        self.accion.setCurrentIndex(max(0, indice))
        self.accion.blockSignals(False)

    def _mostrar(self, pagina: PaginaAuditoria) -> None:
        self._hay_mas = pagina.hay_mas
        filas = [self._fila(r) for r in pagina.registros]
        llenar_tabla(self.tabla, filas, [r.id for r in pagina.registros])
        if pagina.registros:
            desde, hasta = self._offset + 1, self._offset + len(pagina.registros)
            self.pagina.setText(f"Registros {desde}–{hasta}")
        else:
            self.pagina.setText("Sin registros para estos filtros.")

    @staticmethod
    def _fila(r: AuditoriaDTO) -> tuple:
        sobre = f"{r.entidad} {str(r.entidad_id)[:8]}…" if r.entidad else ""
        detalle = json.dumps(r.detalle, ensure_ascii=False) if r.detalle else ""
        return (
            _fecha_local(r.fecha),
            r.usuario_email or "(desconocido)",
            r.accion,
            r.resultado,
            sobre,
            detalle,
        )

    def actualizar_botones(self) -> None:
        libre = not self.ocupado()
        self.boton_buscar.setEnabled(libre)
        self.boton_anterior.setEnabled(libre and self._offset > 0)
        self.boton_siguiente.setEnabled(libre and self._hay_mas)
