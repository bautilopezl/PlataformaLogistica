"""Diálogos comerciales reutilizables: detalle de factura (con PDF), cargos/facturación de un envío y cobro de
una factura con los datos precargados.

Los usan la pestaña Comercial y el detalle del envío, así el recorrido normal no depende de copiar UUID ni de
saltar a ciegas entre pestañas. Como el resto de la UI: todo acceso a datos o archivos va por servicios en un
trabajador (`VistaBase.ejecutar`) y los botones visibles son comodidad (el servicio revalida permisos).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from logistica.core.errors import ValidationError
from logistica.domain import comercial as dc
from logistica.domain.acceso import Permiso
from logistica.domain.comercial_dtos import (
    ComprobanteDTO,
    FacturaDTO,
    ResumenComercialEnvioDTO,
)
from logistica.ui.widgets import (
    Campo,
    VistaBase,
    clave_seleccionada,
    fecha_local,
    llenar_tabla,
    pedir,
    tabla,
)

MONEDAS = (("USD", "USD"), ("ARS", "ARS"), ("EUR", "EUR"), ("BRL", "BRL"))
MEDIOS = tuple((m.value.capitalize(), m.value) for m in dc.Medio)
_dinero = dc.formato_dinero

# Situaciones que piden una acción comercial (se muestran como aviso, no como "ok").
SITUACIONES_ATENCION = {
    dc.SituacionComercial.SIN_PRECIO,
    dc.SituacionComercial.FACTURACION_FALLIDA,
    dc.SituacionComercial.PENDIENTE_DE_FACTURAR,
}


def avisar(parent: QWidget, titulo: str, texto: str) -> None:
    """Aviso informativo que NO bloquea el hilo gráfico (a diferencia de `QMessageBox.information`)."""
    caja = QMessageBox(QMessageBox.Icon.Information, titulo, texto, QMessageBox.StandardButton.Ok, parent)
    caja.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    caja.open()


def puede(permisos: frozenset[str], *codigos: Permiso) -> bool:
    return any(c.value in permisos for c in codigos)


def _estilo(label: QLabel, estado: str) -> None:
    label.setProperty("estado", estado)
    label.style().unpolish(label)
    label.style().polish(label)


# =========================================================================== formularios
def pedir_cargo(parent: QWidget, envio_codigo: str, cliente: str) -> dc.CargoDatos | None:
    """Precio de venta al cliente para ESTE envío (no es el costo del tramo)."""

    def armar(v: dict[str, Any]) -> dc.CargoDatos:
        return dc.CargoDatos(v["concepto"], v["cantidad"], v["precio"], v["moneda"])

    valores = pedir(
        parent,
        f"Precio de venta · {envio_codigo} · cliente {cliente}",
        [
            Campo("concepto", "Concepto que se le cobra al cliente (ej. Flete internacional)", obligatorio=True),
            Campo("cantidad", "Cantidad", valor="1", obligatorio=True),
            Campo("precio", "Precio unitario de venta (sin impuestos agregados por el sistema)", obligatorio=True),
            Campo("moneda", "Moneda", "opciones", opciones=MONEDAS),
        ],
        validar=lambda v: armar(v).normalizado(),
    )
    return armar(valores) if valores is not None else None


def pedir_cobro(parent: QWidget, factura: FacturaDTO) -> dict[str, Any] | None:
    """Formulario precargado con tercero, moneda y saldo de la factura. Importe, fecha y medio los confirma el
    usuario: emitir una factura nunca crea un cobro por sí sola."""
    venta = factura.sentido == dc.Sentido.VENTA
    quien = "Cliente que paga" if venta else "Proveedor/operador al que se paga"
    saldo = str(factura.pendiente)

    def validar(v: dict[str, Any]) -> None:
        monto = dc.decimal_de(v["monto"], "el importe recibido" if venta else "el importe pagado", decimales=2)
        aplicar = dc.decimal_de(v["aplicar"], "el importe a aplicar", decimales=2)
        if aplicar > monto:
            raise ValidationError(
                "aplicar > monto", user_message="No se puede aplicar más de lo que se registra."
            )
        if aplicar > factura.pendiente:
            raise ValidationError(
                "aplicar > saldo",
                user_message=f"La factura solo debe {_dinero(factura.pendiente)} {factura.moneda_codigo}.",
            )
        dc.PagoDatos(
            factura.tercero_id,
            dc.sentido_pago_para(factura.sentido),
            v["fecha"],
            monto,
            factura.moneda_codigo,
            v["medio"],
            v["referencia"],
        ).normalizado(datetime.now(timezone.utc))

    return pedir(
        parent,
        ("Registrar COBRO de la factura " if venta else "Registrar PAGO de la factura ")
        + f"{factura.codigo} · saldo {_dinero(factura.pendiente)} {factura.moneda_codigo}",
        [
            Campo("tercero", quien, "opciones", opciones=[(factura.tercero_razon_social, factura.tercero_id)]),
            Campo("moneda", "Moneda (la de la factura)", "opciones",
                  opciones=[(factura.moneda_codigo, factura.moneda_codigo)]),
            Campo("fecha", "Fecha en que se recibió el dinero" if venta else "Fecha del pago", "fecha_hora"),
            Campo("monto", "Importe recibido" if venta else "Importe pagado", valor=saldo, obligatorio=True),
            Campo("medio", "Medio", "opciones", opciones=MEDIOS),
            Campo("referencia", "Referencia (n.º de transferencia, recibo…)"),
            Campo("aplicar", "Importe a aplicar a esta factura", valor=saldo, obligatorio=True),
        ],
        validar=validar,
    )


class CobroFactura:
    """Registrar + aplicar en una sola operación (`ComercialService.cobrar_factura`). Conserva la clave de
    idempotencia si falla, para que un reintento no duplique el cobro; se descarta al cancelar o al terminar."""

    def __init__(self, vista: VistaBase, comercial: Any):
        self._vista, self._com = vista, comercial
        self._claves: dict[uuid.UUID, uuid.UUID] = {}

    def iniciar(self, factura: FacturaDTO, al_terminar: Any) -> None:
        valores = pedir_cobro(self._vista, factura)
        if valores is None:
            self._claves.pop(factura.id, None)
            return
        clave = self._claves.setdefault(factura.id, uuid.uuid4())

        def hecho(resultado: Any) -> None:
            self._claves.pop(factura.id, None)
            pago, aplicacion = resultado
            aplicado = aplicacion.monto_aplicado if aplicacion else Decimal("0")
            texto = (
                f"{'Cobro' if pago.sentido == dc.SentidoPago.COBRO else 'Pago'} registrado: "
                f"{_dinero(pago.monto)} {pago.moneda_codigo}; aplicado {_dinero(aplicado)} a {factura.codigo}."
            )
            if pago.disponible > 0:
                texto += f" Queda {_dinero(pago.disponible)} disponible (saldo a favor) para otras facturas."
            al_terminar(resultado)
            avisar(self._vista, "Operación registrada", texto)

        self._vista.ejecutar(
            partial(
                self._com.cobrar_factura,
                self._vista.sesion_id,
                factura.id,
                fecha=valores["fecha"],
                monto=valores["monto"],
                medio=valores["medio"],
                referencia=valores["referencia"],
                monto_aplicado=valores["aplicar"],
                clave_idempotencia=clave,
            ),
            hecho,
        )


def boton_cobro_texto(factura: FacturaDTO | None) -> str:
    if factura is not None and factura.sentido == dc.Sentido.COMPRA:
        return "Registrar pago al proveedor de esta factura"
    return "Registrar cobro de esta factura"


def factura_cobrable(factura: FacturaDTO | None) -> bool:
    return factura is not None and factura.estado == dc.EstadoFactura.EMITIDA and factura.pendiente > 0


# =========================================================================== detalle de factura
class FacturaDetalleView(VistaBase):
    """Detalle legible: tipo (venta/compra), número, fechas, estado, contraparte, envíos, conceptos, importe
    original y, aparte, el estado de cobranza a la fecha; aplicaciones y anulación. Exporta el PDF."""

    def __init__(self, comercial: Any, sesion_id: uuid.UUID, factura_id: uuid.UUID,
                 permisos: frozenset[str], parent: QWidget | None = None):
        super().__init__(sesion_id, parent)
        self._com, self.factura_id, self._permisos = comercial, factura_id, permisos
        self.comprobante: ComprobanteDTO | None = None
        self._cobro = CobroFactura(self, comercial)
        self.cambio = False  # hubo cobros: quien abrió el diálogo recarga
        self.titulo = QLabel("Cargando…")
        self.titulo.setObjectName("titulo")
        self.tipo = QLabel("")
        self.datos = QLabel("")
        self.datos.setWordWrap(True)
        self.partes = QLabel("")
        self.partes.setWordWrap(True)
        self.renglones = tabla(("#", "Concepto", "Envío", "Cantidad", "Precio unitario", "Importe"))
        self.total = QLabel("")
        self.cobranza = QLabel("")
        self.cobranza.setWordWrap(True)
        self.aplicaciones = tabla(("Fecha", "Importe aplicado", "Estado"))
        self.anulacion = QLabel("")
        self.anulacion.setWordWrap(True)
        self.b_pdf = QPushButton("Exportar PDF")
        self.b_pdf.clicked.connect(self.exportar_pdf)
        self.b_cobrar = QPushButton("Registrar cobro de esta factura")
        self.b_cobrar.clicked.connect(self.cobrar)
        self.b_cobrar.setVisible(puede(permisos, Permiso.PAGOS_REGISTRAR) and puede(permisos, Permiso.PAGOS_APLICAR))
        aviso = QLabel("Documento académico/interno sin validez fiscal: no es un comprobante autorizado por ARCA.")
        _estilo(aviso, "pendiente")
        botones = QHBoxLayout()
        for b in (self.b_pdf, self.b_cobrar):
            botones.addWidget(b)
        botones.addStretch()
        layout = QVBoxLayout(self)
        for w in (self.titulo, self.tipo, aviso, self.datos, self.partes):
            layout.addWidget(w)
        layout.addWidget(QLabel("Conceptos (importes registrados al emitir; no se recalculan):"))
        layout.addWidget(self.renglones, 3)
        layout.addWidget(self.total)
        layout.addWidget(self.cobranza)
        layout.addWidget(QLabel("Aplicaciones de cobros/pagos:"))
        layout.addWidget(self.aplicaciones, 1)
        layout.addWidget(self.anulacion)
        layout.addLayout(botones)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    def recargar(self) -> None:
        self.ejecutar(self._com.comprobante, self._mostrar, self.sesion_id, self.factura_id)

    def _mostrar(self, c: ComprobanteDTO) -> None:
        self.comprobante = c
        f = c.factura
        venta = f.sentido == dc.Sentido.VENTA
        self.titulo.setText(f"{'Factura de venta' if venta else 'Factura de compra'} {f.codigo} · {f.estado}")
        self.tipo.setText(
            "VENTA: la emitimos al cliente; se cancela con un COBRO del cliente."
            if venta
            else "COMPRA: nos la emitió un operador/proveedor; se cancela con un PAGO nuestro (no con un cobro)."
        )
        _estilo(self.tipo, "ok" if venta else "pendiente")
        envios = ", ".join(sorted(set(c.envio_codigos.values()))) or "sin envío asociado"
        self.datos.setText(
            f"Emitida: {fecha_local(f.emitida_en) or '—'}   ·   Vence: {fecha_local(f.vence_en) or '—'}   ·   "
            f"Moneda: {f.moneda_codigo}   ·   Envío(s): {envios}"
        )
        k = c.contraparte
        emisor = c.emisor.razon_social if c.emisor.configurado else "plataforma (emisor no configurado)"
        self.partes.setText(
            f"{'Cliente' if venta else 'Proveedor/operador emisor'}: {k.razon_social} · "
            f"{k.tipo_identificacion} {k.numero_identificacion} ({k.pais_codigo})"
            + (f" · {k.direccion}" if k.direccion else " · domicilio no registrado")
            + (f"\nEmisor: {emisor}" if venta else f"\nReceptor: {emisor}")
        )
        llenar_tabla(
            self.renglones,
            [
                (r.renglon, r.concepto, c.envio_codigos.get(r.envio_id, "") if r.envio_id else "",
                 r.cantidad.normalize(), r.precio_unitario, _dinero(r.subtotal))
                for r in f.renglones
            ],
            [r.renglon for r in f.renglones],
        )
        self.total.setText(f"Importe original de la factura: {f.moneda_codigo} {_dinero(f.importe_total)}")
        accion = "cobranza" if venta else "pago"
        self.cobranza.setText(
            f"Estado de {accion} al {fecha_local(c.generado_en)}: aplicado {_dinero(f.aplicado)} · "
            f"saldo pendiente {_dinero(f.pendiente)} {f.moneda_codigo}"
            + ("" if f.estado == "EMITIDA" else " (factura anulada: no se cobra)")
        )
        _estilo(self.cobranza, "error" if f.pendiente > 0 else "ok")
        llenar_tabla(
            self.aplicaciones,
            [
                (fecha_local(a.creado_en), _dinero(a.monto_aplicado),
                 "Vigente" if a.anulada_en is None
                 else f"Anulada el {fecha_local(a.anulada_en)}: {a.motivo_anulacion or ''}")
                for a in c.aplicaciones
            ],
            [a.id for a in c.aplicaciones],
        )
        self.anulacion.setText(
            f"Anulada el {fecha_local(f.anulada_en)}. Motivo: {f.motivo_anulacion}" if f.motivo_anulacion else ""
        )
        self.b_cobrar.setText(boton_cobro_texto(f))

    def exportar_pdf(self) -> None:
        self.ejecutar(self._com.exportar_comprobante, self._abrir_pdf, self.sesion_id, self.factura_id)

    def _abrir_pdf(self, ruta: Any) -> None:
        self._set_estado(f"PDF guardado en {ruta}", "ok")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ruta)))

    def cobrar(self) -> None:
        if self.comprobante is not None:
            self._cobro.iniciar(self.comprobante.factura, self._cobrado)

    def _cobrado(self, _resultado: Any) -> None:
        self.cambio = True
        self.recargar()

    def actualizar_botones(self) -> None:
        libre = not self.ocupado() and self.comprobante is not None
        self.b_pdf.setEnabled(libre)
        self.b_cobrar.setEnabled(libre and factura_cobrable(self.comprobante.factura if self.comprobante else None))


def abrir_factura(parent: QWidget, comercial: Any, sesion_id: uuid.UUID, factura_id: uuid.UUID,
                  permisos: frozenset[str], sesion_vencida: Any) -> bool:
    """Abre el detalle modal. Devuelve True si se registró algún cobro (para recargar)."""
    dialogo = QDialog(parent)
    dialogo.setWindowTitle("Factura")
    vista = FacturaDetalleView(comercial, sesion_id, factura_id, permisos, dialogo)
    vista.sesion_vencida.connect(sesion_vencida)
    vista.sesion_vencida.connect(lambda _m: dialogo.reject())
    cerrar = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    cerrar.rejected.connect(dialogo.reject)
    layout = QVBoxLayout(dialogo)
    layout.addWidget(vista)
    layout.addWidget(cerrar)
    dialogo.resize(900, 640)
    vista.recargar()
    dialogo.exec()
    vista.cerrar()
    cambio = vista.cambio
    dialogo.deleteLater()
    return cambio


# =========================================================================== cargos / facturación del envío
class CargosEnvioView(VistaBase):
    """Contexto comercial de UN envío: cliente, precio de venta (cargos), situación de la facturación, sus
    facturas de venta con saldo y las acciones que correspondan (cargar precio, facturar pendientes, ver la
    factura/PDF, registrar el cobro)."""

    def __init__(self, comercial: Any, sesion_id: uuid.UUID, envio_id: uuid.UUID, permisos: frozenset[str],
                 parent: QWidget | None = None):
        super().__init__(sesion_id, parent)
        self._com, self.envio_id, self._permisos = comercial, envio_id, permisos
        self.resumen: ResumenComercialEnvioDTO | None = None
        self._cobro = CobroFactura(self, comercial)
        self.cabecera = QLabel("Cargando…")
        self.cabecera.setObjectName("titulo")
        self.situacion = QLabel("")
        self.situacion.setWordWrap(True)
        self.pendiente = QLabel("")
        self.cargos = tabla(("Concepto", "Cantidad", "Precio unitario", "Importe", "Moneda", "Estado", "Creado"))
        self.cargos.itemSelectionChanged.connect(self.actualizar_botones)
        self.facturas = tabla(("Factura", "Emitida", "Moneda", "Total", "Cobrado", "Saldo", "Estado"))
        self.facturas.itemSelectionChanged.connect(self.actualizar_botones)
        self.facturas.doubleClicked.connect(self.ver_factura)
        gestionar = puede(permisos, Permiso.CARGOS_GESTIONAR)
        self.b_cargo = QPushButton("Agregar precio / cargo")
        self.b_cargo.clicked.connect(self.nuevo_cargo)
        self.b_cargo.setVisible(gestionar)
        self.b_cancelar = QPushButton("Cancelar cargo pendiente")
        self.b_cancelar.clicked.connect(self.cancelar_cargo)
        self.b_cancelar.setVisible(gestionar)
        self.b_facturar = QPushButton("Facturar pendientes")
        self.b_facturar.clicked.connect(self.facturar)
        self.b_facturar.setVisible(puede(permisos, Permiso.FACTURAS_EMITIR))
        self.b_ver = QPushButton("Ver factura / PDF")
        self.b_ver.clicked.connect(self.ver_factura)
        self.b_cobrar = QPushButton("Registrar cobro de esta factura")
        self.b_cobrar.clicked.connect(self.cobrar)
        self.b_cobrar.setVisible(puede(permisos, Permiso.PAGOS_REGISTRAR) and puede(permisos, Permiso.PAGOS_APLICAR))
        fila_cargos = QHBoxLayout()
        for b in (self.b_cargo, self.b_cancelar, self.b_facturar):
            fila_cargos.addWidget(b)
        fila_cargos.addStretch()
        fila_facturas = QHBoxLayout()
        for b in (self.b_ver, self.b_cobrar):
            fila_facturas.addWidget(b)
        fila_facturas.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.cabecera)
        layout.addWidget(self.situacion)
        layout.addWidget(QLabel("Precio de venta al cliente (cargos). No es el costo de los tramos:"))
        layout.addWidget(self.cargos, 2)
        layout.addWidget(self.pendiente)
        layout.addLayout(fila_cargos)
        layout.addWidget(QLabel("Facturas de venta de este envío:"))
        layout.addWidget(self.facturas, 2)
        layout.addLayout(fila_facturas)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    def recargar(self) -> None:
        self.ejecutar(self._com.resumen_envio, self._mostrar, self.sesion_id, self.envio_id)

    def _mostrar(self, r: ResumenComercialEnvioDTO) -> None:
        self.resumen = r
        self.cabecera.setText(
            f"{r.envio_codigo} · {r.envio_estado.replace('_', ' ').title()} · Cliente: {r.cliente_razon_social}"
        )
        auto = (
            f" (facturación automática: {r.facturacion_estado.lower()}, {r.facturacion_intentos} intento(s))"
            if r.facturacion_estado
            else ""
        )
        self.situacion.setText(r.mensaje + auto)
        _estilo(self.situacion, "error" if r.situacion in SITUACIONES_ATENCION else "ok")
        llenar_tabla(
            self.cargos,
            [
                (c.concepto, c.cantidad.normalize(), c.precio_unitario, _dinero(c.importe), c.moneda_codigo,
                 c.estado, fecha_local(c.creado_en))
                for c in r.cargos
            ],
            [c.id for c in r.cargos],
        )
        pendiente = r.pendiente_por_moneda
        self.pendiente.setText(
            "Pendiente de facturar: "
            + (" · ".join(f"{m} {_dinero(v)}" for m, v in sorted(pendiente.items())) if pendiente else "nada")
        )
        llenar_tabla(
            self.facturas,
            [
                (f.codigo, fecha_local(f.emitida_en), f.moneda_codigo, _dinero(f.importe_total),
                 _dinero(f.aplicado), _dinero(f.pendiente), f.estado)
                for f in r.facturas
            ],
            [f.id for f in r.facturas],
        )

    def _factura(self) -> FacturaDTO | None:
        clave = clave_seleccionada(self.facturas)
        return next((f for f in self.resumen.facturas if f.id == clave), None) if self.resumen and clave else None

    def nuevo_cargo(self) -> None:
        if self.resumen is None:
            return
        datos = pedir_cargo(self, self.resumen.envio_codigo, self.resumen.cliente_razon_social)
        if datos is not None:
            self.ejecutar(self._com.crear_cargo, lambda _c: self.recargar(), self.sesion_id, self.envio_id, datos)

    def cancelar_cargo(self) -> None:
        cargo_id = clave_seleccionada(self.cargos)
        if cargo_id is not None:
            self.ejecutar(self._com.cancelar_cargo, lambda _c: self.recargar(), self.sesion_id, cargo_id)

    def facturar(self) -> None:
        self.ejecutar(self._com.emitir_factura, self._facturado, self.sesion_id, self.envio_id)

    def _facturado(self, facturas: list[FacturaDTO]) -> None:
        self.recargar()
        avisar(
            self,
            "Factura emitida",
            "Se emitió: "
            + "; ".join(f"{f.codigo} por {f.moneda_codigo} {_dinero(f.importe_total)}" for f in facturas)
            + ".\nQueda en la cuenta por cobrar del cliente. El cobro NO se registra solo: cuando recibas el "
            "dinero, seleccioná la factura y usá «Registrar cobro de esta factura».",
        )

    def ver_factura(self) -> None:
        f = self._factura()
        if f is not None and abrir_factura(self, self._com, self.sesion_id, f.id, self._permisos,
                                           self.sesion_vencida.emit):
            self.recargar()

    def cobrar(self) -> None:
        f = self._factura()
        if f is not None:
            self._cobro.iniciar(f, lambda _r: self.recargar())

    def actualizar_botones(self) -> None:
        libre = not self.ocupado() and self.resumen is not None
        r = self.resumen
        cargo = next((c for c in r.cargos if c.id == clave_seleccionada(self.cargos)), None) if r else None
        factura = self._factura()
        self.b_cargo.setEnabled(libre and r.envio_estado != "CANCELADO")
        self.b_cancelar.setEnabled(libre and cargo is not None and cargo.estado == "PENDIENTE")
        self.b_facturar.setEnabled(libre and r.envio_estado == "ENTREGADO" and bool(r.pendiente_por_moneda))
        self.b_ver.setEnabled(libre and factura is not None)
        self.b_cobrar.setEnabled(libre and factura_cobrable(factura))


def abrir_cargos_envio(parent: QWidget, comercial: Any, sesion_id: uuid.UUID, envio_id: uuid.UUID,
                       permisos: frozenset[str], sesion_vencida: Any) -> None:
    dialogo = QDialog(parent)
    dialogo.setWindowTitle("Cargos y facturación del envío")
    vista = CargosEnvioView(comercial, sesion_id, envio_id, permisos, dialogo)
    vista.sesion_vencida.connect(sesion_vencida)
    vista.sesion_vencida.connect(lambda _m: dialogo.reject())
    cerrar = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    cerrar.rejected.connect(dialogo.reject)
    layout = QVBoxLayout(dialogo)
    layout.addWidget(vista)
    layout.addWidget(cerrar)
    dialogo.resize(960, 620)
    vista.recargar()
    dialogo.exec()
    vista.cerrar()
    dialogo.deleteLater()
