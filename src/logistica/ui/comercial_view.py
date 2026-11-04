"""Pestaña "Comercial": facturas, cargos, pagos y cuentas corrientes (fase 6).

Internos con permisos comerciales operan; un cliente u operador ve solo su lado (por cobrar / por pagar), en
modo consulta. Ocultar botones es comodidad: el servicio vuelve a validar permiso y alcance en cada operación.
La facturación automática no tiene botón: la emite el proceso de la outbox al confirmarse la entrega.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import partial
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from logistica.domain import comercial as dc
from logistica.domain.acceso import Permiso, Principal
from logistica.domain.comercial_dtos import CuentaDTO, FacturaDTO, PagoDTO
from logistica.ui.widgets import (
    Campo,
    VistaBase,
    clave_seleccionada,
    fecha_local,
    indice_dato,
    llenar_tabla,
    pedir,
    tabla,
)

COL_FACTURAS = (
    "Comprobante",
    "Tercero",
    "Tipo",
    "Emitida",
    "Vence",
    "Moneda",
    "Total",
    "Aplicado",
    "Pendiente",
    "Estado",
)
COL_CARGOS = ("Concepto", "Cantidad", "Precio unitario", "Importe", "Moneda", "Estado", "Creado")
COL_PAGOS = ("Fecha", "Tercero", "Tipo", "Medio", "Moneda", "Monto", "Aplicado", "Disponible", "Estado")
COL_CUENTAS = ("Tercero", "Cuenta", "Moneda", "Saldo", "Último cierre")
COL_MOVIMIENTOS = ("Fecha", "Movimiento", "Importe", "Descripción")
COL_CIERRES = ("Período", "Saldo inicial", "Aumentos", "Disminuciones", "Saldo final", "Cerrado")
NATURALEZA = {"COBRAR": "Por cobrar", "PAGAR": "Por pagar"}
SENTIDO = {"VENTA": "Venta", "COMPRA": "Compra", "COBRO": "Cobro", "PAGO": "Pago"}
MONEDAS = (("USD", "USD"), ("ARS", "ARS"), ("EUR", "EUR"), ("BRL", "BRL"))
MEDIOS = tuple((m.value.capitalize(), m.value) for m in dc.Medio)


def _dinero(valor: Decimal | None) -> str:
    return "" if valor is None else f"{valor:,.2f}"


def _meses_cerrables(cantidad: int = 12) -> list[tuple[str, date]]:
    """Los últimos meses terminados (el mes en curso no se cierra)."""
    hoy = datetime.now(timezone.utc).date().replace(day=1)
    meses, anio, mes = [], hoy.year, hoy.month
    for _ in range(cantidad):
        anio, mes = (anio - 1, 12) if mes == 1 else (anio, mes - 1)
        meses.append((f"{mes:02d}/{anio}", date(anio, mes, 1)))
    return meses


class ComercialView(VistaBase):
    def __init__(
        self, comercial: Any, envios: Any, terceros: Any, principal: Principal, parent: QWidget | None = None
    ):
        super().__init__(principal.sesion_id, parent)
        self._com, self._envios, self._terceros = comercial, envios, terceros
        self._p = principal
        self.puede = {
            "cargos": principal.tiene_permiso(Permiso.CARGOS_GESTIONAR),
            "emitir": principal.tiene_permiso(Permiso.FACTURAS_EMITIR),
            "anular": principal.tiene_permiso(Permiso.FACTURAS_ANULAR),
            "registrar_pago": principal.tiene_permiso(Permiso.PAGOS_REGISTRAR),
            "aplicar": principal.tiene_permiso(Permiso.PAGOS_APLICAR),
            "anular_pago": principal.tiene_permiso(Permiso.PAGOS_ANULAR),
            "cerrar": principal.tiene_permiso(Permiso.CUENTAS_CERRAR),
        }
        self._facturas: dict[uuid.UUID, FacturaDTO] = {}
        self._pagos: dict[uuid.UUID, PagoDTO] = {}
        self._cuentas: dict[uuid.UUID, CuentaDTO] = {}
        self._cursor_mov: tuple[datetime, uuid.UUID] | None = None
        self._clave_pago: uuid.UUID | None = (
            None  # se reutiliza si el registro falla: no duplica al reintentar
        )
        # Ficha del último pedido de facturas / de la cuenta elegida: una respuesta vieja que llega tarde
        # (el usuario cambió de filtro o de cuenta) se descarta en lugar de mezclarse con la actual.
        self._ficha_facturas = 0
        self._ficha_cuenta = 0

        self.pestanias = QTabWidget()
        self.pestanias.addTab(self._armar_facturas(), "Facturas")
        if self.puede["cargos"] or self.puede["emitir"]:
            self.pestanias.addTab(self._armar_cargos(), "Cargos")
        self.pestanias.addTab(self._armar_pagos(), "Pagos")
        self.pestanias.addTab(self._armar_cuentas(), "Cuentas corrientes")
        recargar = QPushButton("Recargar")
        recargar.clicked.connect(self.recargar)
        self.boton_recargar = recargar
        layout = QVBoxLayout(self)
        layout.addWidget(self.pestanias)
        fila = QHBoxLayout()
        fila.addWidget(recargar)
        fila.addWidget(QLabel("Las facturas de venta se emiten solas al confirmarse la entrega del envío."))
        fila.addStretch()
        layout.addLayout(fila)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    # ------------------------------------------------------------------ armado
    def _botonera(self, *botones: tuple[str, Any, bool]) -> tuple[QHBoxLayout, list[QPushButton]]:
        fila, creados = QHBoxLayout(), []
        for texto, accion, visible in botones:
            b = QPushButton(texto)
            b.clicked.connect(accion)
            b.setVisible(visible)
            fila.addWidget(b)
            creados.append(b)
        fila.addStretch()
        return fila, creados

    def _armar_facturas(self) -> QWidget:
        w = QWidget()
        self.filtro_sentido = QComboBox()
        for texto, dato in (("Todas", None), ("Ventas", "VENTA"), ("Compras", "COMPRA")):
            self.filtro_sentido.addItem(texto, dato)
        self.filtro_estado = QComboBox()
        for texto, dato in (("Todos los estados", None), ("Emitidas", "EMITIDA"), ("Anuladas", "ANULADA")):
            self.filtro_estado.addItem(texto, dato)
        self.filtro_sentido.currentIndexChanged.connect(self.recargar_facturas)
        self.filtro_estado.currentIndexChanged.connect(self.recargar_facturas)
        self.tabla_facturas = tabla(COL_FACTURAS)
        self.tabla_facturas.itemSelectionChanged.connect(self.actualizar_botones)
        botones, (self.b_detalle, self.b_anular, self.b_compra) = self._botonera(
            ("Ver detalle", self.ver_factura, True),
            ("Anular factura", self.anular_factura, self.puede["anular"]),
            ("Registrar factura de compra", self.registrar_compra, self.puede["emitir"]),
        )
        filtros = QHBoxLayout()
        filtros.addWidget(self.filtro_sentido)
        filtros.addWidget(self.filtro_estado)
        filtros.addStretch()
        layout = QVBoxLayout(w)
        layout.addLayout(filtros)
        layout.addWidget(self.tabla_facturas)
        layout.addLayout(botones)
        return w

    def _armar_cargos(self) -> QWidget:
        w = QWidget()
        self.envio = QComboBox()
        self.envio.currentIndexChanged.connect(self.recargar_cargos)
        self.tabla_cargos = tabla(COL_CARGOS)
        self.tabla_cargos.itemSelectionChanged.connect(self.actualizar_botones)
        botones, (self.b_cargo, self.b_cancelar_cargo, self.b_facturar) = self._botonera(
            ("Nuevo cargo", self.nuevo_cargo, self.puede["cargos"]),
            ("Cancelar cargo", self.cancelar_cargo, self.puede["cargos"]),
            ("Facturar pendientes", self.facturar_pendientes, self.puede["emitir"]),
        )
        fila = QHBoxLayout()
        fila.addWidget(QLabel("Envío:"))
        fila.addWidget(self.envio, 1)
        layout = QVBoxLayout(w)
        layout.addLayout(fila)
        layout.addWidget(self.tabla_cargos)
        layout.addWidget(QLabel("Facturar a mano: cargos agregados después de la entrega o de reemplazo."))
        layout.addLayout(botones)
        return w

    def _armar_pagos(self) -> QWidget:
        w = QWidget()
        self.tabla_pagos = tabla(COL_PAGOS)
        self.tabla_pagos.itemSelectionChanged.connect(self.actualizar_botones)
        botones, (self.b_pago, self.b_aplicar, self.b_desaplicar, self.b_anular_pago) = self._botonera(
            ("Registrar cobro o pago", self.registrar_pago, self.puede["registrar_pago"]),
            ("Aplicar a factura", self.aplicar_pago, self.puede["aplicar"]),
            ("Anular aplicación", self.anular_aplicacion, self.puede["aplicar"]),
            ("Anular pago", self.anular_pago, self.puede["anular_pago"]),
        )
        layout = QVBoxLayout(w)
        layout.addWidget(self.tabla_pagos)
        layout.addLayout(botones)
        return w

    def _armar_cuentas(self) -> QWidget:
        w = QWidget()
        self.tabla_cuentas = tabla(COL_CUENTAS)
        self.tabla_cuentas.itemSelectionChanged.connect(self._al_elegir_cuenta)
        self.tabla_movimientos = tabla(COL_MOVIMIENTOS)
        self.tabla_cierres = tabla(COL_CIERRES)
        botones, (self.b_mas, self.b_cerrar) = self._botonera(
            ("Más movimientos", self.mas_movimientos, True),
            ("Cerrar período", self.cerrar_periodo, self.puede["cerrar"]),
        )
        layout = QVBoxLayout(w)
        layout.addWidget(self.tabla_cuentas)
        layout.addWidget(QLabel("Movimientos (más recientes primero; el saldo se calcula con ellos):"))
        layout.addWidget(self.tabla_movimientos)
        layout.addLayout(botones)
        layout.addWidget(QLabel("Cierres mensuales:"))
        layout.addWidget(self.tabla_cierres)
        return w

    # ------------------------------------------------------------------ carga
    def recargar(self) -> None:
        self.recargar_facturas()
        self.ejecutar(self._com.pagos, self._mostrar_pagos, self.sesion_id)
        self.ejecutar(self._com.cuentas, self._mostrar_cuentas, self.sesion_id)
        if hasattr(self, "envio"):
            self.ejecutar(self._envios.listar, self._mostrar_envios, self.sesion_id)

    def recargar_facturas(self) -> None:
        self._ficha_facturas += 1
        ficha = self._ficha_facturas
        self.ejecutar(
            partial(
                self._com.facturas,
                self.sesion_id,
                sentido=self.filtro_sentido.currentData(),
                estado=self.filtro_estado.currentData(),
            ),
            lambda r: self._mostrar_facturas(r) if ficha == self._ficha_facturas else None,
        )

    def _mostrar_facturas(self, facturas: list[FacturaDTO]) -> None:
        self._facturas = {f.id: f for f in facturas}
        llenar_tabla(
            self.tabla_facturas,
            [
                (
                    f.codigo,
                    f.tercero_razon_social,
                    SENTIDO[f.sentido],
                    fecha_local(f.emitida_en),
                    fecha_local(f.vence_en),
                    f.moneda_codigo,
                    _dinero(f.importe_total),
                    _dinero(f.aplicado),
                    _dinero(f.pendiente),
                    f.estado,
                )
                for f in facturas
            ],
            [f.id for f in facturas],
        )
        self._set_estado(f"{len(facturas)} factura(s).", "ok")

    def _mostrar_envios(self, envios: list[Any]) -> None:
        actual = self.envio.currentData()
        self.envio.blockSignals(True)
        self.envio.clear()
        for e in envios:
            self.envio.addItem(f"{e.codigo} · {e.estado}", e.id)
        indice = indice_dato(self.envio, actual)
        self.envio.setCurrentIndex(indice if indice >= 0 else (0 if envios else -1))
        self.envio.blockSignals(False)
        self._set_estado(f"{len(envios)} envío(s).", "ok")
        self.recargar_cargos()

    def seleccionar_envio(self, envio_id: uuid.UUID) -> None:
        self.envio.setCurrentIndex(indice_dato(self.envio, envio_id))

    def recargar_cargos(self) -> None:
        envio_id = self.envio.currentData()
        if envio_id is None:
            llenar_tabla(self.tabla_cargos, [], [])
            return
        self.ejecutar(self._com.cargos, self._mostrar_cargos, self.sesion_id, envio_id)

    def _mostrar_cargos(self, cargos: list[Any]) -> None:
        llenar_tabla(
            self.tabla_cargos,
            [
                (
                    c.concepto,
                    c.cantidad,
                    c.precio_unitario,
                    _dinero(c.importe),
                    c.moneda_codigo,
                    c.estado,
                    fecha_local(c.creado_en),
                )
                for c in cargos
            ],
            [c.id for c in cargos],
        )
        self._set_estado(f"{len(cargos)} cargo(s).", "ok")

    def _mostrar_pagos(self, pagos: list[PagoDTO]) -> None:
        self._pagos = {p.id: p for p in pagos}
        llenar_tabla(
            self.tabla_pagos,
            [
                (
                    fecha_local(p.fecha),
                    p.tercero_razon_social,
                    SENTIDO[p.sentido],
                    p.medio,
                    p.moneda_codigo,
                    _dinero(p.monto),
                    _dinero(p.aplicado),
                    _dinero(p.disponible),
                    p.estado,
                )
                for p in pagos
            ],
            [p.id for p in pagos],
        )
        self._set_estado(f"{len(pagos)} pago(s).", "ok")

    def _mostrar_cuentas(self, cuentas: list[CuentaDTO]) -> None:
        self._cuentas = {c.id: c for c in cuentas}
        llenar_tabla(
            self.tabla_cuentas,
            [
                (
                    c.tercero_razon_social,
                    NATURALEZA[c.naturaleza],
                    c.moneda_codigo,
                    _dinero(c.saldo),
                    c.ultimo_cierre.strftime("%m/%Y") if c.ultimo_cierre else "",
                )
                for c in cuentas
            ],
            [c.id for c in cuentas],
        )
        self._set_estado(f"{len(cuentas)} cuenta(s).", "ok")

    def _al_elegir_cuenta(self) -> None:
        self.actualizar_botones()
        cuenta_id = clave_seleccionada(self.tabla_cuentas)
        self._ficha_cuenta += 1
        ficha = self._ficha_cuenta
        self._cursor_mov = None
        llenar_tabla(self.tabla_movimientos, [], [])
        llenar_tabla(self.tabla_cierres, [], [])
        if cuenta_id is not None:
            self.ejecutar(
                partial(self._com.movimientos, self.sesion_id, cuenta_id),
                self._si_vigente(ficha, self._agregar_movimientos),
            )
            self.ejecutar(
                self._com.cierres, self._si_vigente(ficha, self._mostrar_cierres), self.sesion_id, cuenta_id
            )

    def _si_vigente(self, ficha: int, mostrar: Any) -> Any:
        return lambda resultado: mostrar(resultado) if ficha == self._ficha_cuenta else None

    def mas_movimientos(self) -> None:
        cuenta_id = clave_seleccionada(self.tabla_cuentas)
        if cuenta_id is not None and self._cursor_mov is not None:
            self.ejecutar(
                partial(self._com.movimientos, self.sesion_id, cuenta_id, antes=self._cursor_mov),
                self._si_vigente(self._ficha_cuenta, self._agregar_movimientos),
            )

    def _agregar_movimientos(self, pagina: Any) -> None:
        filas = [self.tabla_movimientos.item(i, 0) for i in range(self.tabla_movimientos.rowCount())]
        previas = [
            [self.tabla_movimientos.item(i, j).text() for j in range(len(COL_MOVIMIENTOS))]
            for i in range(len(filas))
        ]
        claves = [x.data(Qt.ItemDataRole.UserRole) for x in filas]
        nuevas = [
            (
                fecha_local(m.fecha),
                "Reversa" if m.reversa_de_id else ("Aumenta" if m.sentido == dc.AUMENTA else "Disminuye"),
                ("+" if m.sentido == dc.AUMENTA else "−") + _dinero(m.importe),
                m.descripcion,
            )
            for m in pagina.movimientos
        ]
        llenar_tabla(self.tabla_movimientos, previas + nuevas, claves + [m.id for m in pagina.movimientos])
        self._cursor_mov = pagina.siguiente
        self._set_estado(f"{self.tabla_movimientos.rowCount()} movimiento(s).", "ok")

    def _mostrar_cierres(self, cierres: list[Any]) -> None:
        llenar_tabla(
            self.tabla_cierres,
            [
                (
                    k.periodo.strftime("%m/%Y"),
                    _dinero(k.saldo_inicial),
                    _dinero(k.total_aumentos),
                    _dinero(k.total_disminuciones),
                    _dinero(k.saldo_final),
                    fecha_local(k.cerrado_en),
                )
                for k in cierres
            ],
            [k.id for k in cierres],
        )
        self._set_estado(f"{len(cierres)} cierre(s).", "ok")

    # ------------------------------------------------------------------ facturas
    def ver_factura(self) -> None:
        factura_id = clave_seleccionada(self.tabla_facturas)
        if factura_id is not None:
            self.ejecutar(self._com.factura, self._mostrar_detalle, self.sesion_id, factura_id)

    def _mostrar_detalle(self, f: FacturaDTO) -> None:
        self._set_estado("Detalle cargado.", "ok")
        renglones = "\n".join(
            f"{r.renglon}. {r.concepto}: {r.cantidad} × {r.precio_unitario} = {_dinero(r.subtotal)}"
            for r in f.renglones
        )
        texto = (
            f"{f.codigo} · {SENTIDO[f.sentido]} · {f.tercero_razon_social}\n\n{renglones}\n\n"
            f"Total {f.moneda_codigo} {_dinero(f.importe_total)} · Aplicado {_dinero(f.aplicado)} · "
            f"Pendiente {_dinero(f.pendiente)}\nEstado: {f.estado}"
        )
        if f.motivo_anulacion:
            texto += f"\nAnulada el {fecha_local(f.anulada_en)}: {f.motivo_anulacion}"
        QMessageBox.information(self, "Factura", texto)

    def anular_factura(self) -> None:
        factura_id = clave_seleccionada(self.tabla_facturas)
        if factura_id is None:
            return
        valores = pedir(
            self,
            "Anular factura",
            [
                Campo("motivo", "Motivo", obligatorio=True),
                Campo(
                    "refacturar",
                    "¿Refacturar?",
                    "opciones",
                    opciones=[
                        ("No: el cobro no correspondía", False),
                        ("Sí: crear cargos de reemplazo para corregir", True),
                    ],
                ),
            ],
            validar=lambda v: dc.exigir_motivo(v["motivo"]),
        )
        if valores is not None:
            self.ejecutar(
                partial(
                    self._com.anular_factura,
                    self.sesion_id,
                    factura_id,
                    valores["motivo"],
                    refacturar=valores["refacturar"],
                ),
                self._anulada,
            )

    def _anulada(self, resultado: tuple[FacturaDTO, list[Any]]) -> None:
        factura, reemplazos = resultado
        self._set_estado(f"Factura {factura.codigo} anulada; {len(reemplazos)} cargo(s) de reemplazo.", "ok")
        self.recargar()

    def registrar_compra(self) -> None:
        self.ejecutar(self._terceros.listar, self._pedir_compra, self.sesion_id)

    def _pedir_compra(self, terceros: list[Any]) -> None:
        self._set_estado("", "ok")
        opciones = [
            (t.razon_social, t.id)
            for t in terceros
            if t.activo and {"OPERADOR", "PROVEEDOR"} & set(t.roles_comerciales)
        ]
        if not opciones:
            self._set_estado("No hay operadores ni proveedores activos.", "error")
            return
        ahora = datetime.now().astimezone()
        valores = pedir(
            self,
            "Factura de compra (la emite el operador o proveedor)",
            [
                Campo("tercero", "Emisor", "opciones", opciones=opciones),
                Campo("serie", "Serie / punto de venta", obligatorio=True),
                Campo("numero", "Número", obligatorio=True),
                Campo("emitida", "Emitida", "fecha_hora", valor=ahora),
                Campo("vence", "Vence", "fecha_hora", valor=ahora),
                Campo("moneda", "Moneda", "opciones", opciones=MONEDAS),
                Campo("concepto", "Concepto", obligatorio=True),
                Campo("cantidad", "Cantidad", valor="1", obligatorio=True),
                Campo("precio", "Precio unitario", obligatorio=True),
            ],
            validar=lambda v: self._compra(v).normalizado(datetime.now(timezone.utc)),
        )
        if valores is not None:
            self.ejecutar(
                self._com.registrar_factura_compra,
                lambda _f: self.recargar(),
                self.sesion_id,
                self._compra(valores),
            )

    @staticmethod
    def _compra(v: dict[str, Any]) -> dc.FacturaCompraDatos:
        return dc.FacturaCompraDatos(
            v["tercero"],
            v["serie"],
            v["numero"],
            v["emitida"],
            v["vence"],
            v["moneda"],
            [dc.Renglon(v["concepto"], v["cantidad"], v["precio"])],
        )

    # ------------------------------------------------------------------ cargos
    def nuevo_cargo(self) -> None:
        envio_id = self.envio.currentData()
        if envio_id is None:
            return
        valores = pedir(
            self,
            "Nuevo cargo (lo que se le cobra al cliente)",
            [
                Campo("concepto", "Concepto", obligatorio=True),
                Campo("cantidad", "Cantidad", valor="1", obligatorio=True),
                Campo("precio", "Precio unitario", obligatorio=True),
                Campo("moneda", "Moneda", "opciones", opciones=MONEDAS),
            ],
            validar=lambda v: self._cargo(v).normalizado(),
        )
        if valores is not None:
            self.ejecutar(
                self._com.crear_cargo,
                lambda _c: self.recargar_cargos(),
                self.sesion_id,
                envio_id,
                self._cargo(valores),
            )

    @staticmethod
    def _cargo(v: dict[str, Any]) -> dc.CargoDatos:
        return dc.CargoDatos(v["concepto"], v["cantidad"], v["precio"], v["moneda"])

    def cancelar_cargo(self) -> None:
        cargo_id = clave_seleccionada(self.tabla_cargos)
        if cargo_id is not None:
            self.ejecutar(
                self._com.cancelar_cargo, lambda _c: self.recargar_cargos(), self.sesion_id, cargo_id
            )

    def facturar_pendientes(self) -> None:
        envio_id = self.envio.currentData()
        if envio_id is not None:
            self.ejecutar(self._com.emitir_factura, self._facturado, self.sesion_id, envio_id)

    def _facturado(self, facturas: list[FacturaDTO]) -> None:
        self._set_estado("Emitida(s): " + ", ".join(f.codigo for f in facturas), "ok")
        self.recargar()

    # ------------------------------------------------------------------ pagos
    def registrar_pago(self) -> None:
        self.ejecutar(self._terceros.listar, self._pedir_pago, self.sesion_id)

    def _pedir_pago(self, terceros: list[Any]) -> None:
        self._set_estado("", "ok")
        opciones = [(t.razon_social, t.id) for t in terceros if t.activo and t.roles_comerciales]
        valores = pedir(
            self,
            "Registrar cobro o pago",
            [
                Campo("tercero", "Tercero", "opciones", opciones=opciones),
                Campo(
                    "sentido",
                    "Tipo",
                    "opciones",
                    opciones=[
                        ("Cobro (nos paga un cliente)", "COBRO"),
                        ("Pago (pagamos a un operador/proveedor)", "PAGO"),
                    ],
                ),
                Campo("fecha", "Fecha", "fecha_hora"),
                Campo("monto", "Monto", obligatorio=True),
                Campo("moneda", "Moneda", "opciones", opciones=MONEDAS),
                Campo("medio", "Medio", "opciones", opciones=MEDIOS),
                Campo("referencia", "Referencia"),
            ],
            validar=lambda v: self._pago(v).normalizado(datetime.now(timezone.utc)),
        )
        if valores is None:
            self._clave_pago = None
            return
        self._clave_pago = self._clave_pago or uuid.uuid4()
        self.ejecutar(
            self._com.registrar_pago,
            self._pago_registrado,
            self.sesion_id,
            self._pago(valores),
            self._clave_pago,
        )

    def _pago_registrado(self, _pago: PagoDTO) -> None:
        self._clave_pago = None
        self._set_estado("Pago registrado.", "ok")
        self.recargar()

    @staticmethod
    def _pago(v: dict[str, Any]) -> dc.PagoDatos:
        return dc.PagoDatos(
            v["tercero"], v["sentido"], v["fecha"], v["monto"], v["moneda"], v["medio"], v["referencia"]
        )

    def aplicar_pago(self) -> None:
        pago = self._pagos.get(clave_seleccionada(self.tabla_pagos))
        if pago is None:
            return
        sentido = dc.Sentido.VENTA if pago.sentido == dc.SentidoPago.COBRO else dc.Sentido.COMPRA
        candidatas = [
            f
            for f in self._facturas.values()
            if f.estado == "EMITIDA"
            and f.pendiente > 0
            and (f.tercero_id, f.moneda_codigo, f.sentido) == (pago.tercero_id, pago.moneda_codigo, sentido)
        ]
        if not candidatas:
            self._set_estado(
                "No hay facturas con saldo de ese tercero y moneda (revise el filtro de facturas).", "error"
            )
            return
        valores = pedir(
            self,
            "Aplicar a factura",
            [
                Campo(
                    "factura",
                    "Factura",
                    "opciones",
                    opciones=[(f"{f.codigo} · debe {_dinero(f.pendiente)}", f.id) for f in candidatas],
                ),
                Campo(
                    "monto",
                    "Monto",
                    valor=str(min(pago.disponible, candidatas[0].pendiente)),
                    obligatorio=True,
                ),
            ],
            validar=lambda v: dc.decimal_de(v["monto"], "el monto", decimales=2),
        )
        if valores is not None:
            self.ejecutar(
                self._com.aplicar_pago,
                lambda _a: self.recargar(),
                self.sesion_id,
                pago.id,
                valores["factura"],
                valores["monto"],
            )

    def anular_aplicacion(self) -> None:
        pago_id = clave_seleccionada(self.tabla_pagos)
        if pago_id is not None:
            self.ejecutar(
                partial(self._com.aplicaciones, self.sesion_id, pago_id=pago_id), self._pedir_desaplicar
            )

    def _pedir_desaplicar(self, aplicaciones: list[Any]) -> None:
        self._set_estado("", "ok")
        vigentes = [a for a in aplicaciones if a.anulada_en is None]
        if not vigentes:
            self._set_estado("El pago no tiene aplicaciones vigentes.", "error")
            return
        valores = pedir(
            self,
            "Anular aplicación",
            [
                Campo(
                    "aplicacion",
                    "Aplicación",
                    "opciones",
                    opciones=[(f"{a.factura_codigo} · {_dinero(a.monto_aplicado)}", a.id) for a in vigentes],
                ),
                Campo("motivo", "Motivo", obligatorio=True),
            ],
            validar=lambda v: dc.exigir_motivo(v["motivo"]),
        )
        if valores is not None:
            self.ejecutar(
                self._com.anular_aplicacion,
                lambda _a: self.recargar(),
                self.sesion_id,
                valores["aplicacion"],
                valores["motivo"],
            )

    def anular_pago(self) -> None:
        pago_id = clave_seleccionada(self.tabla_pagos)
        if pago_id is None:
            return
        valores = pedir(
            self,
            "Anular pago",
            [Campo("motivo", "Motivo", obligatorio=True)],
            validar=lambda v: dc.exigir_motivo(v["motivo"]),
        )
        if valores is not None:
            self.ejecutar(
                self._com.anular_pago, lambda _p: self.recargar(), self.sesion_id, pago_id, valores["motivo"]
            )

    # ------------------------------------------------------------------ cuentas
    def cerrar_periodo(self) -> None:
        cuenta_id = clave_seleccionada(self.tabla_cuentas)
        if cuenta_id is None:
            return
        valores = pedir(
            self, "Cierre mensual", [Campo("periodo", "Mes", "opciones", opciones=_meses_cerrables())]
        )
        if valores is not None:
            self.ejecutar(
                self._com.cerrar_periodo, self._cerrado, self.sesion_id, cuenta_id, valores["periodo"]
            )

    def _cerrado(self, cierre: Any) -> None:
        # Un cierre repetido devuelve el existente: se avisa cuándo se cerró, para que no parezca uno nuevo.
        self._set_estado(
            f"Período {cierre.periodo.strftime('%m/%Y')} cerrado el {fecha_local(cierre.cerrado_en)}; "
            f"saldo final {_dinero(cierre.saldo_final)}.",
            "ok",
        )
        self.ejecutar(self._com.cuentas, self._mostrar_cuentas, self.sesion_id)  # refresca "Último cierre"

    # ------------------------------------------------------------------ estado de botones
    def actualizar_botones(self) -> None:
        libre = not self.ocupado()
        factura = self._facturas.get(clave_seleccionada(self.tabla_facturas))
        pago = self._pagos.get(clave_seleccionada(self.tabla_pagos))
        cuenta = clave_seleccionada(self.tabla_cuentas)
        self.boton_recargar.setEnabled(libre)
        self.b_detalle.setEnabled(libre and factura is not None)
        self.b_anular.setEnabled(libre and factura is not None and factura.estado == "EMITIDA")
        self.b_compra.setEnabled(libre)
        if hasattr(self, "b_cargo"):
            hay_envio = self.envio.currentData() is not None
            self.b_cargo.setEnabled(libre and hay_envio)
            self.b_cancelar_cargo.setEnabled(libre and clave_seleccionada(self.tabla_cargos) is not None)
            self.b_facturar.setEnabled(libre and hay_envio)
        confirmado = pago is not None and pago.estado == "CONFIRMADO"
        self.b_pago.setEnabled(libre)
        self.b_aplicar.setEnabled(libre and confirmado and pago.disponible > 0)
        self.b_desaplicar.setEnabled(libre and pago is not None and pago.aplicado > 0)
        self.b_anular_pago.setEnabled(libre and confirmado and pago.aplicado == 0)
        self.b_mas.setEnabled(libre and cuenta is not None and self._cursor_mov is not None)
        self.b_cerrar.setEnabled(libre and cuenta is not None)
