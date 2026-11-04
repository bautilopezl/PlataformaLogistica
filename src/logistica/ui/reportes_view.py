"""Pestaña "Reportes": tipo de reporte, filtros, vista previa con gráfico, exportación PDF/CSV e historial
(fase 7).

Todo se calcula en el servicio, en un trabajador; esta pantalla solo arma los filtros y muestra la tabla que
vuelve. Los tipos que aparecen dependen de los permisos (el servicio vuelve a validarlos al consultar, generar
y abrir).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from functools import partial
from typing import Any

from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from logistica.core.errors import LogisticaError
from logistica.domain.acceso import PERMISOS_CONSULTA_TERCEROS, Permiso, Principal
from logistica.domain.analitica import Parametros, Tabla, celda
from logistica.ui.widgets import VistaBase, clave_seleccionada, fecha_local, llenar_tabla, tabla

COL_HISTORIAL = ("Solicitado", "Tipo", "Formato", "Estado", "Archivo / error", "Solicitante")


class ReportesView(VistaBase):
    def __init__(
        self,
        reportes: Any,
        predicciones: Any,
        terceros: Any,
        principal: Principal,
        parent: QWidget | None = None,
    ):
        super().__init__(principal.sesion_id, parent)
        self._rep, self._pred, self._terceros, self._p = reportes, predicciones, terceros, principal
        self._tipos: dict[str, Any] = {}
        self._historial: dict[uuid.UUID, Any] = {}
        self._tabla_actual: Tabla | None = None

        self.tipo = QComboBox()
        self.tipo.currentIndexChanged.connect(self._al_cambiar_tipo)
        hoy = QDate.currentDate()
        self.desde, self.hasta = QDateEdit(hoy.addMonths(-6)), QDateEdit(hoy)
        for d in (self.desde, self.hasta):
            d.setCalendarPopup(True)
            d.setDisplayFormat("dd/MM/yyyy")
        self.sin_rango = QCheckBox("Todo el período")
        self.sin_rango.toggled.connect(
            lambda marcado: [d.setEnabled(not marcado) for d in (self.desde, self.hasta)]
        )
        self.cliente = QComboBox()
        self.cliente.addItem("Todos los clientes", None)
        self.dimension = QComboBox()
        for texto, dato in (
            ("Por país de destino", "PAIS"),
            ("Por estado", "ESTADO"),
            ("Por cliente", "CLIENTE"),
        ):
            self.dimension.addItem(texto, dato)
        self.segun = QComboBox()
        for texto, dato in (("País de destino", "DESTINO"), ("País de origen", "ORIGEN")):
            self.segun.addItem(texto, dato)
        self.etiquetas = {
            "rango": QLabel("Desde / hasta:"),
            "cliente": QLabel("Cliente:"),
            "dimension": QLabel("Agrupar:"),
            "segun": QLabel("Según:"),
        }

        self.b_ver = QPushButton("Ver")
        self.b_pdf = QPushButton("Exportar PDF")
        self.b_csv = QPushButton("Exportar CSV")
        self.b_predecir = QPushButton("Calcular predicciones de hoy")
        self.b_predecir.setVisible(
            principal.es_interno and principal.tiene_permiso(Permiso.PREDICCIONES_CONSULTAR)
        )
        self.b_ver.clicked.connect(self.ver)
        self.b_pdf.clicked.connect(partial(self.exportar, "PDF"))
        self.b_csv.clicked.connect(partial(self.exportar, "CSV"))
        self.b_predecir.clicked.connect(self.calcular_predicciones)

        self.titulo = QLabel("")
        self.titulo.setProperty("rol", "titulo")
        self.resultado = tabla(("",))
        self.notas = QLabel("")
        self.notas.setWordWrap(True)
        self.grafico = _grafico()

        self.historial = tabla(COL_HISTORIAL)
        self.historial.itemSelectionChanged.connect(self.actualizar_botones)
        self.b_abrir = QPushButton("Abrir")
        self.b_repetir = QPushButton("Generar de nuevo")
        self.b_abrir.clicked.connect(self.abrir)
        self.b_repetir.clicked.connect(self.repetir)

        filtros = QHBoxLayout()
        filtros.addWidget(QLabel("Reporte:"))
        filtros.addWidget(self.tipo, 2)
        for clave, widgets in (
            ("rango", (self.desde, self.hasta, self.sin_rango)),
            ("cliente", (self.cliente,)),
            ("dimension", (self.dimension,)),
            ("segun", (self.segun,)),
        ):
            filtros.addWidget(self.etiquetas[clave])
            for w in widgets:
                filtros.addWidget(w)
        acciones = QHBoxLayout()
        for b in (self.b_ver, self.b_pdf, self.b_csv, self.b_predecir):
            acciones.addWidget(b)
        acciones.addStretch()
        arriba = QWidget()
        capa = QVBoxLayout(arriba)
        capa.setContentsMargins(0, 0, 0, 0)
        capa.addWidget(self.titulo)
        fila = QSplitter()
        fila.addWidget(self.resultado)
        if self.grafico is not None:
            fila.addWidget(self.grafico)
            fila.setSizes([700, 400])
        capa.addWidget(fila, 1)
        capa.addWidget(self.notas)
        abajo = QWidget()
        capa2 = QVBoxLayout(abajo)
        capa2.setContentsMargins(0, 0, 0, 0)
        capa2.addWidget(QLabel("Reportes generados (los suyos; el administrador ve todos):"))
        capa2.addWidget(self.historial)
        botones = QHBoxLayout()
        botones.addWidget(self.b_abrir)
        botones.addWidget(self.b_repetir)
        botones.addStretch()
        capa2.addLayout(botones)
        partes = QSplitter(Qt.Orientation.Vertical)
        partes.addWidget(arriba)
        partes.addWidget(abajo)
        partes.setSizes([480, 220])
        layout = QVBoxLayout(self)
        layout.addLayout(filtros)
        layout.addLayout(acciones)
        layout.addWidget(partes, 1)
        layout.addWidget(self.estado)
        self._al_cambiar_tipo()

    # ------------------------------------------------------------------ carga
    def recargar(self) -> None:
        self.ejecutar(self._rep.tipos, self._mostrar_tipos, self.sesion_id)
        self.ejecutar(self._rep.reportes, self._mostrar_historial, self.sesion_id)
        if self._p.es_interno and self._p.tiene_permiso(*PERMISOS_CONSULTA_TERCEROS):
            self.ejecutar(
                partial(self._terceros.listar, self.sesion_id, rol="CLIENTE"), self._mostrar_clientes
            )

    def _mostrar_tipos(self, tipos: list[Any]) -> None:
        actual = self.tipo.currentData()
        self._tipos = {t.codigo: t for t in tipos}
        self.tipo.blockSignals(True)
        self.tipo.clear()
        for t in tipos:
            self.tipo.addItem(f"{t.grupo} · {t.titulo}", t.codigo)
        self.tipo.setCurrentIndex(max(0, self.tipo.findData(actual)))  # el código es texto: findData sirve
        self.tipo.blockSignals(False)
        self._al_cambiar_tipo()
        if not tipos:
            self._set_estado("Su perfil no tiene reportes habilitados.", "error")

    def _mostrar_clientes(self, terceros: list[Any]) -> None:
        self.cliente.clear()
        self.cliente.addItem("Todos los clientes", None)
        for t in terceros:
            self.cliente.addItem(t.razon_social, t.id)

    def _mostrar_historial(self, reportes: list[Any]) -> None:
        self._historial = {r.id: r for r in reportes}
        nombres = {codigo: t.titulo for codigo, t in self._tipos.items()}
        llenar_tabla(
            self.historial,
            [
                (
                    fecha_local(r.solicitado_en),
                    nombres.get(r.tipo, r.tipo),
                    r.formato,
                    r.estado,
                    r.archivo_clave or r.error_codigo or "",
                    r.solicitado_por,
                )
                for r in reportes
            ],
            [r.id for r in reportes],
        )

    def _al_cambiar_tipo(self) -> None:
        t = self._tipos.get(self.tipo.currentData())
        usa = t.usa if t else frozenset()
        visibles = {
            "rango": (self.desde, self.hasta, self.sin_rango),
            "cliente": (self.cliente,),
            "dimension": (self.dimension,),
            "segun": (self.segun,),
        }
        for clave, widgets in visibles.items():
            mostrar = clave in usa and (
                clave != "cliente" or self._p.es_interno
            )  # un cliente ve solo lo suyo
            self.etiquetas[clave].setVisible(mostrar)
            for w in widgets:
                w.setVisible(mostrar)
        self.actualizar_botones()

    # ------------------------------------------------------------------ acciones
    def parametros(self) -> Parametros:
        usa = self._tipos[self.tipo.currentData()].usa
        rango = "rango" in usa and not self.sin_rango.isChecked()
        return Parametros(
            desde=self.desde.date().toPython() if rango else None,
            hasta=self.hasta.date().toPython() if rango else None,
            cliente_id=self.cliente.currentData() if "cliente" in usa and self._p.es_interno else None,
            dimension=self.dimension.currentData(),
            segun=self.segun.currentData(),
        )

    def ver(self) -> None:
        if self.tipo.currentData() is None:
            return
        try:
            parametros = self.parametros().normalizado()  # misma validación que el servicio, antes de enviar
        except LogisticaError as exc:
            self._set_estado(exc.user_message, "error")
            return
        self.ejecutar(
            self._rep.consultar, self._mostrar_tabla, self.sesion_id, self.tipo.currentData(), parametros
        )

    def _mostrar_tabla(self, t: Tabla) -> None:
        self._tabla_actual = t
        self.titulo.setText(t.titulo)
        self.resultado.setColumnCount(len(t.columnas))
        self.resultado.setHorizontalHeaderLabels(list(t.columnas))
        llenar_tabla(
            self.resultado, [[celda(v) for v in fila] for fila in t.filas], list(range(len(t.filas)))
        )
        self.notas.setText("\n".join(t.notas))
        self._dibujar(t)
        self._set_estado(
            f"{len(t.filas)} fila(s)." if t.filas else "Sin datos para los filtros elegidos.", "ok"
        )

    def _dibujar(self, t: Tabla) -> None:
        if self.grafico is None:
            return
        self.grafico.clear()
        if t.grafico is None:
            self.grafico.setVisible(False)
            return
        puntos = [
            (str(f[0]) if t.columnas[0] != "Puesto" else str(f[1]), f[t.grafico])
            for f in t.filas
            if isinstance(f[t.grafico], (int, Decimal))
        ]
        self.grafico.setVisible(bool(puntos))
        if not puntos:
            return
        import pyqtgraph as pg

        alturas = [float(v) for _, v in puntos]  # solo para dibujar: los valores exactos están en la tabla
        self.grafico.addItem(
            pg.BarGraphItem(x=list(range(len(puntos))), height=alturas, width=0.6, brush="#3b6ea5")
        )
        self.grafico.getAxis("bottom").setTicks([[(i, n[:14]) for i, (n, _) in enumerate(puntos)]])
        self.grafico.setTitle(t.columnas[t.grafico])

    def exportar(self, formato: str) -> None:
        if self.tipo.currentData() is None:
            return
        try:
            parametros = self.parametros().normalizado()
        except LogisticaError as exc:
            self._set_estado(exc.user_message, "error")
            return
        self.ejecutar(
            self._rep.generar, self._generado, self.sesion_id, self.tipo.currentData(), parametros, formato
        )

    def _generado(self, reporte: Any) -> None:
        self._set_estado(f"Reporte generado: {reporte.archivo_clave}", "ok")
        self.ejecutar(self._rep.reportes, self._mostrar_historial, self.sesion_id)

    def abrir(self) -> None:
        reporte_id = clave_seleccionada(self.historial)
        if reporte_id is not None:
            self.ejecutar(self._rep.abrir, self._abrir_archivo, self.sesion_id, reporte_id)

    def _abrir_archivo(self, ruta: Any) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ruta)))  # visor del sistema (PDF) o planilla (CSV)
        self._set_estado(f"Abierto: {ruta}", "ok")

    def repetir(self) -> None:
        reporte_id = clave_seleccionada(self.historial)
        if reporte_id is not None:
            self.ejecutar(self._rep.repetir, self._generado, self.sesion_id, reporte_id)

    def calcular_predicciones(self) -> None:
        self.ejecutar(self._pred.calcular_ahora, self._predicho, self.sesion_id)

    def _predicho(self, r: Any) -> None:
        self._set_estado(f"Predicciones de hoy: {r.calculadas} nuevas, {r.existentes} ya calculadas.", "ok")

    def actualizar_botones(self) -> None:
        libre = not self.ocupado()
        hay_tipo = self.tipo.currentData() is not None
        for b in (self.b_ver, self.b_pdf, self.b_csv):
            b.setEnabled(libre and hay_tipo)
        self.b_predecir.setEnabled(libre)
        elegido = self._historial.get(clave_seleccionada(self.historial))
        self.b_abrir.setEnabled(libre and elegido is not None and elegido.estado == "COMPLETADO")
        self.b_repetir.setEnabled(libre and elegido is not None)


def _grafico() -> Any:
    """Gráfico de barras nativo (pyqtgraph). Si la librería no está, la pantalla funciona sin gráfico."""
    try:
        import pyqtgraph as pg
    except ImportError:  # pragma: no cover - es dependencia declarada, pero no es imprescindible
        return None
    w = pg.PlotWidget()
    w.setBackground("w")
    w.showGrid(y=True, alpha=0.3)
    w.setMinimumWidth(280)
    w.setVisible(False)
    return w
