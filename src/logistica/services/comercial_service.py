"""Gestión comercial: cargos, facturación (automática y manual), pagos, aplicaciones, cuentas y cierres.

AGENTS.md §10.3. Cada operación de usuario: sesión → permiso → alcance → regla → transacción → auditoría.
Factura + detalles + movimiento de cuenta (y los cambios de los cargos) van en UNA transacción PostgreSQL.

Facturación automática (decisión B del grupo, §16.3): confirmar la entrega NO factura. El evento
ENVIO_ENTREGADO lleva el destino FACTURACION en la outbox; `SincronizacionService` llama a `facturar_entrega`
dentro de la misma transacción en la que completa esa entrega: o queda la factura y la entrega completada, o
ninguna de las dos. SUPUESTOS C1..C7: ver AGENTS.md §16 ("Supuestos de la fase 6").
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Any

from logistica.core.errors import BusinessRuleError, NotFoundError, ValidationError
from logistica.domain import comercial as dc
from logistica.domain.acceso import Permiso, Principal, Rol, RolComercial
from logistica.domain.comercial_dtos import (
    AplicacionDTO,
    CargoDTO,
    CierreDTO,
    CuentaDTO,
    FacturaDTO,
    PaginaMovimientos,
    PagoDTO,
)
from logistica.domain.logistica import EstadoEnvio
from logistica.domain.procesos import meses_a_cerrar
from logistica.repositories.connections import Connections
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.comercial import (
    AlcanceComercial,
    AplicacionRepository,
    CargoRepository,
    CuentaRepository,
    FacturaRepository,
    PagoRepository,
)
from logistica.repositories.postgres.envios import EnvioRepository
from logistica.repositories.postgres.monitoreo import ahora as ahora_pg
from logistica.repositories.postgres.terceros import TerceroRepository
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador

log = logging.getLogger(__name__)


class ComercialService:
    def __init__(self, connections: Connections, autorizador: Autorizador):
        self._conn = connections
        self._aut = autorizador
        self._cargos = CargoRepository()
        self._facturas = FacturaRepository()
        self._pagos = PagoRepository()
        self._aplicaciones = AplicacionRepository()
        self._cuentas = CuentaRepository()
        self._envios = EnvioRepository()
        self._terceros = TerceroRepository()

    # ======================================================================= cargos
    def crear_cargo(self, sesion_id: uuid.UUID, envio_id: uuid.UUID, datos: dc.CargoDatos) -> CargoDTO:
        with self._aut.operacion(sesion_id, Permiso.CARGOS_GESTIONAR) as (tx, p):
            datos = datos.normalizado()
            envio = self._envio(tx, envio_id, bloquear=True)
            if envio.estado == EstadoEnvio.CANCELADO:
                raise BusinessRuleError(
                    "envío cancelado", user_message="No se cargan cargos a un envío cancelado."
                )
            cargo_id = uuid.uuid4()
            self._cargos.crear(
                tx,
                cargo_id,
                envio_id,
                datos.concepto,
                datos.cantidad,
                datos.precio_unitario,
                datos.moneda_codigo,
            )
            auditoria.registrar(
                tx,
                "crear_cargo",
                p,
                entidad="cargo_envio",
                entidad_id=cargo_id,
                envio_id=str(envio_id),
                moneda=datos.moneda_codigo,
            )
            return self._cargos.obtener(tx, cargo_id)

    def cancelar_cargo(self, sesion_id: uuid.UUID, cargo_id: uuid.UUID) -> CargoDTO:
        with self._aut.operacion(sesion_id, Permiso.CARGOS_GESTIONAR) as (tx, p):
            if self._cargos.obtener(tx, cargo_id, bloquear=True) is None:
                raise NotFoundError(f"cargo {cargo_id}", user_message="El cargo no existe.")
            if not self._cargos.cambiar_estado(
                tx, [cargo_id], dc.EstadoCargo.PENDIENTE, dc.EstadoCargo.CANCELADO
            ):
                raise BusinessRuleError(
                    "cargo no pendiente", user_message="Solo se cancela un cargo pendiente."
                )
            auditoria.registrar(tx, "cancelar_cargo", p, entidad="cargo_envio", entidad_id=cargo_id)
            return self._cargos.obtener(tx, cargo_id)

    def cargos(self, sesion_id: uuid.UUID, envio_id: uuid.UUID) -> list[CargoDTO]:
        with self._aut.operacion(sesion_id, Permiso.CARGOS_GESTIONAR, Permiso.FACTURAS_EMITIR) as (tx, _p):
            self._envio(tx, envio_id)
            return self._cargos.de_envio(tx, envio_id)

    # ======================================================================= facturación de ventas
    def facturar_entrega(
        self, tx: Any, envio_id: uuid.UUID, usuario_id: uuid.UUID | None, evento_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Facturación AUTOMÁTICA de un envío entregado (proceso técnico, sin sesión). Corre en la transacción
        del consumidor de la outbox. Sin cargos pendientes no emite nada (los posteriores se facturan a mano).

        `creada_por_id` = quien confirmó la entrega (SUPUESTO C2): la factura no la "crea" un usuario sin
        permiso de facturar, sino el proceso, y la auditoría lo deja explícito (`factura_automatica`).
        """
        envio = self._envios.obtener(tx, envio_id, bloquear=True)
        if envio is None or envio.estado != EstadoEnvio.ENTREGADO:
            log.warning("Facturación automática omitida: el envío %s no está ENTREGADO", envio_id)
            return []
        if usuario_id is None:
            raise ValidationError(
                "evento sin usuario", user_message="El evento de entrega no indica el usuario."
            )
        ids = self._facturar(tx, envio, self._cargos.pendientes(tx, envio_id), usuario_id)
        for factura_id in ids:
            auditoria.registrar(
                tx,
                "factura_automatica",
                None,
                usuario_id=usuario_id,
                entidad="factura",
                entidad_id=factura_id,
                envio_id=str(envio_id),
                evento_id=str(evento_id),
            )
        if ids:
            log.info("Facturación automática del envío %s: %d factura(s)", envio.codigo, len(ids))
        return ids

    def emitir_factura(
        self, sesion_id: uuid.UUID, envio_id: uuid.UUID, cargo_ids: list[uuid.UUID] | None = None
    ) -> list[FacturaDTO]:
        """Factura MANUAL de cargos pendientes (los agregados después de la entrega, o los de reemplazo de una
        factura anulada). SUPUESTO C4: solo para envíos ENTREGADOS (se factura lo entregado)."""
        with self._aut.operacion(sesion_id, Permiso.FACTURAS_EMITIR) as (tx, p):
            envio = self._envio(tx, envio_id, bloquear=True)
            if envio.estado != EstadoEnvio.ENTREGADO:
                raise BusinessRuleError(
                    "envío no entregado", user_message="Solo se facturan envíos entregados."
                )
            cargos = self._cargos.pendientes(tx, envio_id, cargo_ids)
            if cargo_ids is not None and len(cargos) != len(set(cargo_ids)):
                raise BusinessRuleError(
                    "cargos no pendientes",
                    user_message="Algún cargo elegido no está pendiente o es de otro envío.",
                )
            if not cargos:
                raise BusinessRuleError("sin cargos", user_message="El envío no tiene cargos pendientes.")
            ids = self._facturar(tx, envio, cargos, p.usuario_id)
            for factura_id in ids:
                auditoria.registrar(
                    tx, "emitir_factura", p, entidad="factura", entidad_id=factura_id, envio_id=str(envio_id)
                )
            return [self._facturas.obtener(tx, i, con_renglones=True) for i in ids]

    def _facturar(
        self, tx: Any, envio: Any, cargos: list[dc.CargoPendiente], usuario_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Una factura de VENTA por moneda, con su movimiento en la cuenta por cobrar del cliente.

        Los cargos llegan bloqueados (FOR UPDATE) y además `factura_detalle.cargo_envio_id` es UNIQUE: aunque
        dos procesos intentaran lo mismo, un cargo nunca queda en dos facturas.
        """
        ids = []
        for moneda, renglones in dc.agrupar_por_moneda(cargos).items():
            total = dc.total_de(renglones)
            emitida = ahora_pg(tx)
            serie = dc.serie_venta(emitida)
            numero = dc.siguiente_numero(self._facturas.ultimo_numero(tx, serie))
            factura_id = uuid.uuid4()
            self._facturas.crear_emitida(
                tx,
                factura_id=factura_id,
                tercero_id=envio.cliente_id,
                sentido=dc.Sentido.VENTA,
                tipo=dc.TIPO_FACTURA,
                serie=serie,
                numero=numero,
                emitida_en=emitida,
                vence_en=dc.vencimiento(emitida),
                moneda=moneda,
                total=total,
                creada_por_id=usuario_id,
                renglones=renglones,
            )
            self._cargos.cambiar_estado(
                tx, [r.cargo_id for r in renglones], dc.EstadoCargo.PENDIENTE, dc.EstadoCargo.FACTURADO
            )
            cuenta = self._cuentas.obtener_o_crear(tx, envio.cliente_id, dc.Naturaleza.COBRAR, moneda)
            self._cuentas.registrar_movimiento(
                tx,
                cuenta_id=cuenta,
                sentido=dc.AUMENTA,
                importe=total,
                factura_id=factura_id,
                clave_origen=f"factura:{factura_id}",
                descripcion=f"Factura {dc.codigo_factura(serie, numero)} · envío {envio.codigo}",
            )
            ids.append(factura_id)
        return ids

    # ======================================================================= facturas de compra
    def registrar_factura_compra(self, sesion_id: uuid.UUID, datos: dc.FacturaCompraDatos) -> FacturaDTO:
        """Factura que nos emite un operador o proveedor (su numeración). Aumenta nuestra cuenta por pagar."""
        with self._aut.operacion(sesion_id, Permiso.FACTURAS_EMITIR) as (tx, p):
            datos = datos.normalizado(ahora_pg(tx))
            tercero = self._terceros.obtener(tx, datos.tercero_id)
            if (
                tercero is None
                or not tercero.activo
                or not ({RolComercial.OPERADOR, RolComercial.PROVEEDOR} & set(tercero.roles_comerciales))
            ):
                raise BusinessRuleError(
                    "tercero sin rol de compra",
                    user_message="La factura de compra debe ser de un operador o proveedor activo.",
                )
            if self._facturas.existe_compra(
                tx, datos.tercero_id, datos.tipo_comprobante, datos.serie, datos.numero
            ):
                raise ValidationError(
                    "compra duplicada", user_message="Esa factura del proveedor ya está registrada."
                )
            for r in datos.renglones:
                if r.envio_id is not None:
                    self._envio(tx, r.envio_id)
            total = dc.total_de(datos.renglones)
            factura_id = uuid.uuid4()
            self._facturas.crear_emitida(
                tx,
                factura_id=factura_id,
                tercero_id=datos.tercero_id,
                sentido=dc.Sentido.COMPRA,
                tipo=datos.tipo_comprobante,
                serie=datos.serie,
                numero=datos.numero,
                emitida_en=datos.emitida_en,
                vence_en=datos.vence_en,
                moneda=datos.moneda_codigo,
                total=total,
                creada_por_id=p.usuario_id,
                renglones=datos.renglones,
            )
            cuenta = self._cuentas.obtener_o_crear(
                tx, datos.tercero_id, dc.Naturaleza.PAGAR, datos.moneda_codigo
            )
            self._cuentas.registrar_movimiento(
                tx,
                cuenta_id=cuenta,
                sentido=dc.AUMENTA,
                importe=total,
                factura_id=factura_id,
                clave_origen=f"factura:{factura_id}",
                descripcion=f"Factura de compra {dc.codigo_factura(datos.serie, datos.numero)}",
            )
            auditoria.registrar(tx, "registrar_factura_compra", p, entidad="factura", entidad_id=factura_id)
            return self._facturas.obtener(tx, factura_id, con_renglones=True)

    # ======================================================================= anulación
    def anular_factura(
        self, sesion_id: uuid.UUID, factura_id: uuid.UUID, motivo: str, *, refacturar: bool
    ) -> tuple[FacturaDTO, list[CargoDTO]]:
        """Anula (nunca borra) y compensa con una reversa. Decisión del grupo (§16.3): no se anula con pagos
        aplicados vigentes; `refacturar` crea cargos de reemplazo PENDIENTES para facturar a mano.

        SUPUESTO C7: los cargos de la factura anulada pasan a CANCELADO (quedan atados a ella); así FACTURADO
        siempre significa "en una factura vigente". Devuelve la factura y los cargos de reemplazo creados.
        """
        with self._aut.operacion(sesion_id, Permiso.FACTURAS_ANULAR) as (tx, p):
            motivo = dc.exigir_motivo(motivo)
            factura = self._facturas.obtener(tx, factura_id, bloquear=True)
            if factura is None:
                raise NotFoundError(f"factura {factura_id}", user_message="La factura no existe.")
            if factura.estado != dc.EstadoFactura.EMITIDA:
                raise BusinessRuleError(
                    "factura no emitida", user_message="Solo se anula una factura emitida."
                )
            if factura.aplicado > 0:
                raise BusinessRuleError(
                    "factura con pagos",
                    user_message="La factura tiene pagos aplicados: anule primero esas aplicaciones.",
                )
            self._facturas.anular(tx, factura_id, p.usuario_id, motivo)
            self._revertir(tx, factura_id=factura_id, descripcion=f"Anulación de la factura {factura.codigo}")
            originales = self._facturas.cargos(tx, factura_id)
            if originales:
                self._cargos.cambiar_estado(
                    tx, originales, dc.EstadoCargo.FACTURADO, dc.EstadoCargo.CANCELADO
                )
            reemplazos = []
            if refacturar:
                if not originales:
                    raise BusinessRuleError(
                        "sin cargos", user_message="Esta factura no tiene cargos para refacturar."
                    )
                for cargo_id in originales:
                    c = self._cargos.obtener(tx, cargo_id)
                    nuevo = uuid.uuid4()
                    self._cargos.crear(
                        tx, nuevo, c.envio_id, c.concepto, c.cantidad, c.precio_unitario, c.moneda_codigo
                    )
                    auditoria.registrar(
                        tx,
                        "crear_cargo",
                        p,
                        entidad="cargo_envio",
                        entidad_id=nuevo,
                        reemplaza_cargo_id=str(cargo_id),
                        factura_anulada_id=str(factura_id),
                    )
                    reemplazos.append(self._cargos.obtener(tx, nuevo))
            auditoria.registrar(
                tx, "anular_factura", p, entidad="factura", entidad_id=factura_id, refacturar=refacturar
            )
            return self._facturas.obtener(tx, factura_id, con_renglones=True), reemplazos

    def _revertir(
        self,
        tx: Any,
        *,
        descripcion: str,
        factura_id: uuid.UUID | None = None,
        pago_id: uuid.UUID | None = None,
    ) -> None:
        original = self._cuentas.movimiento_original(tx, factura_id=factura_id, pago_id=pago_id)
        if original is None:
            raise BusinessRuleError("sin movimiento", user_message="No se encontró el movimiento a revertir.")
        movimiento_id, cuenta_id, sentido, importe = original
        self._cuentas.bloquear(tx, cuenta_id)
        self._cuentas.registrar_movimiento(
            tx,
            cuenta_id=cuenta_id,
            sentido=dc.opuesto(sentido),
            importe=importe,
            reversa_de_id=movimiento_id,
            clave_origen=f"reversa:{movimiento_id}",
            descripcion=descripcion,
        )

    # ======================================================================= pagos
    def registrar_pago(
        self, sesion_id: uuid.UUID, datos: dc.PagoDatos, clave_idempotencia: uuid.UUID
    ) -> PagoDTO:
        """Cobro (de un cliente) o pago (a un operador/proveedor). Disminuye la cuenta en el momento: un pago
        sin aplicar es saldo a favor. Reintentar con la misma clave devuelve el mismo pago, sin duplicarlo."""
        with self._aut.operacion(sesion_id, Permiso.PAGOS_REGISTRAR) as (tx, p):
            datos = datos.normalizado(ahora_pg(tx))
            existente = self._pagos.por_clave(tx, clave_idempotencia)
            if existente is not None:
                return self._mismo_pago(existente, datos)
            roles = (
                {RolComercial.CLIENTE}
                if datos.sentido == dc.SentidoPago.COBRO
                else {RolComercial.OPERADOR, RolComercial.PROVEEDOR}
            )
            tercero = self._terceros.obtener(tx, datos.tercero_id)
            if tercero is None or not tercero.activo or not roles & set(tercero.roles_comerciales):
                raise BusinessRuleError(
                    "tercero sin rol",
                    user_message=(
                        "Un cobro es de un cliente activo."
                        if datos.sentido == dc.SentidoPago.COBRO
                        else "Un pago es a un operador o proveedor activo."
                    ),
                )
            cuenta = self._cuentas.obtener_o_crear(
                tx, datos.tercero_id, dc.naturaleza_pago(datos.sentido), datos.moneda_codigo
            )
            ultimo = self._cuentas.ultimo_cierre(tx, cuenta)
            if ultimo is not None and dc.periodo_de(datos.fecha) <= ultimo:
                raise BusinessRuleError(
                    "período cerrado", user_message="La fecha del pago cae en un período ya cerrado."
                )
            pago_id = uuid.uuid4()
            if not self._pagos.crear(
                tx,
                pago_id,
                tercero_id=datos.tercero_id,
                sentido=datos.sentido,
                fecha=datos.fecha,
                monto=datos.monto,
                moneda=datos.moneda_codigo,
                medio=datos.medio,
                referencia=datos.referencia,
                usuario_id=p.usuario_id,
                clave=clave_idempotencia,
            ):
                # Otra transacción registró la misma clave mientras tanto: devolver ese pago.
                return self._mismo_pago(self._pagos.por_clave(tx, clave_idempotencia), datos)
            self._cuentas.registrar_movimiento(
                tx,
                cuenta_id=cuenta,
                sentido=dc.DISMINUYE,
                importe=datos.monto,
                pago_id=pago_id,
                clave_origen=f"pago:{pago_id}",
                descripcion=("Cobro" if datos.sentido == dc.SentidoPago.COBRO else "Pago")
                + f" ({datos.medio})",
            )
            auditoria.registrar(
                tx, "registrar_pago", p, entidad="pago", entidad_id=pago_id, sentido=datos.sentido
            )
            return self._pagos.obtener(tx, pago_id)

    @staticmethod
    def _mismo_pago(existente: PagoDTO, datos: dc.PagoDatos) -> PagoDTO:
        # La fecha NO se compara: al reintentar desde la UI el formulario vuelve a proponer "ahora".
        if (
            existente.tercero_id,
            existente.sentido,
            existente.monto,
            existente.moneda_codigo,
            existente.medio,
            existente.referencia,
        ) != (
            datos.tercero_id,
            datos.sentido,
            datos.monto,
            datos.moneda_codigo,
            datos.medio,
            datos.referencia,
        ):
            raise ValidationError(
                "clave reutilizada", user_message="Esa clave ya corresponde a otro pago con datos distintos."
            )
        return existente

    def aplicar_pago(
        self, sesion_id: uuid.UUID, pago_id: uuid.UUID, factura_id: uuid.UUID, monto: Any
    ) -> AplicacionDTO:
        """Aplicación parcial o total. Bloquea pago y factura (en ese orden): dos aplicaciones simultáneas no
        pueden superar el disponible del pago ni el saldo de la factura. No mueve la cuenta (el pago ya la
        movió)."""
        with self._aut.operacion(sesion_id, Permiso.PAGOS_APLICAR) as (tx, p):
            monto = dc.decimal_de(monto, "el monto", decimales=2)
            if self._pagos.obtener(tx, pago_id, bloquear=True) is None:
                raise NotFoundError(f"pago {pago_id}", user_message="El pago no existe.")
            if self._facturas.obtener(tx, factura_id, bloquear=True) is None:
                raise NotFoundError(f"factura {factura_id}", user_message="La factura no existe.")
            dc.exigir_aplicable(self._pagos.saldo(tx, pago_id), self._facturas.saldo(tx, factura_id), monto)
            aplicacion_id = uuid.uuid4()
            self._aplicaciones.crear(tx, aplicacion_id, pago_id, factura_id, monto)
            auditoria.registrar(
                tx,
                "aplicar_pago",
                p,
                entidad="pago_aplicacion",
                entidad_id=aplicacion_id,
                pago_id=str(pago_id),
                factura_id=str(factura_id),
                monto=str(monto),
            )
            return self._aplicaciones.obtener(tx, aplicacion_id)

    def anular_aplicacion(self, sesion_id: uuid.UUID, aplicacion_id: uuid.UUID, motivo: str) -> AplicacionDTO:
        with self._aut.operacion(sesion_id, Permiso.PAGOS_APLICAR) as (tx, p):
            motivo = dc.exigir_motivo(motivo)
            aplicacion = self._aplicaciones.obtener(tx, aplicacion_id)
            if aplicacion is None:
                raise NotFoundError(f"aplicación {aplicacion_id}", user_message="La aplicación no existe.")
            self._pagos.obtener(tx, aplicacion.pago_id, bloquear=True)
            self._facturas.obtener(tx, aplicacion.factura_id, bloquear=True)
            if not self._aplicaciones.anular(tx, aplicacion_id, motivo):
                raise BusinessRuleError("ya anulada", user_message="La aplicación ya estaba anulada.")
            auditoria.registrar(
                tx, "anular_aplicacion", p, entidad="pago_aplicacion", entidad_id=aplicacion_id
            )
            return self._aplicaciones.obtener(tx, aplicacion_id)

    def anular_pago(self, sesion_id: uuid.UUID, pago_id: uuid.UUID, motivo: str) -> PagoDTO:
        """Como la factura: no se anula con aplicaciones vigentes; la reversa devuelve el saldo."""
        with self._aut.operacion(sesion_id, Permiso.PAGOS_ANULAR) as (tx, p):
            motivo = dc.exigir_motivo(motivo)
            pago = self._pagos.obtener(tx, pago_id, bloquear=True)
            if pago is None:
                raise NotFoundError(f"pago {pago_id}", user_message="El pago no existe.")
            if pago.estado != dc.EstadoPago.CONFIRMADO:
                raise BusinessRuleError("pago anulado", user_message="El pago ya está anulado.")
            if pago.aplicado > 0:
                raise BusinessRuleError(
                    "pago aplicado",
                    user_message="El pago está aplicado a facturas: anule primero las aplicaciones.",
                )
            self._pagos.anular(tx, pago_id, p.usuario_id, motivo)
            self._revertir(tx, pago_id=pago_id, descripcion="Anulación de " + pago.sentido.lower())
            auditoria.registrar(tx, "anular_pago", p, entidad="pago", entidad_id=pago_id)
            return self._pagos.obtener(tx, pago_id)

    # ======================================================================= cuentas y cierres
    def cerrar_periodo(self, sesion_id: uuid.UUID, cuenta_id: uuid.UUID, periodo: date) -> CierreDTO:
        """Cierre mensual idempotente: si ya estaba cerrado devuelve ese cierre (no crea otro ni falla)."""
        with self._aut.operacion(sesion_id, Permiso.CUENTAS_CERRAR) as (tx, p):
            periodo = dc.periodo_de(periodo)
            if not self._cuentas.bloquear(tx, cuenta_id):
                raise NotFoundError(f"cuenta {cuenta_id}", user_message="La cuenta no existe.")
            existente = self._cuentas.cierre(tx, cuenta_id, periodo)
            if existente is not None:
                return existente
            dc.exigir_cerrable(periodo, ahora_pg(tx), self._cuentas.ultimo_cierre(tx, cuenta_id))
            resumen = self._cuentas.resumen_periodo(
                tx, cuenta_id, dc.inicio_periodo(periodo), dc.fin_periodo(periodo)
            )
            self._cuentas.crear_cierre(tx, cuenta_id, periodo, resumen)
            auditoria.registrar(
                tx,
                "cerrar_cuenta",
                p,
                entidad="cuenta_corriente",
                entidad_id=cuenta_id,
                periodo=periodo.isoformat(),
                saldo_final=str(resumen.saldo_final),
            )
            return self._cuentas.cierre(tx, cuenta_id, periodo)

    def cerrar_pendientes(self) -> int:
        """Cierre mensual AUTOMÁTICO (proceso técnico de la fase 8, sin sesión). Cierra, en orden, los meses
        terminados que cada cuenta todavía no cerró. Idempotente: UNIQUE(cuenta, período) y el chequeo previo
        de `cerrar` hacen que repetirlo, o correrlo en dos instancias, no cree un segundo cierre.

        Cada cuenta va en su propia transacción: un problema en una no impide cerrar las demás. Si alguna
        falló, al final se lanza un error (después de cerrar las otras) para que el planificador lo informe y
        reintente. Devuelve cuántos cierres creó.
        """
        with unit_of_work(self._conn) as tx:
            cuentas = self._cuentas.ids(tx)
        creados = 0
        fallidas: list[uuid.UUID] = []
        for cuenta_id in cuentas:
            try:
                creados += self._cerrar_cuenta_pendiente(cuenta_id)
            except Exception:  # noqa: BLE001 — se registra y se sigue con las demás cuentas
                log.exception("Cierre automático de la cuenta %s falló", cuenta_id)
                fallidas.append(cuenta_id)
        if creados:
            log.info("Cierre mensual automático: %d cierre(s)", creados)
        if fallidas:
            raise BusinessRuleError(f"cierre automático: {len(fallidas)} cuenta(s) con error: {fallidas}",
                                    user_message=f"No se cerraron {len(fallidas)} cuenta(s); ver logs.")
        return creados

    def _cerrar_cuenta_pendiente(self, cuenta_id: uuid.UUID) -> int:
        """Cierra los meses pendientes de UNA cuenta, en su propia transacción. Devuelve cuántos cerró."""
        creados = 0
        with unit_of_work(self._conn) as tx:
            self._cuentas.bloquear(tx, cuenta_id)  # mismo lock que los movimientos y el cierre manual
            ahora = ahora_pg(tx)
            for periodo in meses_a_cerrar(self._cuentas.primer_movimiento(tx, cuenta_id),
                                          self._cuentas.ultimo_cierre(tx, cuenta_id), ahora):
                resumen = self._cuentas.resumen_periodo(tx, cuenta_id, dc.inicio_periodo(periodo),
                                                        dc.fin_periodo(periodo))
                if self._cuentas.crear_cierre(tx, cuenta_id, periodo, resumen):
                    creados += 1
                    auditoria.registrar(tx, "cierre_automatico", None, entidad="cuenta_corriente",
                                        entidad_id=cuenta_id, periodo=periodo.isoformat(),
                                        saldo_final=str(resumen.saldo_final))
        return creados

    # ======================================================================= consultas (con alcance)
    def cuentas(self, sesion_id: uuid.UUID, *, tercero_id: uuid.UUID | None = None) -> list[CuentaDTO]:
        with self._aut.operacion(sesion_id, Permiso.CUENTAS_CONSULTAR) as (tx, p):
            return self._cuentas.listar(tx, alcance=_alcance(p), tercero_id=tercero_id)

    def movimientos(
        self,
        sesion_id: uuid.UUID,
        cuenta_id: uuid.UUID,
        *,
        limite: int = 50,
        antes: tuple[datetime, uuid.UUID] | None = None,
    ) -> PaginaMovimientos:
        if not 1 <= limite <= 500:
            raise ValidationError("límite", user_message="Tamaño de página inválido.")
        with self._aut.operacion(sesion_id, Permiso.CUENTAS_CONSULTAR) as (tx, p):
            self._exigir_cuenta_visible(tx, p, cuenta_id)
            filas = self._cuentas.movimientos(tx, cuenta_id, limite=limite + 1, antes=antes)
            pagina = filas[:limite]
            siguiente = (pagina[-1].fecha, pagina[-1].id) if len(filas) > limite else None
            return PaginaMovimientos(pagina, siguiente)

    def cierres(self, sesion_id: uuid.UUID, cuenta_id: uuid.UUID) -> list[CierreDTO]:
        with self._aut.operacion(sesion_id, Permiso.CUENTAS_CONSULTAR) as (tx, p):
            self._exigir_cuenta_visible(tx, p, cuenta_id)
            return self._cuentas.cierres(tx, cuenta_id)

    def facturas(
        self,
        sesion_id: uuid.UUID,
        *,
        sentido: str | None = None,
        estado: str | None = None,
        envio_id: uuid.UUID | None = None,
    ) -> list[FacturaDTO]:
        with self._aut.operacion(sesion_id, Permiso.CUENTAS_CONSULTAR, Permiso.FACTURAS_EMITIR) as (tx, p):
            return self._facturas.listar(
                tx, alcance=_alcance(p), sentido=sentido, estado=estado, envio_id=envio_id
            )

    def factura(self, sesion_id: uuid.UUID, factura_id: uuid.UUID) -> FacturaDTO:
        with self._aut.operacion(sesion_id, Permiso.CUENTAS_CONSULTAR, Permiso.FACTURAS_EMITIR) as (tx, p):
            f = self._facturas.obtener(tx, factura_id, con_renglones=True)
            if f is None or not _visible(_alcance(p), f.tercero_id, dc.naturaleza_factura(f.sentido)):
                raise NotFoundError(f"factura {factura_id}", user_message="La factura no existe.")
            return f

    def pagos(self, sesion_id: uuid.UUID, *, tercero_id: uuid.UUID | None = None) -> list[PagoDTO]:
        with self._aut.operacion(sesion_id, Permiso.CUENTAS_CONSULTAR, Permiso.PAGOS_REGISTRAR) as (tx, p):
            return self._pagos.listar(tx, alcance=_alcance(p), tercero_id=tercero_id)

    def aplicaciones(
        self, sesion_id: uuid.UUID, *, pago_id: uuid.UUID | None = None, factura_id: uuid.UUID | None = None
    ) -> list[AplicacionDTO]:
        with self._aut.operacion(sesion_id, Permiso.CUENTAS_CONSULTAR, Permiso.PAGOS_APLICAR) as (tx, p):
            alcance = _alcance(p)
            if pago_id is not None:
                pago = self._pagos.obtener(tx, pago_id)
                if pago is None or not _visible(alcance, pago.tercero_id, dc.naturaleza_pago(pago.sentido)):
                    raise NotFoundError(f"pago {pago_id}", user_message="El pago no existe.")
                return self._aplicaciones.de_pago(tx, pago_id)
            if factura_id is not None:
                f = self._facturas.obtener(tx, factura_id)
                if f is None or not _visible(alcance, f.tercero_id, dc.naturaleza_factura(f.sentido)):
                    raise NotFoundError(f"factura {factura_id}", user_message="La factura no existe.")
                return self._aplicaciones.de_factura(tx, factura_id)
            raise ValidationError("sin filtro", user_message="Indique un pago o una factura.")

    # ======================================================================= auxiliares
    def _envio(self, tx: Any, envio_id: uuid.UUID, *, bloquear: bool = False) -> Any:
        envio = self._envios.obtener(tx, envio_id, bloquear=bloquear)
        if envio is None:
            raise NotFoundError(f"envío {envio_id}", user_message="El envío no existe.")
        return envio

    def _exigir_cuenta_visible(self, tx: Any, p: Principal, cuenta_id: uuid.UUID) -> None:
        # Ajena o inexistente dan la MISMA respuesta: un externo no puede sondear qué cuentas existen.
        cuenta = self._cuentas.obtener(tx, cuenta_id)
        if cuenta is None or not _visible(_alcance(p), cuenta.tercero_id, cuenta.naturaleza):
            raise NotFoundError(
                f"cuenta {cuenta_id} inexistente o fuera de alcance de {p.usuario_id}",
                user_message="La cuenta no existe.",
            )


def _alcance(p: Principal) -> AlcanceComercial:
    """Internos ven todo. Un cliente, su cuenta por cobrar (lo que nos debe); un operador, su cuenta por pagar
    (lo que le debemos): ninguno ve lo del otro lado (§11)."""
    if p.es_interno:
        return AlcanceComercial(None)
    naturalezas = set()
    if Rol.CLIENTE in p.roles:
        naturalezas.add(dc.Naturaleza.COBRAR.value)
    if Rol.OPERADOR_LOGISTICO in p.roles:
        naturalezas.add(dc.Naturaleza.PAGAR.value)
    return AlcanceComercial(p.tercero_id, frozenset(naturalezas))


def _visible(alcance: AlcanceComercial, tercero_id: uuid.UUID, naturaleza: str) -> bool:
    return alcance.tercero_id is None or (
        alcance.tercero_id == tercero_id and naturaleza in alcance.naturalezas
    )
