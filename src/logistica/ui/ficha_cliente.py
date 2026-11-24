"""Ficha unificada de un cliente (desde Terceros): datos, envíos, facturas de venta, cobros y cuenta por cobrar.

Solo el circuito COMO CLIENTE: aunque la empresa también sea operador/proveedor, acá no aparecen sus compras,
pagos ni cuentas por pagar. Cada pestaña usa el servicio que ya valida sesión, permiso y alcance, con un filtro
exacto por cliente aplicado en la consulta a la base. Las pestañas se cargan al abrirlas (en un trabajador) y
una respuesta vieja no pisa una más nueva. Envío y factura se abren con los diálogos existentes, con sus
propios controles y permisos.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from logistica.domain import comercial as dc
from logistica.domain.acceso import Permiso, TerceroDTO
from logistica.domain.logistica import EstadoEnvio
from logistica.ui.comercial_dialogos import abrir_factura
from logistica.ui.envios_view import EnvioDetalleDialog
from logistica.ui.widgets import VistaBase, clave_seleccionada, fecha_local, llenar_tabla, tabla

SIN_INFORMAR = "Sin informar"
_dinero = dc.formato_dinero

DATOS, ENVIOS, FACTURAS, COBROS, CUENTA = "datos", "envios", "facturas", "cobros", "cuenta"


@dataclass(frozen=True)
class ServiciosFicha:
    """Servicios que usa la ficha (los arma la raíz de composición; la UI no crea repositorios)."""

    terceros: Any
    envios: Any
    catalogo: Any
    comercial: Any


def _tiene(permisos: frozenset[str], *codigos: Permiso) -> bool:
    return any(c.value in permisos for c in codigos)


def permisos_ficha(permisos: frozenset[str]) -> dict[str, bool]:
    """Qué secciones puede consultar el usuario: los MISMOS permisos que exige cada servicio (el servicio
    vuelve a validar; esto solo decide qué pestañas se habilitan)."""
    return {
        DATOS: True,  # se abre desde Terceros: TerceroService.obtener exige el permiso de consulta de terceros
        ENVIOS: _tiene(permisos, Permiso.ENVIOS_CONSULTAR),
        FACTURAS: _tiene(permisos, Permiso.CUENTAS_CONSULTAR, Permiso.FACTURAS_EMITIR),
        COBROS: _tiene(permisos, Permiso.CUENTAS_CONSULTAR, Permiso.PAGOS_REGISTRAR),
        CUENTA: _tiene(permisos, Permiso.CUENTAS_CONSULTAR),
    }


def es_cliente(t: TerceroDTO | None) -> bool:
    return t is not None and "CLIENTE" in t.roles_comerciales


def _texto(valor: Any) -> str:
    texto = "" if valor is None else str(valor).strip()
    return texto or SIN_INFORMAR


def _gris(tabla_qt: Any, fila: int) -> None:
    for j in range(tabla_qt.columnCount()):
        item = tabla_qt.item(fila, j)
        if item is not None:
            item.setForeground(QBrush(QColor("#8a8f98")))


class FichaClienteView(VistaBase):
    def __init__(
        self,
        servicios: ServiciosFicha,
        sesion_id: uuid.UUID,
        tercero: TerceroDTO,
        permisos: frozenset[str],
        parent: QWidget | None = None,
    ):
        super().__init__(sesion_id, parent)
        self._s, self._permisos = servicios, permisos
        self.tercero_id = tercero.id  # identificador estable: nunca el texto ni la fila elegida
        self.tercero = tercero
        self.puede = permisos_ficha(permisos)
        self._cargadas: set[str] = set()
        self._fichas: dict[str, int] = dict.fromkeys((DATOS, ENVIOS, FACTURAS, COBROS, CUENTA, "movimientos"), 0)
        self._pendientes: set[str] = set()  # secciones esperando respuesta (para marcar un error)
        self._facturas: dict[uuid.UUID, Any] = {}
        self._cobros: dict[uuid.UUID, Any] = {}
        self._cursor_mov: tuple[datetime, uuid.UUID] | None = None

        self.cabecera = QLabel("")
        self.cabecera.setObjectName("titulo")
        self.subtitulo = QLabel("Circuito como CLIENTE: envíos, facturas de venta, cobros y cuenta por cobrar.")
        self.pestanias = QTabWidget()
        self.mensajes: dict[str, QLabel] = {}
        self._indices: dict[str, int] = {}
        for clave, titulo, armar in (
            (DATOS, "Datos generales", self._armar_datos),
            (ENVIOS, "Envíos", self._armar_envios),
            (FACTURAS, "Facturas de venta", self._armar_facturas),
            (COBROS, "Cobros", self._armar_cobros),
            (CUENTA, "Cuenta corriente", self._armar_cuenta),
        ):
            self.mensajes[clave] = QLabel("")
            self.mensajes[clave].setWordWrap(True)
            self._indices[clave] = self.pestanias.addTab(armar(), titulo)
            if not self.puede[clave]:
                self.pestanias.setTabEnabled(self._indices[clave], False)
                self.pestanias.setTabToolTip(self._indices[clave], "Su usuario no tiene permiso para esta sección.")
        self.pestanias.currentChanged.connect(self._al_cambiar)
        self.boton_recargar = QPushButton("Recargar pestaña")
        self.boton_recargar.clicked.connect(self.recargar)
        sin_permiso = [t for c, t in ((FACTURAS, "facturas"), (COBROS, "cobros"), (CUENTA, "cuenta corriente"),
                                      (ENVIOS, "envíos")) if not self.puede[c]]
        aviso = QLabel("Secciones sin permiso: " + ", ".join(sin_permiso) + "." if sin_permiso else "")
        fila = QHBoxLayout()
        fila.addWidget(self.boton_recargar)
        fila.addWidget(aviso)
        fila.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.cabecera)
        layout.addWidget(self.subtitulo)
        layout.addWidget(self.pestanias)
        layout.addLayout(fila)
        layout.addWidget(self.estado)
        self._mostrar_cabecera(tercero)
        self.actualizar_botones()

    # ------------------------------------------------------------------ armado
    def _armar_datos(self) -> QWidget:
        w = QWidget()
        self.form = QFormLayout()
        self.campos: dict[str, QLabel] = {}
        for clave, etiqueta in (
            ("razon", "Razón social"),
            ("identificacion", "Identificación tributaria"),
            ("pais", "País"),
            ("email", "Email"),
            ("telefono", "Teléfono"),
            ("direccion", "Domicilio"),
            ("estado", "Estado"),
            ("roles", "Roles comerciales"),
        ):
            self.campos[clave] = QLabel("")
            self.campos[clave].setWordWrap(True)
            self.form.addRow(etiqueta + ":", self.campos[clave])
        layout = QVBoxLayout(w)
        layout.addLayout(self.form)
        layout.addWidget(self.mensajes[DATOS])
        layout.addStretch()
        return w

    def _armar_envios(self) -> QWidget:
        w = QWidget()
        self.filtro_estado = QComboBox()
        self.filtro_estado.addItem("Todos los estados", None)
        for e in EstadoEnvio:
            self.filtro_estado.addItem(e.value.replace("_", " ").title(), e.value)
        self.filtro_estado.currentIndexChanged.connect(lambda _i: self.cargar(ENVIOS))
        self.tabla_envios = tabla(
            ("Código", "Origen", "Destino", "Estado", "Creado", "Entrega estimada", "Compromiso", "Entrega real")
        )
        self.tabla_envios.itemSelectionChanged.connect(self.actualizar_botones)
        self.tabla_envios.doubleClicked.connect(self.abrir_envio)
        self.b_abrir_envio = QPushButton("Abrir envío")
        self.b_abrir_envio.clicked.connect(self.abrir_envio)
        filtros = QHBoxLayout()
        filtros.addWidget(QLabel("Estado:"))
        filtros.addWidget(self.filtro_estado)
        filtros.addStretch()
        botones = QHBoxLayout()
        botones.addWidget(self.b_abrir_envio)
        botones.addStretch()
        layout = QVBoxLayout(w)
        layout.addLayout(filtros)
        layout.addWidget(self.tabla_envios)
        layout.addWidget(self.mensajes[ENVIOS])
        layout.addLayout(botones)
        return w

    def _armar_facturas(self) -> QWidget:
        w = QWidget()
        self.tabla_facturas = tabla(
            ("Número", "Emitida", "Envío(s)", "Moneda", "Total", "Aplicado", "Pendiente", "Estado")
        )
        self.tabla_facturas.itemSelectionChanged.connect(self.actualizar_botones)
        self.tabla_facturas.doubleClicked.connect(self.ver_factura)
        self.b_ver_factura = QPushButton("Ver factura / PDF")
        self.b_ver_factura.clicked.connect(self.ver_factura)
        botones = QHBoxLayout()
        botones.addWidget(self.b_ver_factura)
        botones.addStretch()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Solo facturas de VENTA a este cliente. Las anuladas se muestran en gris."))
        layout.addWidget(self.tabla_facturas)
        layout.addWidget(self.mensajes[FACTURAS])
        layout.addLayout(botones)
        return w

    def _armar_cobros(self) -> QWidget:
        w = QWidget()
        self.tabla_cobros = tabla(
            ("Fecha", "Medio", "Moneda", "Importe", "Aplicado", "Disponible", "Estado", "Referencia")
        )
        self.tabla_cobros.itemSelectionChanged.connect(self.actualizar_botones)
        self.b_aplicaciones = QPushButton("Ver aplicaciones")
        self.b_aplicaciones.clicked.connect(self.ver_aplicaciones)
        # El servicio de aplicaciones exige cuentas.consultar o pagos.aplicar.
        self.b_aplicaciones.setVisible(_tiene(self._permisos, Permiso.CUENTAS_CONSULTAR, Permiso.PAGOS_APLICAR))
        botones = QHBoxLayout()
        botones.addWidget(self.b_aplicaciones)
        botones.addStretch()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Solo cobros recibidos de este cliente (no incluye pagos a proveedores). "
                                "Consultar no crea ni aplica cobros."))
        layout.addWidget(self.tabla_cobros)
        layout.addWidget(self.mensajes[COBROS])
        layout.addLayout(botones)
        return w

    def _armar_cuenta(self) -> QWidget:
        w = QWidget()
        self.saldos = QLabel("")
        self.saldos.setWordWrap(True)
        self.tabla_cuentas = tabla(("Moneda", "Saldo", "Último cierre"))
        self.tabla_cuentas.itemSelectionChanged.connect(self._al_elegir_cuenta)
        self.tabla_movimientos = tabla(("Fecha", "Movimiento", "Importe", "Descripción"))
        self.b_mas = QPushButton("Más movimientos")
        self.b_mas.clicked.connect(self.mas_movimientos)
        botones = QHBoxLayout()
        botones.addWidget(self.b_mas)
        botones.addStretch()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Cuentas POR COBRAR del cliente, una por moneda (los saldos no se suman entre "
                                "monedas). El saldo lo calcula el servicio comercial a partir de los movimientos."))
        layout.addWidget(self.saldos)
        layout.addWidget(self.tabla_cuentas, 1)
        layout.addWidget(self.mensajes[CUENTA])
        layout.addWidget(QLabel("Movimientos de la cuenta elegida (más recientes primero, de a 50):"))
        layout.addWidget(self.tabla_movimientos, 2)
        layout.addLayout(botones)
        return w

    # ------------------------------------------------------------------ carga
    def iniciar(self) -> None:
        """Primera carga: datos generales (la pestaña visible)."""
        self.cargar(DATOS)

    def recargar(self) -> None:
        clave = self._clave_actual()
        if clave is not None:
            self.cargar(clave)

    def _clave_actual(self) -> str | None:
        indice = self.pestanias.currentIndex()
        return next((c for c, i in self._indices.items() if i == indice), None)

    def _al_cambiar(self, _indice: int) -> None:
        clave = self._clave_actual()
        if clave is not None and clave not in self._cargadas:
            self.cargar(clave)

    def _invalidar(self, *claves: str) -> None:
        """Esas secciones se vuelven a consultar la próxima vez que se abran."""
        self._cargadas.difference_update(claves)

    def cargar(self, clave: str) -> None:
        if not self.puede[clave]:
            return
        self._fichas[clave] += 1
        ficha = self._fichas[clave]
        self._pendientes.add(clave)
        self.mensajes[clave].setText("Cargando…")
        sid, tid = self.sesion_id, self.tercero_id
        consulta = {
            DATOS: partial(self._s.terceros.obtener, sid, tid),
            ENVIOS: partial(
                self._s.envios.listar, sid, estado=self.filtro_estado.currentData(), cliente_id=tid
            ),
            FACTURAS: partial(self._s.comercial.facturas, sid, sentido=dc.Sentido.VENTA.value, tercero_id=tid),
            COBROS: partial(self._s.comercial.pagos, sid, tercero_id=tid, sentido=dc.SentidoPago.COBRO.value),
            CUENTA: partial(self._s.comercial.cuentas, sid, tercero_id=tid, naturaleza=dc.Naturaleza.COBRAR.value),
        }[clave]
        mostrar = {
            DATOS: self._mostrar_datos,
            ENVIOS: self._mostrar_envios,
            FACTURAS: self._mostrar_facturas,
            COBROS: self._mostrar_cobros,
            CUENTA: self._mostrar_cuentas,
        }[clave]

        def al_llegar(resultado: Any) -> None:
            if ficha != self._fichas[clave]:
                return  # llegó tarde: ya se pidió una consulta más nueva de esta sección
            self._pendientes.discard(clave)
            self._cargadas.add(clave)
            mostrar(resultado)

        self.ejecutar(consulta, al_llegar)

    def _terminar(self, texto: str, estado: str) -> None:
        super()._terminar(texto, estado)
        if estado == "error":  # VistaBase ya mostró el mensaje; además se marca la sección que lo esperaba
            for clave in list(self._pendientes):
                self.mensajes[clave].setText(f"No se pudo cargar: {texto} Use «Recargar pestaña».")
            self._pendientes.clear()

    # ------------------------------------------------------------------ mostrar
    def _mostrar_cabecera(self, t: TerceroDTO) -> None:
        estado = "Activo" if t.activo else "INACTIVO"
        self.cabecera.setText(
            f"Ficha del cliente · {t.razon_social} · {t.tipo_identificacion} {t.numero_identificacion} · {estado}"
        )

    def _mostrar_datos(self, t: TerceroDTO) -> None:
        self.tercero = t
        self._mostrar_cabecera(t)
        self.campos["razon"].setText(_texto(t.razon_social))
        identificacion = f"{t.tipo_identificacion} {t.numero_identificacion}".strip()
        self.campos["identificacion"].setText(_texto(identificacion))
        self.campos["pais"].setText(_texto(t.pais_codigo))
        self.campos["email"].setText(_texto(t.email))
        self.campos["telefono"].setText(_texto(t.telefono))
        self.campos["direccion"].setText(_texto(t.direccion))
        self.campos["estado"].setText("Activo" if t.activo else "Inactivo")
        self.campos["roles"].setText(_texto(", ".join(sorted(t.roles_comerciales))))
        self.mensajes[DATOS].setText(
            "" if es_cliente(t) else "Atención: esta empresa ya no tiene el rol CLIENTE activo; se muestra su historial."
        )

    def _mostrar_envios(self, envios: list[Any]) -> None:
        llenar_tabla(
            self.tabla_envios,
            [
                (e.codigo, e.origen_nombre, e.destino_nombre, e.estado.replace("_", " ").title(),
                 fecha_local(e.creado_en), fecha_local(e.entrega_estimada_en) or "-",
                 fecha_local(e.entrega_comprometida_en) or "-", fecha_local(e.entrega_real_en) or "-")
                for e in envios
            ],
            [e.id for e in envios],
        )
        filtrado = self.filtro_estado.currentData() is not None
        self.mensajes[ENVIOS].setText(
            f"{len(envios)} envío(s)." if envios
            else ("No hay envíos de este cliente en ese estado." if filtrado
                  else "Este cliente todavía no tiene envíos.")
        )

    def _mostrar_facturas(self, facturas: list[Any]) -> None:
        self._facturas = {f.id: f for f in facturas}
        llenar_tabla(
            self.tabla_facturas,
            [
                (f.codigo, fecha_local(f.emitida_en), f.envio_codigos or "-", f.moneda_codigo,
                 _dinero(f.importe_total), _dinero(f.aplicado),
                 _dinero(f.pendiente) if f.estado == "EMITIDA" else "- (anulada)",
                 "ANULADA" if f.estado == "ANULADA" else f.estado)
                for f in facturas
            ],
            [f.id for f in facturas],
        )
        for i, f in enumerate(facturas):
            if f.estado == "ANULADA":
                _gris(self.tabla_facturas, i)
        self.mensajes[FACTURAS].setText(
            f"{len(facturas)} factura(s) de venta." if facturas
            else "Este cliente todavía no tiene facturas de venta (se emiten al entregar un envío con precio)."
        )

    def _mostrar_cobros(self, cobros: list[Any]) -> None:
        self._cobros = {c.id: c for c in cobros}
        llenar_tabla(
            self.tabla_cobros,
            [
                (fecha_local(c.fecha), c.medio.capitalize(), c.moneda_codigo, _dinero(c.monto), _dinero(c.aplicado),
                 _dinero(c.disponible), c.estado, c.referencia or "-")
                for c in cobros
            ],
            [c.id for c in cobros],
        )
        for i, c in enumerate(cobros):
            if c.estado != "CONFIRMADO":
                _gris(self.tabla_cobros, i)
        self.mensajes[COBROS].setText(
            f"{len(cobros)} cobro(s)." if cobros else "Este cliente todavía no tiene cobros registrados."
        )

    def _mostrar_cuentas(self, cuentas: list[Any]) -> None:
        llenar_tabla(
            self.tabla_cuentas,
            [(c.moneda_codigo, _dinero(c.saldo), c.ultimo_cierre.strftime("%m/%Y") if c.ultimo_cierre else "-")
             for c in cuentas],
            [c.id for c in cuentas],
        )
        self.saldos.setText(
            "Saldo por cobrar por moneda: " + "  ·  ".join(f"{c.moneda_codigo} {_dinero(c.saldo)}" for c in cuentas)
            + "   (positivo = nos debe; negativo = saldo a favor del cliente)"
            if cuentas else ""
        )
        self._ficha_movimientos_nueva()
        llenar_tabla(self.tabla_movimientos, [], [])
        self.mensajes[CUENTA].setText(
            "Elegí una cuenta para ver sus movimientos." if cuentas
            else "Este cliente todavía no tiene cuenta por cobrar (se crea con la primera factura o cobro)."
        )
        if len(cuentas) == 1:
            self.tabla_cuentas.selectRow(0)

    # ------------------------------------------------------------------ movimientos (paginados)
    def _ficha_movimientos_nueva(self) -> int:
        self._fichas["movimientos"] += 1
        self._cursor_mov = None
        return self._fichas["movimientos"]

    def _al_elegir_cuenta(self) -> None:
        ficha = self._ficha_movimientos_nueva()
        llenar_tabla(self.tabla_movimientos, [], [])
        cuenta_id = clave_seleccionada(self.tabla_cuentas)
        self.actualizar_botones()
        if cuenta_id is not None:
            self.ejecutar(partial(self._s.comercial.movimientos, self.sesion_id, cuenta_id),
                          lambda pagina: self._agregar_movimientos(ficha, pagina))

    def mas_movimientos(self) -> None:
        cuenta_id = clave_seleccionada(self.tabla_cuentas)
        if cuenta_id is not None and self._cursor_mov is not None:
            ficha = self._fichas["movimientos"]
            self.ejecutar(partial(self._s.comercial.movimientos, self.sesion_id, cuenta_id, antes=self._cursor_mov),
                          lambda pagina: self._agregar_movimientos(ficha, pagina))

    def _agregar_movimientos(self, ficha: int, pagina: Any) -> None:
        if ficha != self._fichas["movimientos"]:
            return  # de otra cuenta o de una recarga anterior
        filas = self.tabla_movimientos.rowCount()
        previas = [[self.tabla_movimientos.item(i, j).text() for j in range(4)] for i in range(filas)]
        claves = [self.tabla_movimientos.item(i, 0).data(Qt.ItemDataRole.UserRole) for i in range(filas)]
        nuevas = [
            (fecha_local(m.fecha),
             "Reversa" if m.reversa_de_id else ("Aumenta" if m.sentido == dc.AUMENTA else "Disminuye"),
             ("+" if m.sentido == dc.AUMENTA else "−") + _dinero(m.importe), m.descripcion)
            for m in pagina.movimientos
        ]
        llenar_tabla(self.tabla_movimientos, previas + nuevas, claves + [m.id for m in pagina.movimientos])
        self._cursor_mov = pagina.siguiente

    # ------------------------------------------------------------------ acciones
    def abrir_envio(self) -> None:
        envio_id = clave_seleccionada(self.tabla_envios)
        if envio_id is None:
            return
        dialogo = EnvioDetalleDialog(
            self, self._s.envios, self._s.catalogo, self._s.terceros, self.sesion_id, envio_id,
            self._permisos, self._s.comercial,
        )
        dialogo.detalle.sesion_vencida.connect(self.sesion_vencida)
        dialogo.exec()
        dialogo.detalle.cerrar()
        dialogo.deleteLater()
        # El detalle pudo cambiar el envío, sus cargos o facturas: se refresca y lo comercial se relee al abrirlo.
        self._invalidar(FACTURAS, COBROS, CUENTA)
        self.cargar(ENVIOS)

    def ver_factura(self) -> None:
        factura_id = clave_seleccionada(self.tabla_facturas)
        if factura_id is not None and abrir_factura(
            self, self._s.comercial, self.sesion_id, factura_id, self._permisos, self.sesion_vencida.emit
        ):
            self._invalidar(COBROS, CUENTA)  # hubo un cobro: se relee al abrir esas pestañas
            self.cargar(FACTURAS)

    def ver_aplicaciones(self) -> None:
        pago_id = clave_seleccionada(self.tabla_cobros)
        if pago_id is not None:
            self.ejecutar(partial(self._s.comercial.aplicaciones, self.sesion_id, pago_id=pago_id),
                          lambda apl: self._mostrar_aplicaciones(pago_id, apl))

    def _mostrar_aplicaciones(self, pago_id: uuid.UUID, aplicaciones: list[Any]) -> None:
        cobro = self._cobros.get(pago_id)
        dialogo = QDialog(self)
        dialogo.setWindowTitle("Aplicaciones del cobro")
        t = tabla(("Factura", "Importe aplicado", "Fecha", "Estado"))
        llenar_tabla(
            t,
            [(a.factura_codigo, _dinero(a.monto_aplicado), fecha_local(a.creado_en),
              "Vigente" if a.anulada_en is None else f"Anulada: {a.motivo_anulacion or ''}")
             for a in aplicaciones],
            [a.id for a in aplicaciones],
        )
        encabezado = (
            f"Cobro del {fecha_local(cobro.fecha)} · {cobro.moneda_codigo} {_dinero(cobro.monto)} · "
            f"disponible {_dinero(cobro.disponible)}" if cobro else "Cobro"
        )
        cerrar = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        cerrar.rejected.connect(dialogo.reject)
        layout = QVBoxLayout(dialogo)
        layout.addWidget(QLabel(encabezado))
        layout.addWidget(t if aplicaciones else QLabel("Este cobro todavía no está aplicado a ninguna factura."))
        layout.addWidget(cerrar)
        dialogo.resize(620, 300)
        dialogo.exec()
        dialogo.deleteLater()

    def actualizar_botones(self) -> None:
        libre = not self.ocupado()
        self.boton_recargar.setEnabled(libre)
        self.filtro_estado.setEnabled(libre)
        self.b_abrir_envio.setEnabled(libre and clave_seleccionada(self.tabla_envios) is not None)
        self.b_ver_factura.setEnabled(libre and clave_seleccionada(self.tabla_facturas) is not None)
        self.b_aplicaciones.setEnabled(libre and clave_seleccionada(self.tabla_cobros) is not None)
        self.b_mas.setEnabled(
            libre and clave_seleccionada(self.tabla_cuentas) is not None and self._cursor_mov is not None
        )


def abrir_ficha_cliente(
    parent: QWidget,
    servicios: ServiciosFicha,
    sesion_id: uuid.UUID,
    tercero: TerceroDTO,
    permisos: frozenset[str],
    sesion_vencida: Any,
) -> None:
    dialogo = QDialog(parent)
    dialogo.setWindowTitle(f"Ficha del cliente · {tercero.razon_social}")
    vista = FichaClienteView(servicios, sesion_id, tercero, permisos, dialogo)
    vista.sesion_vencida.connect(sesion_vencida)
    vista.sesion_vencida.connect(lambda _m: dialogo.reject())
    cerrar = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    cerrar.rejected.connect(dialogo.reject)
    layout = QVBoxLayout(dialogo)
    layout.addWidget(vista)
    layout.addWidget(cerrar)
    dialogo.resize(1120, 720)
    vista.iniciar()
    dialogo.exec()
    vista.cerrar()  # no acepta trabajo nuevo y espera lo que estaba en curso: no se tocan widgets destruidos
    dialogo.deleteLater()
