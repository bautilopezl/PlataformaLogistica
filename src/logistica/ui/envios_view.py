"""Pestaña "Envíos": listado y detalle con el circuito completo (AGENTS.md §10.1).

Los botones visibles dependen de los permisos del usuario solo por comodidad: el servicio vuelve a validar
sesión, permiso, alcance y reglas en cada operación.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from functools import partial
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from logistica.domain.logistica import (
    EVENTOS_MANUALES,
    ETAPAS_ADUANA,
    AsignacionDatos,
    EnvioDatos,
    EstadoEnvio,
    Medio,
    Prioridad,
    TramoPlan,
    validar_detalles_evento,
)
from logistica.domain.logistica_dtos import EnvioDetalleDTO, EnvioDTO
from logistica.services.catalogo_service import CatalogoService
from logistica.services.envio_service import EnvioService
from logistica.services.tercero_service import TerceroService
from logistica.ui.widgets import Campo, VistaBase, clave_seleccionada, fecha_local, llenar_tabla, pedir, tabla


def _op(enum) -> list[tuple[str, str]]:
    return [(e.value.replace("_", " ").title(), e.value) for e in enum]


# =========================================================================== listado
class EnviosView(VistaBase):
    COLUMNAS = (
        "Código",
        "Cliente",
        "Recorrido",
        "Estado",
        "Prioridad",
        "Peso (kg)",
        "Entrega estimada",
        "Compromiso",
    )

    def __init__(
        self,
        envios: EnvioService,
        catalogo: CatalogoService,
        terceros: TerceroService,
        sesion_id: uuid.UUID,
        permisos: frozenset[str],
        parent: QWidget | None = None,
    ):
        super().__init__(sesion_id, parent)
        self._envios, self._catalogo, self._terceros, self._permisos = envios, catalogo, terceros, permisos
        self._lista: dict[uuid.UUID, EnvioDTO] = {}
        self.filtro_estado = QComboBox()
        self.filtro_estado.addItem("Todos los estados", None)
        for texto, valor in _op(EstadoEnvio):
            self.filtro_estado.addItem(texto, valor)
        self.buscar = QLineEdit()
        self.buscar.setPlaceholderText("Código o cliente")
        self.buscar.returnPressed.connect(self.recargar)
        self.boton_buscar = QPushButton("Buscar")
        self.boton_buscar.clicked.connect(self.recargar)
        self.boton_nuevo = QPushButton("Nuevo envío")
        self.boton_nuevo.clicked.connect(self.nuevo)
        self.boton_nuevo.setVisible("envios.crear" in permisos)
        self.boton_abrir = QPushButton("Abrir")
        self.boton_abrir.clicked.connect(self.abrir)
        self.tabla = tabla(self.COLUMNAS)
        self.tabla.itemSelectionChanged.connect(self.actualizar_botones)
        self.tabla.doubleClicked.connect(self.abrir)

        filtros = QHBoxLayout()
        for w in (self.filtro_estado, self.buscar, self.boton_buscar):
            filtros.addWidget(w)
        acciones = QHBoxLayout()
        for w in (self.boton_nuevo, self.boton_abrir):
            acciones.addWidget(w)
        acciones.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(filtros)
        layout.addWidget(self.tabla)
        layout.addLayout(acciones)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    def recargar(self) -> None:
        consulta = partial(
            self._envios.listar,
            self.sesion_id,
            estado=self.filtro_estado.currentData(),
            texto=self.buscar.text(),
        )
        self.ejecutar(consulta, self._mostrar)

    def _mostrar(self, envios: list[EnvioDTO]) -> None:
        self._lista = {e.id: e for e in envios}
        filas = [
            (
                e.codigo,
                e.cliente_razon_social,
                f"{e.origen_nombre} → {e.destino_nombre}",
                e.estado,
                e.prioridad,
                e.peso_kg,
                fecha_local(e.entrega_estimada_en),
                fecha_local(e.entrega_comprometida_en),
            )
            for e in envios
        ]
        llenar_tabla(self.tabla, filas, [e.id for e in envios])

    def nuevo(self) -> None:
        def datos(sid):  # corre en el trabajador: solo llama a servicios
            return (self._terceros.listar(sid, rol="CLIENTE"), self._catalogo.listar_ubicaciones(sid))

        self.ejecutar(datos, self._abrir_alta, self.sesion_id)

    def _abrir_alta(self, datos) -> None:
        clientes, ubicaciones = datos
        lugares = [(f"{u.nombre} ({u.ciudad}, {u.pais_codigo})", u.id) for u in ubicaciones]
        campos = [
            Campo("cliente", "Cliente", "opciones", opciones=[(c.razon_social, c.id) for c in clientes]),
            Campo("origen", "Origen", "opciones", opciones=lugares),
            Campo("destino", "Destino", "opciones", opciones=lugares),
            Campo("carga", "Descripción de la carga", obligatorio=True),
            Campo("peso", "Peso (kg)", obligatorio=True),
            Campo("volumen", "Volumen (m³)", obligatorio=True),
            Campo("prioridad", "Prioridad", "opciones", opciones=_op(Prioridad), valor="NORMAL"),
        ]
        armar = lambda v: EnvioDatos(
            v["cliente"],
            v["origen"],
            v["destino"],
            v["carga"],
            v["peso"],  # noqa: E731
            v["volumen"],
            v["prioridad"],
        )
        v = pedir(self, "Nuevo envío", campos, lambda v: armar(v).normalizado())
        if v:
            self.ejecutar(self._envios.crear, self._creado, self.sesion_id, armar(v))

    def _creado(self, envio: EnvioDTO) -> None:
        self.recargar()
        self._abrir_detalle(envio.id)

    def abrir(self) -> None:
        clave = clave_seleccionada(self.tabla)
        if clave:
            self._abrir_detalle(clave)

    def _abrir_detalle(self, envio_id: uuid.UUID) -> None:
        dialogo = EnvioDetalleDialog(
            self, self._envios, self._catalogo, self._terceros, self.sesion_id, envio_id, self._permisos
        )
        dialogo.detalle.sesion_vencida.connect(self.sesion_vencida)
        dialogo.exec()
        dialogo.detalle.cerrar()
        dialogo.deleteLater()
        self.recargar()

    def actualizar_botones(self) -> None:
        libre = not self.ocupado()
        self.boton_buscar.setEnabled(libre)
        self.boton_nuevo.setEnabled(libre)
        self.boton_abrir.setEnabled(libre and clave_seleccionada(self.tabla) is not None)


# =========================================================================== detalle
class EnvioDetalleDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        envios: EnvioService,
        catalogo: CatalogoService,
        terceros: TerceroService,
        sesion_id: uuid.UUID,
        envio_id: uuid.UUID,
        permisos: frozenset[str],
    ):
        super().__init__(parent)
        self.setWindowTitle("Envío")
        self.detalle = EnvioDetalleView(envios, catalogo, terceros, sesion_id, envio_id, permisos, self)
        self.detalle.sesion_vencida.connect(lambda _m: self.reject())
        QVBoxLayout(self).addWidget(self.detalle)
        self.resize(1000, 640)
        self.detalle.recargar()


class EnvioDetalleView(VistaBase):
    def __init__(
        self,
        envios: EnvioService,
        catalogo: CatalogoService,
        terceros: TerceroService,
        sesion_id: uuid.UUID,
        envio_id: uuid.UUID,
        permisos: frozenset[str],
        parent: QWidget | None = None,
    ):
        super().__init__(sesion_id, parent)
        self._envios, self._catalogo, self._terceros, self.envio_id = envios, catalogo, terceros, envio_id
        self.det: EnvioDetalleDTO | None = None
        self.cabecera = QLabel("Cargando…")
        self.cabecera.setObjectName("titulo")
        self.datos = QLabel("")
        self.datos.setWordWrap(True)
        self.tramos = tabla(
            (
                "#",
                "Origen",
                "Destino",
                "Medio",
                "Operador",
                "Vehículo",
                "Estado",
                "Salida est.",
                "Llegada est.",
                "Salida real",
                "Llegada real",
                "Costo",
            )
        )
        self.tramos.itemSelectionChanged.connect(self.actualizar_botones)
        self.asignaciones = tabla(
            (
                "Contenedor",
                "Estado",
                "Peso (kg)",
                "Volumen (m³)",
                "Reserva desde",
                "Reserva hasta",
                "Uso desde",
                "Uso hasta",
            )
        )
        self.eventos = tabla(("Versión", "Evento", "Tipo", "Registrado", "Sincronización"))

        planificar, ejecutar = "envios.planificar" in permisos, "tramos.actualizar" in permisos
        self.botones: dict[str, QPushButton] = {}
        grilla = QGridLayout()
        acciones = [
            ("tramos", "Definir tramos", self.definir_tramos, planificar),
            ("contenedor", "Asignar contenedor", self.asignar_contenedor, planificar),
            ("vehiculo", "Asignar vehículo al tramo", self.asignar_vehiculo, planificar),
            ("planificar", "Planificar", self.planificar, planificar),
            ("iniciar", "Iniciar tramo", self.iniciar_tramo, ejecutar),
            ("finalizar", "Finalizar tramo", self.finalizar_tramo, ejecutar),
            ("evento", "Registrar evento", self.registrar_evento, ejecutar),
            ("entregar", "Confirmar entrega", self.confirmar_entrega, planificar),
            ("cancelar", "Cancelar envío", self.cancelar, planificar),
        ]
        for i, (clave, texto, accion, visible) in enumerate(acciones):
            b = QPushButton(texto)
            b.clicked.connect(accion)
            b.setVisible(visible)
            self.botones[clave] = b
            grilla.addWidget(b, i // 5, i % 5)

        layout = QVBoxLayout(self)
        layout.addWidget(self.cabecera)
        layout.addWidget(self.datos)
        layout.addWidget(QLabel("Tramos"))
        layout.addWidget(self.tramos, 3)
        layout.addWidget(QLabel("Contenedores"))
        layout.addWidget(self.asignaciones, 2)
        layout.addWidget(QLabel("Eventos registrados (se propagan a Mongo y Neo4j en la fase 3)"))
        layout.addWidget(self.eventos, 2)
        layout.addLayout(grilla)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    # --- carga --------------------------------------------------------------
    def recargar(self) -> None:
        def datos(sid, eid):
            return self._envios.obtener(sid, eid), self._envios.eventos(sid, eid)

        self.ejecutar(datos, self._mostrar, self.sesion_id, self.envio_id)

    def _mostrar(self, datos) -> None:
        self.det, eventos = datos
        e = self.det.envio
        self.cabecera.setText(f"{e.codigo} · {e.estado.replace('_', ' ').title()}")
        self.datos.setText(
            f"Cliente: {e.cliente_razon_social}   ·   {e.origen_nombre} → {e.destino_nombre}   ·   "
            f"{e.descripcion_carga}   ·   {e.peso_kg} kg / {e.volumen_m3} m³   ·   Prioridad "
            f"{e.prioridad.lower()}\n"
            f"Entrega estimada: {fecha_local(e.entrega_estimada_en) or '—'}   ·   "
            f"Compromiso: {fecha_local(e.entrega_comprometida_en) or '—'}   ·   "
            f"Entrega real: {fecha_local(e.entrega_real_en) or '—'}"
        )
        llenar_tabla(
            self.tramos,
            [
                (
                    t.orden,
                    t.origen_nombre,
                    t.destino_nombre,
                    t.medio,
                    t.operador_razon_social,
                    t.vehiculo_identificacion or "(sin asignar)",
                    t.estado,
                    fecha_local(t.salida_estimada_en),
                    fecha_local(t.llegada_estimada_en),
                    fecha_local(t.salida_real_en),
                    fecha_local(t.llegada_real_en),
                    f"{t.costo} {t.moneda_codigo}" if t.costo is not None else "",
                )
                for t in self.det.tramos
            ],
            [t.id for t in self.det.tramos],
        )
        llenar_tabla(
            self.asignaciones,
            [
                (
                    a.contenedor_codigo,
                    a.estado,
                    a.peso_asignado_kg,
                    a.volumen_asignado_m3,
                    fecha_local(a.reserva_desde),
                    fecha_local(a.reserva_hasta),
                    fecha_local(a.uso_desde),
                    fecha_local(a.uso_hasta),
                )
                for a in self.det.asignaciones
            ],
            [a.id for a in self.det.asignaciones],
        )
        llenar_tabla(
            self.eventos,
            [
                (
                    ev.agregado_version,
                    ev.evento_tipo,
                    ev.tipo_logistico or "",
                    fecha_local(ev.creado_en),
                    "pendiente" if ev.pendiente else "entregado",
                )
                for ev in eventos
            ],
            [ev.id for ev in eventos],
        )

    def _tramo_seleccionado(self):
        clave = clave_seleccionada(self.tramos)
        return next((t for t in self.det.tramos if t.id == clave), None) if self.det and clave else None

    def _hecho(self, _resultado: Any) -> None:
        self.recargar()

    # --- planificación ------------------------------------------------------
    def definir_tramos(self) -> None:
        def datos(sid):
            return (
                self._catalogo.listar_ubicaciones(sid),
                self._catalogo.listar_vehiculos(sid),
                self._operadores(sid),
            )

        self.ejecutar(datos, self._abrir_tramos, self.sesion_id)

    def _operadores(self, sid):
        return [(t.id, t.razon_social) for t in self._terceros.listar(sid, rol="OPERADOR")]

    def _abrir_tramos(self, datos) -> None:
        ubicaciones, vehiculos, operadores = datos
        dialogo = TramosDialog(self, self.det, ubicaciones, vehiculos, operadores)
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        planes = dialogo.planes
        dialogo.deleteLater()
        if aceptado and planes:
            self.ejecutar(self._envios.definir_tramos, self._hecho, self.sesion_id, self.envio_id, planes)

    def asignar_contenedor(self) -> None:
        self.ejecutar(self._catalogo.listar_contenedores, self._abrir_contenedor, self.sesion_id)

    def _abrir_contenedor(self, contenedores) -> None:
        e = self.det.envio
        desde = self.det.tramos[0].salida_estimada_en if self.det.tramos else None
        hasta = (self.det.tramos[-1].llegada_estimada_en + timedelta(days=1)) if self.det.tramos else None
        campos = [
            Campo(
                "contenedor",
                "Contenedor",
                "opciones",
                opciones=[
                    (f"{c.codigo_internacional} · {c.tipo} · {c.capacidad_kg} kg / {c.capacidad_m3} m³", c.id)
                    for c in contenedores
                    if c.estado == "HABILITADO"
                ],
            ),
            Campo("peso", "Peso asignado (kg)", valor=e.peso_kg, obligatorio=True),
            Campo("volumen", "Volumen asignado (m³)", valor=e.volumen_m3, obligatorio=True),
            Campo("desde", "Reserva desde", "fecha_hora", valor=desde),
            Campo("hasta", "Reserva hasta", "fecha_hora", valor=hasta),
        ]
        def armar(v):
            return AsignacionDatos(v["contenedor"], v["peso"], v["volumen"], v["desde"], v["hasta"])

        v = pedir(self, "Asignar contenedor", campos, lambda v: armar(v).normalizado())
        if v and v["contenedor"]:
            self.ejecutar(
                self._envios.asignar_contenedor, self._hecho, self.sesion_id, self.envio_id, armar(v)
            )

    def asignar_vehiculo(self) -> None:
        t = self._tramo_seleccionado()
        if t:
            self.ejecutar(
                partial(self._catalogo.listar_vehiculos, self.sesion_id, operador_id=t.operador_id),
                self._abrir_vehiculo,
            )

    def _abrir_vehiculo(self, vehiculos) -> None:
        t = self._tramo_seleccionado()
        if t is None:
            return
        v = pedir(
            self,
            f"Vehículo del tramo {t.orden} ({t.medio.lower()})",
            [
                Campo(
                    "vehiculo",
                    "Vehículo",
                    "opciones",
                    opciones=[(f"{x.tipo} {x.identificacion} ({x.estado})", x.id) for x in vehiculos],
                )
            ],
        )
        if v and v["vehiculo"]:
            self.ejecutar(self._envios.asignar_vehiculo, self._hecho, self.sesion_id, t.id, v["vehiculo"])

    def planificar(self) -> None:
        self.ejecutar(self._envios.planificar, self._hecho, self.sesion_id, self.envio_id)

    # --- ejecución ----------------------------------------------------------
    def iniciar_tramo(self) -> None:
        t = self._tramo_seleccionado()
        v = t and pedir(self, f"Iniciar tramo {t.orden}", [Campo("fecha", "Salida real", "fecha_hora")])
        if v:
            self.ejecutar(self._envios.iniciar_tramo, self._hecho, self.sesion_id, t.id, v["fecha"])

    def finalizar_tramo(self) -> None:
        t = self._tramo_seleccionado()
        v = t and pedir(self, f"Finalizar tramo {t.orden}", [Campo("fecha", "Llegada real", "fecha_hora")])
        if v:
            self.ejecutar(self._envios.finalizar_tramo, self._hecho, self.sesion_id, t.id, v["fecha"])

    def registrar_evento(self) -> None:
        tramos = [("(ninguno)", None)] + [
            (f"Tramo {t.orden}: {t.origen_nombre} → {t.destino_nombre}", t.id)
            for t in (self.det.tramos if self.det else [])
        ]
        campos = [
            Campo(
                "tipo",
                "Tipo",
                "opciones",
                opciones=sorted((t.replace("_", " ").title(), t.value) for t in EVENTOS_MANUALES),
            ),
            Campo("fecha", "Ocurrió", "fecha_hora"),
            Campo("tramo", "Tramo", "opciones", opciones=tramos),
            Campo("motivo", "Motivo (retraso)"),
            Campo("demora", "Demora estimada en minutos (retraso)"),
            Campo(
                "etapa",
                "Etapa (aduana)",
                "opciones",
                opciones=[("(sin etapa)", "")] + [(e.title(), e) for e in sorted(ETAPAS_ADUANA)],
            ),
            Campo("descripcion", "Descripción"),
        ]
        def detalles(v):
            return {"motivo": v["motivo"], "demora_estimada_min": v["demora"], "etapa": v["etapa"]}

        v = pedir(
            self,
            "Registrar evento",
            campos,
            lambda v: validar_detalles_evento(v["tipo"], _solo(v["tipo"], detalles(v))),
        )
        if v:
            self.ejecutar(
                partial(
                    self._envios.registrar_evento,
                    self.sesion_id,
                    self.envio_id,
                    v["tipo"],
                    v["fecha"],
                    _solo(v["tipo"], detalles(v)),
                    tramo_id=v["tramo"],
                    descripcion=v["descripcion"],
                ),
                self._hecho,
            )

    def confirmar_entrega(self) -> None:
        v = pedir(self, "Confirmar entrega", [Campo("fecha", "Entregado el", "fecha_hora")])
        if v:
            self.ejecutar(
                self._envios.confirmar_entrega, self._hecho, self.sesion_id, self.envio_id, v["fecha"]
            )

    def cancelar(self) -> None:
        v = pedir(self, "Cancelar envío", [Campo("motivo", "Motivo", obligatorio=True)])
        if v:
            self.ejecutar(self._envios.cancelar, self._hecho, self.sesion_id, self.envio_id, v["motivo"])

    def actualizar_botones(self) -> None:
        libre = not self.ocupado() and self.det is not None
        estado = self.det.envio.estado if self.det else None
        editable = estado in ("BORRADOR", "PLANIFICADO")
        t = self._tramo_seleccionado()
        habilitar = {
            "tramos": editable,
            "contenedor": editable,
            "planificar": estado == "BORRADOR",
            "vehiculo": t is not None and t.estado == "PENDIENTE",
            "iniciar": t is not None and t.estado == "PENDIENTE" and estado in ("PLANIFICADO", "EN_TRANSITO"),
            "finalizar": t is not None and t.estado == "EN_CURSO",
            "evento": estado in ("PLANIFICADO", "EN_TRANSITO"),
            "entregar": estado == "EN_TRANSITO",
            "cancelar": editable,
        }
        for clave, b in self.botones.items():
            b.setEnabled(libre and habilitar[clave])


def _solo(tipo: str, detalles: dict[str, Any]) -> dict[str, Any]:
    """Del formulario genérico, solo los detalles que corresponden al tipo de evento."""
    if tipo == "RETRASO":
        return {k: detalles[k] for k in ("motivo", "demora_estimada_min")}
    if tipo == "ADUANA":
        return {"etapa": detalles["etapa"]} if detalles["etapa"] else {}
    return {}


# =========================================================================== editor de tramos
class TramosDialog(QDialog):
    """Arma el recorrido tramo a tramo. Cada tramo nuevo sale de donde llega el anterior (continuidad)."""

    def __init__(self, parent: QWidget | None, det: EnvioDetalleDTO, ubicaciones, vehiculos, operadores):
        super().__init__(parent)
        self.setWindowTitle(f"Tramos de {det.envio.codigo}")
        self._det, self._ubicaciones, self._vehiculos, self._operadores = (
            det,
            ubicaciones,
            vehiculos,
            operadores,
        )
        self._nombres = {u.id: u.nombre for u in ubicaciones}
        self.planes: list[TramoPlan] = []
        self.tabla = tabla(("#", "Origen", "Destino", "Medio", "Salida est.", "Llegada est.", "Costo"))
        self.aviso = QLabel(
            f"Recorrido: {det.envio.origen_nombre} → {det.envio.destino_nombre}. "
            "El primer tramo sale del origen y el último llega al destino."
        )
        self.aviso.setWordWrap(True)
        agregar, quitar = QPushButton("Agregar tramo"), QPushButton("Quitar el último")
        agregar.clicked.connect(self.agregar)
        quitar.clicked.connect(self.quitar)
        botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        botones.accepted.connect(self.accept)
        botones.rejected.connect(self.reject)
        fila = QHBoxLayout()
        fila.addWidget(agregar)
        fila.addWidget(quitar)
        fila.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.aviso)
        layout.addWidget(self.tabla)
        layout.addLayout(fila)
        layout.addWidget(botones)
        self.resize(820, 360)

    def agregar(self) -> None:
        origen = self.planes[-1].destino_id if self.planes else self._det.envio.origen_id
        salida = self.planes[-1].llegada_estimada_en if self.planes else None
        lugares = [(f"{u.nombre} ({u.ciudad})", u.id) for u in self._ubicaciones]
        campos = [
            Campo("origen", "Origen", "opciones", opciones=lugares, valor=origen),
            Campo("destino", "Destino", "opciones", opciones=lugares, valor=self._det.envio.destino_id),
            Campo("medio", "Medio", "opciones", opciones=_op(Medio)),
            Campo("operador", "Operador", "opciones", opciones=[(n, i) for i, n in self._operadores]),
            Campo(
                "vehiculo",
                "Vehículo (opcional)",
                "opciones",
                opciones=[("(asignar después)", None)]
                + [
                    (f"{v.operador_razon_social}: {v.tipo} {v.identificacion}", v.id) for v in self._vehiculos
                ],
            ),
            Campo("salida", "Salida estimada", "fecha_hora", valor=salida),
            Campo(
                "llegada",
                "Llegada estimada",
                "fecha_hora",
                valor=(salida + timedelta(hours=12)) if salida else None,
            ),
            Campo("costo", "Costo (opcional)"),
            Campo("moneda", "Moneda (ISO, si hay costo)", valor="USD"),
        ]

        def armar(v):
            return TramoPlan(
                v["origen"],
                v["destino"],
                v["medio"],
                v["operador"],
                v["salida"],
                v["llegada"],
                v["vehiculo"],
                v["costo"] or None,
                v["moneda"] if v["costo"] else None,
            )

        v = pedir(self, f"Tramo {len(self.planes) + 1}", campos, lambda v: armar(v).normalizado())
        if v:
            self.planes.append(armar(v))
            self._mostrar()

    def quitar(self) -> None:
        if self.planes:
            self.planes.pop()
            self._mostrar()

    def _mostrar(self) -> None:
        llenar_tabla(
            self.tabla,
            [
                (
                    i,
                    self._nombres.get(t.origen_id, "?"),
                    self._nombres.get(t.destino_id, "?"),
                    t.medio,
                    fecha_local(t.salida_estimada_en),
                    fecha_local(t.llegada_estimada_en),
                    f"{t.costo} {t.moneda_codigo}" if t.costo else "",
                )
                for i, t in enumerate(self.planes, start=1)
            ],
            list(range(len(self.planes))),
        )
