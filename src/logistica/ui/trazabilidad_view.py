"""Pantalla de trazabilidad de envíos y contenedores."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from logistica.core.errors import ValidationError
from logistica.domain.logistica_dtos import EnvioDTO, EnvioDetalleDTO
from logistica.domain.trazabilidad_dtos import (
    AsignacionContenedorDTO,
    EventoCursor,
    PaginaEventosDTO,
    PosicionContenedorDTO,
    RedTrazabilidadDTO,
    RecorridoContenedorDTO,
    RutaTrazabilidadDTO,
)
from logistica.services.envio_service import EnvioService
from logistica.services.trazabilidad_service import TrazabilidadService
from logistica.ui.widgets import VistaBase, clave_seleccionada, fecha_local, llenar_tabla, tabla

TIPOS_EVENTO = (
    "ADUANA",
    "CARGA_INICIADA",
    "DESCARGA",
    "EN_TRANSITO",
    "ENTREGADO",
    "INCIDENTE_ABIERTO",
    "INCIDENTE_RESUELTO",
    "RETRASO",
    "TRAMO_FINALIZADO",
    "TRAMO_INICIADO",
    "CANCELADO",
)


class VistaRed(QGraphicsView):
    ESCALA_MINIMA = 0.5
    ESCALA_MAXIMA = 2.5
    FACTOR_ZOOM = 1.15

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setToolTip("Rueda: desplazar · Ctrl+rueda: zoom · Arrastrar: mover el grafo")

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().wheelEvent(event)
            return
        pasos = event.angleDelta().y() / 120
        if pasos == 0:
            event.ignore()
            return
        escala_actual = self.transform().m11()
        escala_nueva = min(
            self.ESCALA_MAXIMA,
            max(self.ESCALA_MINIMA, escala_actual * self.FACTOR_ZOOM**pasos),
        )
        factor = escala_nueva / escala_actual
        self.scale(factor, factor)
        event.accept()


class TrazabilidadView(VistaBase):
    def __init__(
        self,
        trazabilidad: TrazabilidadService,
        envios: EnvioService,
        sesion_id: uuid.UUID,
        parent: QWidget | None = None,
    ):
        super().__init__(sesion_id, parent)
        self._trazabilidad, self._envios = trazabilidad, envios
        self._asignaciones: dict[uuid.UUID, AsignacionContenedorDTO] = {}
        self._contenedor_id: uuid.UUID | None = None
        self._cursores_eventos: list[tuple | None] = [None]
        self._indice_eventos = 0
        self._siguiente_eventos: tuple | None = None
        self._cargando_envios = False

        self.selector_envio = QComboBox()
        self.selector_envio.currentIndexChanged.connect(self._cambio_envio)
        self.boton_actualizar_envios = QPushButton("Actualizar envíos")
        self.boton_actualizar_envios.clicked.connect(self.recargar)
        cabecera = QHBoxLayout()
        cabecera.addWidget(QLabel("Envío"))
        cabecera.addWidget(self.selector_envio, 1)
        cabecera.addWidget(self.boton_actualizar_envios)

        self.secciones = QTabWidget()
        self._crear_pestania_eventos()
        self._crear_pestania_ruta()
        self._crear_pestania_contenedor()
        self._crear_pestania_red()
        layout = QVBoxLayout(self)
        layout.addLayout(cabecera)
        layout.addWidget(self.secciones)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    def recargar(self) -> None:
        self.ejecutar(self._envios.listar, self._mostrar_envios, self.sesion_id)

    def _crear_pestania_eventos(self) -> None:
        self.filtro_tipo = QComboBox()
        self.filtro_tipo.addItem("Todos los tipos", None)
        for tipo in TIPOS_EVENTO:
            self.filtro_tipo.addItem(tipo.replace("_", " ").title(), tipo)
        self.filtro_desde = QLineEdit()
        self.filtro_desde.setPlaceholderText("Desde ISO con zona (opcional)")
        self.filtro_hasta = QLineEdit()
        self.filtro_hasta.setPlaceholderText("Hasta exclusivo ISO con zona")
        self.filtro_texto = QLineEdit()
        self.filtro_texto.setPlaceholderText("Texto en descripción")
        self.filtro_tramo = QComboBox()
        self.filtro_tramo.addItem("Todos los tramos", None)
        self.boton_buscar_eventos = QPushButton("Consultar eventos")
        self.boton_buscar_eventos.clicked.connect(self._buscar_eventos_boton)
        filtros = QGridLayout()
        filtros.addWidget(self.filtro_tipo, 0, 0)
        filtros.addWidget(self.filtro_tramo, 0, 1)
        filtros.addWidget(self.filtro_desde, 1, 0)
        filtros.addWidget(self.filtro_hasta, 1, 1)
        filtros.addWidget(self.filtro_texto, 2, 0, 1, 2)
        self.tabla_eventos = tabla(
            ("Ocurrencia", "Registro", "Tipo", "Descripción", "Tramo", "Detalles", "Sincronización")
        )
        self.estado_eventos = QLabel("")
        self.estado_eventos.setWordWrap(True)
        self.boton_anterior_eventos = QPushButton("Anterior")
        self.boton_siguiente_eventos = QPushButton("Siguiente")
        self.boton_anterior_eventos.clicked.connect(self._anterior_eventos)
        self.boton_siguiente_eventos.clicked.connect(self._siguiente_pagina_eventos)
        paginacion = QHBoxLayout()
        paginacion.addWidget(self.boton_anterior_eventos)
        paginacion.addWidget(self.boton_siguiente_eventos)
        paginacion.addStretch()
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        layout.addLayout(filtros)
        layout.addWidget(self.boton_buscar_eventos)
        layout.addWidget(self.tabla_eventos)
        layout.addWidget(self.estado_eventos)
        layout.addLayout(paginacion)
        self.secciones.addTab(pagina, "Línea de tiempo")

    def _crear_pestania_ruta(self) -> None:
        self.boton_consultar_ruta = QPushButton("Consultar ruta proyectada")
        self.boton_consultar_ruta.clicked.connect(self._consultar_ruta)
        self.estado_ruta = QLabel("La ruta se consulta en Neo4j; no se sustituye por datos de PostgreSQL.")
        self.estado_ruta.setWordWrap(True)
        self.tabla_ruta = tabla(
            (
                "Orden",
                "Origen",
                "Destino",
                "Medio",
                "Operador",
                "Estado proyectado",
                "Salida real",
                "Llegada real",
            )
        )
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        layout.addWidget(self.boton_consultar_ruta)
        layout.addWidget(self.estado_ruta)
        layout.addWidget(self.tabla_ruta)
        self.secciones.addTab(pagina, "Ruta del envío")

    def _crear_pestania_contenedor(self) -> None:
        self.codigo_contenedor = QLineEdit()
        self.codigo_contenedor.setPlaceholderText("Código internacional del contenedor")
        self.boton_buscar_contenedor = QPushButton("Consultar recorrido")
        self.boton_buscar_contenedor.clicked.connect(self._buscar_contenedor)
        fila = QHBoxLayout()
        fila.addWidget(self.codigo_contenedor)
        fila.addWidget(self.boton_buscar_contenedor)
        self.tabla_asignaciones = tabla(
            ("Envío", "Cliente", "Estado", "Reserva desde", "Reserva hasta", "Uso desde", "Uso hasta")
        )
        self.tabla_asignaciones.itemSelectionChanged.connect(self._mostrar_tramos_contenedor)
        self.tabla_tramos_contenedor = tabla(
            (
                "Orden",
                "Origen",
                "Destino",
                "Medio",
                "Operador",
                "Planificado en reserva",
                "Realizado dentro del uso",
                "Salida real",
                "Llegada real",
            )
        )
        self.boton_gps = QPushButton("Cargar GPS medido del uso")
        self.boton_gps.clicked.connect(self._cargar_gps)
        self.estado_gps = QLabel(
            "El recorrido planificado y las posiciones GPS medidas se consultan por separado."
        )
        self.estado_gps.setWordWrap(True)
        self.tabla_gps = tabla(("Medición", "Latitud", "Longitud", "Sensor"))
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        layout.addLayout(fila)
        layout.addWidget(self.tabla_asignaciones)
        layout.addWidget(QLabel("Tramos planificados y tramos realizados dentro del período de uso"))
        layout.addWidget(self.tabla_tramos_contenedor)
        layout.addWidget(self.boton_gps)
        layout.addWidget(self.estado_gps)
        layout.addWidget(self.tabla_gps)
        self.secciones.addTab(pagina, "Contenedor")

    def _crear_pestania_red(self) -> None:
        self.filtro_cliente_red = QLineEdit()
        self.filtro_cliente_red.setPlaceholderText("UUID de cliente (opcional)")
        self.filtro_operador_red = QLineEdit()
        self.filtro_operador_red.setPlaceholderText("UUID de operador (opcional)")
        self.filtro_envio_red = QLineEdit()
        self.filtro_envio_red.setPlaceholderText("UUID de envío (opcional)")
        self.limite_red = QComboBox()
        for limite in (25, 50, 100):
            self.limite_red.addItem(str(limite), limite)
        self.boton_buscar_red = QPushButton("Consultar red en Neo4j")
        self.boton_buscar_red.clicked.connect(self._consultar_red)
        filtros = QGridLayout()
        filtros.addWidget(self.filtro_cliente_red, 0, 0)
        filtros.addWidget(self.filtro_operador_red, 0, 1)
        filtros.addWidget(self.filtro_envio_red, 1, 0)
        filtros.addWidget(self.limite_red, 1, 1)
        self.estado_red = QLabel(
            "Se consultan conexiones cliente–envío–operador con alcance de PostgreSQL y límites."
        )
        self.estado_red.setWordWrap(True)
        self.tabla_red = tabla(
            ("Cliente", "Envío", "Tramo", "Operador", "Versión proyectada")
        )
        self.grafo_red = VistaRed()
        self.grafo_red.setMinimumHeight(300)
        self.escena_red = QGraphicsScene(self.grafo_red)
        self.grafo_red.setScene(self.escena_red)
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        layout.addLayout(filtros)
        layout.addWidget(self.boton_buscar_red)
        layout.addWidget(self.estado_red)
        layout.addWidget(self.tabla_red)
        layout.addWidget(self.grafo_red, 1)
        self.secciones.addTab(pagina, "Red")

    def _mostrar_envios(self, envios: list[EnvioDTO]) -> None:
        seleccionado = self.selector_envio.currentData()
        self._cargando_envios = True
        self.selector_envio.clear()
        for envio in envios:
            self.selector_envio.addItem(f"{envio.codigo} · {envio.estado}", envio.id)
        indice = self.selector_envio.findData(seleccionado)
        self.selector_envio.setCurrentIndex(indice if indice >= 0 else (0 if envios else -1))
        self._cargando_envios = False
        self._cambio_envio()

    def _cambio_envio(self) -> None:
        if self._cargando_envios:
            return
        envio_id = self.selector_envio.currentData()
        self.filtro_tramo.clear()
        self.filtro_tramo.addItem("Todos los tramos", None)
        self._cursores_eventos = [None]
        self._indice_eventos = 0
        self._siguiente_eventos = None
        if envio_id is None:
            self.actualizar_botones()
            return
        self.ejecutar(
            partial(self._envios.obtener, self.sesion_id, envio_id),
            self._mostrar_detalle_envio,
        )
        self._buscar_eventos(resetear=True)

    def _mostrar_detalle_envio(self, detalle: EnvioDetalleDTO) -> None:
        if detalle.envio.id != self.selector_envio.currentData():
            return
        self.filtro_tramo.clear()
        self.filtro_tramo.addItem("Todos los tramos", None)
        for tramo in detalle.tramos:
            self.filtro_tramo.addItem(
                f"Tramo {tramo.orden}: {tramo.origen_nombre} → {tramo.destino_nombre}",
                tramo.id,
            )

    def _buscar_eventos(self, *, resetear: bool = False) -> None:
        envio_id = self.selector_envio.currentData()
        if envio_id is None:
            return
        if resetear:
            self._cursores_eventos = [None]
            self._indice_eventos = 0
        consulta = partial(
            _consultar_eventos,
            self._trazabilidad,
            self.sesion_id,
            envio_id,
            self.filtro_tipo.currentData(),
            self.filtro_desde.text(),
            self.filtro_hasta.text(),
            self.filtro_texto.text(),
            self.filtro_tramo.currentData(),
            self._cursores_eventos[self._indice_eventos],
        )
        self.ejecutar(consulta, partial(self._mostrar_eventos_si_actual, envio_id))

    def _buscar_eventos_boton(self) -> None:
        self._buscar_eventos(resetear=True)

    def _mostrar_eventos_si_actual(self, envio_id: uuid.UUID, pagina: PaginaEventosDTO) -> None:
        if self.selector_envio.currentData() == envio_id:
            self._mostrar_eventos(pagina)

    def _mostrar_eventos(self, pagina: PaginaEventosDTO) -> None:
        llenar_tabla(
            self.tabla_eventos,
            [
                (
                    fecha_local(evento.fecha_ocurrencia),
                    fecha_local(evento.fecha_registro),
                    evento.tipo,
                    evento.descripcion or "",
                    str(evento.tramo_id) if evento.tramo_id else "—",
                    json.dumps(evento.detalles, ensure_ascii=False, sort_keys=True),
                    evento.sincronizacion,
                )
                for evento in pagina.eventos
            ],
            [evento.id for evento in pagina.eventos],
        )
        mensaje = (
            f"{len(pagina.eventos)} eventos históricos de MongoDB. "
            f"Outbox Mongo: {pagina.pendientes_mongo} pendientes/en proceso, "
            f"{pagina.fallidas_mongo} fallidos; estos no se muestran como eventos históricos."
        )
        self.estado_eventos.setText(mensaje)
        self._siguiente_eventos = pagina.siguiente
        self.actualizar_botones()

    def _siguiente_pagina_eventos(self) -> None:
        if self._siguiente_eventos is None:
            return
        self._cursores_eventos = self._cursores_eventos[: self._indice_eventos + 1]
        self._cursores_eventos.append(self._siguiente_eventos)
        self._indice_eventos += 1
        self._buscar_eventos()

    def _anterior_eventos(self) -> None:
        if self._indice_eventos == 0:
            return
        self._indice_eventos -= 1
        self._buscar_eventos()

    def _consultar_ruta(self) -> None:
        envio_id = self.selector_envio.currentData()
        if envio_id is not None:
            self.ejecutar(
                partial(self._trazabilidad.ruta, self.sesion_id, envio_id),
                partial(self._mostrar_ruta_si_actual, envio_id),
            )

    def _mostrar_ruta_si_actual(self, envio_id: uuid.UUID, ruta: RutaTrazabilidadDTO) -> None:
        if self.selector_envio.currentData() == envio_id:
            self._mostrar_ruta(ruta)

    def _mostrar_ruta(self, ruta: RutaTrazabilidadDTO) -> None:
        llenar_tabla(
            self.tabla_ruta,
            [
                (
                    tramo.orden,
                    tramo.origen_nombre,
                    tramo.destino_nombre,
                    tramo.medio,
                    tramo.operador_razon_social,
                    tramo.estado,
                    fecha_local(tramo.salida_real_en),
                    fecha_local(tramo.llegada_real_en),
                )
                for tramo in ruta.tramos
            ],
            [tramo.id for tramo in ruta.tramos],
        )
        versiones = (
            f"operativa {ruta.version_operativa} · proyectada {ruta.version_proyectada}"
            if ruta.version_proyectada is not None
            else f"operativa {ruta.version_operativa} · sin versión proyectada"
        )
        self.estado_ruta.setText(
            f"Neo4j: {ruta.estado_proyeccion} ({versiones}). {ruta.nota_catalogo}"
            + (" Se muestran hasta 200 tramos." if ruta.truncada else "")
        )

    def _buscar_contenedor(self) -> None:
        codigo = self.codigo_contenedor.text()
        self.ejecutar(
            partial(self._trazabilidad.recorrido_contenedor, self.sesion_id, codigo),
            self._mostrar_recorrido,
        )

    def _mostrar_recorrido(self, recorrido: RecorridoContenedorDTO) -> None:
        self._contenedor_id = recorrido.contenedor_id
        self._asignaciones = {asignacion.id: asignacion for asignacion in recorrido.asignaciones}
        llenar_tabla(
            self.tabla_asignaciones,
            [
                (
                    asignacion.envio_codigo,
                    asignacion.cliente_razon_social,
                    asignacion.estado,
                    fecha_local(asignacion.reserva_desde),
                    fecha_local(asignacion.reserva_hasta),
                    fecha_local(asignacion.uso_desde),
                    fecha_local(asignacion.uso_hasta),
                )
                for asignacion in recorrido.asignaciones
            ],
            [asignacion.id for asignacion in recorrido.asignaciones],
        )
        self.tabla_tramos_contenedor.setRowCount(0)
        self.tabla_gps.setRowCount(0)
        self.estado_gps.setText(
            f"{len(recorrido.asignaciones)} asignaciones visibles. "
            + ("Listado limitado a 100 asignaciones." if recorrido.truncado else "")
        )
        self.actualizar_botones()

    def _mostrar_tramos_contenedor(self) -> None:
        asignacion_id = clave_seleccionada(self.tabla_asignaciones)
        asignacion = self._asignaciones.get(asignacion_id)
        if asignacion is None:
            self.tabla_tramos_contenedor.setRowCount(0)
            self.actualizar_botones()
            return
        llenar_tabla(
            self.tabla_tramos_contenedor,
            [
                (
                    tramo.orden,
                    tramo.origen_nombre,
                    tramo.destino_nombre,
                    tramo.medio,
                    tramo.operador_razon_social,
                    "Sí" if tramo.planificado_durante_reserva else "No",
                    "Sí" if tramo.realizado_durante_uso else "No",
                    fecha_local(tramo.salida_real_en),
                    fecha_local(tramo.llegada_real_en),
                )
                for tramo in asignacion.tramos
            ],
            [tramo.id for tramo in asignacion.tramos],
        )
        self.actualizar_botones()

    def _cargar_gps(self) -> None:
        asignacion_id = clave_seleccionada(self.tabla_asignaciones)
        if self._contenedor_id is None or asignacion_id is None:
            return
        self.ejecutar(
            partial(
                self._trazabilidad.posiciones_gps,
                self.sesion_id,
                self._contenedor_id,
                asignacion_id,
            ),
            self._mostrar_gps,
        )

    def _mostrar_gps(self, posiciones: list[PosicionContenedorDTO]) -> None:
        llenar_tabla(
            self.tabla_gps,
            [
                (
                    fecha_local(posicion.fecha_medicion),
                    posicion.latitud,
                    posicion.longitud,
                    str(posicion.sensor_id),
                )
                for posicion in posiciones
            ],
            list(range(len(posiciones))),
        )
        self.estado_gps.setText(
            f"{len(posiciones)} posiciones GPS medidas en MongoDB dentro del período de uso "
            "(hasta 200 por asignación). No representan el recorrido planificado."
        )

    def _consultar_red(self) -> None:
        consulta = partial(
            _consultar_red,
            self._trazabilidad,
            self.sesion_id,
            self.filtro_cliente_red.text(),
            self.filtro_operador_red.text(),
            self.filtro_envio_red.text(),
            self.limite_red.currentData(),
        )
        self.ejecutar(consulta, self._mostrar_red)

    def _mostrar_red(self, red: RedTrazabilidadDTO) -> None:
        llenar_tabla(
            self.tabla_red,
            [
                (
                    relacion.cliente_razon_social,
                    relacion.envio_codigo,
                    f"{relacion.tramo_orden} · {relacion.tramo_id}",
                    relacion.operador_razon_social,
                    relacion.envio_version_proyectada,
                )
                for relacion in red.relaciones
            ],
            [relacion.tramo_id for relacion in red.relaciones],
        )
        notas = [f"Neo4j: {red.estado_grafo}."]
        if red.versiones_desfasadas:
            notas.append(f"{red.versiones_desfasadas} versiones difieren de PostgreSQL.")
        if red.truncada:
            notas.append("Resultados limitados por la consulta.")
        if red.envios_omitidos:
            notas.append("Se acotó el conjunto operativo a 500 envíos.")
        notas.append("Los nombres se resuelven desde el catálogo actual de PostgreSQL (D6).")
        self.estado_red.setText(" ".join(notas))
        self._dibujar_red(red)

    def _dibujar_red(self, red: RedTrazabilidadDTO) -> None:
        self.escena_red.clear()
        self.grafo_red.resetTransform()
        clientes: dict[uuid.UUID, str] = {}
        envios: dict[uuid.UUID, str] = {}
        operadores: dict[uuid.UUID, str] = {}
        for relacion in red.relaciones:
            clientes[relacion.cliente_id] = relacion.cliente_razon_social
            envios[relacion.envio_id] = relacion.envio_codigo
            operadores[relacion.operador_id] = relacion.operador_razon_social
        grupos = (clientes, envios, operadores)
        colores = (QColor("#dbeafe"), QColor("#dcfce7"), QColor("#fef3c7"))
        tipos = ("cliente", "envio", "operador")
        ancho_nodo = 280
        separacion_columnas = 360
        separacion_filas = 18
        fuente = QFont()
        fuente.setPointSize(10)
        posiciones: dict[tuple[str, uuid.UUID], tuple[float, float, float]] = {}
        etiquetas: dict[tuple[str, uuid.UUID], QGraphicsTextItem] = {}
        for columna, grupo in enumerate(grupos):
            y = 0.0
            for identificador, nombre in sorted(grupo.items(), key=lambda par: par[1].casefold()):
                clave = (tipos[columna], identificador)
                texto = QGraphicsTextItem(nombre)
                texto.setFont(fuente)
                texto.setTextWidth(ancho_nodo - 24)
                alto = max(54.0, texto.boundingRect().height() + 16)
                x = columna * separacion_columnas
                posiciones[clave] = (x, y, alto)
                etiquetas[clave] = texto
                y += alto + separacion_filas
        for relacion in red.relaciones:
            cliente = posiciones.get(("cliente", relacion.cliente_id))
            envio = posiciones.get(("envio", relacion.envio_id))
            operador = posiciones.get(("operador", relacion.operador_id))
            if cliente and envio:
                linea = self.escena_red.addLine(
                    cliente[0] + ancho_nodo,
                    cliente[1] + cliente[2] / 2,
                    envio[0],
                    envio[1] + envio[2] / 2,
                    QPen(QColor("#64748b"), 1.5),
                )
                linea.setZValue(-2)
            if envio and operador:
                linea = self.escena_red.addLine(
                    envio[0] + ancho_nodo,
                    envio[1] + envio[2] / 2,
                    operador[0],
                    operador[1] + operador[2] / 2,
                    QPen(QColor("#64748b"), 1.5),
                )
                linea.setZValue(-2)
        for columna, (grupo, color) in enumerate(zip(grupos, colores)):
            for identificador, _ in grupo.items():
                clave = (tipos[columna], identificador)
                x, y, alto = posiciones[clave]
                item = self.escena_red.addRect(
                    x, y, ancho_nodo, alto, QPen(Qt.GlobalColor.darkGray), color
                )
                item.setZValue(-1)
                item.setToolTip(str(identificador))
                texto = etiquetas[clave]
                self.escena_red.addItem(texto)
                texto.setPos(x + 12, y + 8)
        if not red.relaciones:
            texto = self.escena_red.addText("No hay conexiones visibles para esos filtros.", fuente)
            texto.setPos(12, 12)
        bounds = self.escena_red.itemsBoundingRect()
        if not bounds.isEmpty():
            self.escena_red.setSceneRect(bounds.adjusted(-16, -16, 16, 16))
        self.grafo_red.horizontalScrollBar().setValue(0)
        self.grafo_red.verticalScrollBar().setValue(0)

    def actualizar_botones(self) -> None:
        ocupado = self.ocupado()
        hay_envio = self.selector_envio.currentData() is not None
        for boton in (
            self.boton_actualizar_envios,
            self.boton_buscar_eventos,
            self.boton_consultar_ruta,
            self.boton_buscar_contenedor,
            self.boton_buscar_red,
        ):
            boton.setEnabled(not ocupado)
        self.selector_envio.setEnabled(not ocupado)
        for campo in (
            self.filtro_tipo,
            self.filtro_tramo,
            self.filtro_desde,
            self.filtro_hasta,
            self.filtro_texto,
            self.codigo_contenedor,
            self.filtro_cliente_red,
            self.filtro_operador_red,
            self.filtro_envio_red,
            self.limite_red,
        ):
            campo.setEnabled(not ocupado)
        self.boton_anterior_eventos.setEnabled(
            not ocupado and self._indice_eventos > 0 and hay_envio
        )
        self.boton_siguiente_eventos.setEnabled(
            not ocupado and self._siguiente_eventos is not None and hay_envio
        )
        self.boton_gps.setEnabled(
            not ocupado
            and self._contenedor_id is not None
            and clave_seleccionada(self.tabla_asignaciones) is not None
        )


def _fecha_iso(valor: str) -> datetime | None:
    texto = valor.strip()
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto)
    except ValueError:
        raise ValidationError(
            "fecha ISO inválida", user_message="Use una fecha ISO 8601 con zona horaria."
        ) from None


def _uuid_opcional(valor: str, etiqueta: str) -> uuid.UUID | None:
    texto = valor.strip()
    if not texto:
        return None
    try:
        return uuid.UUID(texto)
    except ValueError:
        raise ValidationError(
            f"UUID inválido: {etiqueta}", user_message=f"El filtro de {etiqueta} debe ser un UUID."
        ) from None


def _consultar_eventos(
    servicio: TrazabilidadService,
    sesion_id: uuid.UUID,
    envio_id: uuid.UUID,
    tipo: str | None,
    desde: str,
    hasta: str,
    texto: str,
    tramo_id: uuid.UUID | None,
    despues: EventoCursor | None,
) -> PaginaEventosDTO:
    return servicio.eventos(
        sesion_id,
        envio_id,
        tipo=tipo,
        desde=_fecha_iso(desde),
        hasta=_fecha_iso(hasta),
        texto=texto,
        tramo_id=tramo_id,
        despues=despues,
        limite=50,
    )


def _consultar_red(
    servicio: TrazabilidadService,
    sesion_id: uuid.UUID,
    cliente: str,
    operador: str,
    envio: str,
    limite: int,
) -> RedTrazabilidadDTO:
    return servicio.red(
        sesion_id,
        cliente_id=_uuid_opcional(cliente, "cliente"),
        operador_id=_uuid_opcional(operador, "operador"),
        envio_id=_uuid_opcional(envio, "envío"),
        limite=limite,
    )
