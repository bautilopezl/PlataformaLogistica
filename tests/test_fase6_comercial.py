"""Fase 6 — comercial contra PostgreSQL real (PG_APP_USER), sin dejar datos (transacción revertida).

Aceptación (AGENTS.md §17): sin doble facturación, sin sobre-aplicación, cierre repetido idempotente y saldos
que reconcilian con los movimientos. La facturación automática se dispara al confirmar la entrega (decisión B,
§16.3). Las pruebas con dos conexiones simultáneas están en `test_fase6_concurrencia.py`.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from logistica.core.errors import BusinessRuleError, NotFoundError, PermissionDeniedError, ValidationError
from logistica.domain.comercial import CargoDatos, FacturaCompraDatos, PagoDatos, Renglon
from logistica.repositories.postgres.outbox import FACTURACION
from logistica.services.comercial_service import ComercialService
from logistica.services.sincronizacion_service import SincronizacionService
from tests.test_fase2_logistica import Logistica, h

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y PostgreSQL levantado)",
)


class Comercial(Logistica):
    def __init__(self, conns):
        super().__init__(conns)
        self.com = ComercialService(conns, self.aut)
        self.comercial = self.login(self.emails["COMERCIAL"]).sesion_id
        self.sync = SincronizacionService(conns, facturacion=self.com.facturar_entrega)

    def entregado(self, cargos=(("Flete", "1", "12000", "USD"),), facturar=True, segundo=False):
        """Envío entregado de punta a punta, con cargos cargados ANTES de la entrega. `segundo` usa el otro
        contenedor (dos envíos con el mismo contenedor y las mismas fechas chocarían por la exclusión
        temporal)."""
        s = self.ops.sesion_id
        if segundo:
            e = self.nuevo_envio(peso="4000", volumen="8")
            self.ruta(e.id)
            self.asignar(e.id, self.cont2, peso="4000", volumen="8")
            p = self.env.planificar(s, e.id)
        else:
            p = self.planificado()
        for c in cargos:
            self.com.crear_cargo(self.comercial, p.id, CargoDatos(*c))
        t1, t2 = self.env.obtener(s, p.id).tramos
        self.env.iniciar_tramo(s, t1.id, h(0.5))
        self.env.finalizar_tramo(s, t1.id, h(5))
        self.env.iniciar_tramo(s, t2.id, h(6))
        self.env.finalizar_tramo(s, t2.id, h(80))
        self.env.confirmar_entrega(s, p.id, h(81))
        if facturar:
            self.facturar(p.id)
        return p

    def facturar(self, envio_id):
        return self.sync.procesar(FACTURACION, agregado_id=envio_id)

    def facturas(self, envio_id):
        return self.com.facturas(self.comercial, envio_id=envio_id)

    def cuenta(self, tercero_id, naturaleza="COBRAR", moneda="USD"):
        [c] = [
            c
            for c in self.com.cuentas(self.comercial, tercero_id=tercero_id)
            if (c.naturaleza, c.moneda_codigo) == (naturaleza, moneda)
        ]
        return c

    def cobro(self, monto, *, tercero=None, moneda="USD", clave=None, sentido="COBRO", fecha=None):
        datos = PagoDatos(
            (tercero or self.cliente_sa).id,
            sentido,
            fecha or datetime.now(timezone.utc),
            monto,
            moneda,
            "TRANSFERENCIA",
        )
        return self.com.registrar_pago(self.comercial, datos, clave or uuid.uuid4())


@pytest.fixture()
def c(tx_connections):
    return Comercial(tx_connections)


def estado_entrega(c, envio_id, destino=FACTURACION):
    with c.conns.pg_session() as s:
        return s.execute(
            text(
                "SELECT x.estado, x.intentos FROM outbox_entrega x JOIN outbox_evento e"
                " ON e.id = x.evento_id WHERE e.agregado_id = :e AND x.destino = :d"
            ),
            {"e": envio_id, "d": destino},
        ).one()


# --------------------------------------------------------------------------- facturación automática
def test_la_entrega_dispara_una_factura_por_moneda_con_su_movimiento(c):
    p = c.entregado(
        cargos=(
            ("Flete", "1", "12000", "USD"),
            ("Seguro", "1", "300", "ARS"),
            ("Estiba", "2", "150.5", "USD"),
        ),
        facturar=False,
    )
    assert c.facturas(p.id) == []  # confirmar la entrega no factura por sí sola (decisión B)
    assert c.facturar(p.id).procesados == 1
    facturas = {f.moneda_codigo: c.com.factura(c.comercial, f.id) for f in c.facturas(p.id)}
    assert sorted(facturas) == ["ARS", "USD"]
    usd = facturas["USD"]
    assert (usd.estado, usd.importe_total, usd.sentido, usd.tercero_id) == (
        "EMITIDA",
        Decimal("12301.00"),
        "VENTA",
        c.cliente_sa.id,
    )
    assert [r.concepto for r in usd.renglones] == ["Flete", "Estiba"]
    assert usd.serie.startswith("FV-") and len(usd.numero) == 6
    assert {f.numero for f in facturas.values()} == {
        f"{int(usd.numero):06d}",
        f"{int(facturas['ARS'].numero):06d}",
    }
    assert {x.estado for x in c.com.cargos(c.comercial, p.id)} == {"FACTURADO"}
    assert c.cuenta(c.cliente_sa.id).saldo == Decimal("12301.00")
    assert c.cuenta(c.cliente_sa.id, moneda="ARS").saldo == Decimal("300.00")
    assert estado_entrega(c, p.id)[0] == "COMPLETADA"
    with c.conns.pg_session() as s:
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE accion = 'factura_automatica' AND entidad_id = :f"
                ),
                {"f": usd.id},
            ).scalar_one()
            == 1
        )


def test_una_reentrega_del_evento_no_factura_dos_veces(c):
    p = c.entregado()
    with c.conns.pg_session() as s:
        s.execute(
            text(
                "UPDATE outbox_entrega x SET estado = 'PENDIENTE', procesada_en = NULL,"
                " proximo_intento_en = clock_timestamp() FROM outbox_evento e WHERE e.id = x.evento_id"
                " AND e.agregado_id = :e AND x.destino = :d"
            ),
            {"e": p.id, "d": FACTURACION},
        )
    assert c.facturar(p.id).procesados == 1  # se reprocesa…
    assert len(c.facturas(p.id)) == 1  # …pero ya no hay cargos pendientes: no hay otra factura
    assert c.cuenta(c.cliente_sa.id).saldo == Decimal("12000.00")


def test_sin_cargos_no_se_factura_y_la_entrega_se_completa(c):
    p = c.entregado(cargos=())
    assert c.facturas(p.id) == [] and estado_entrega(c, p.id)[0] == "COMPLETADA"


def test_un_fallo_de_facturacion_no_bloquea_la_entrega_y_se_reintenta(c, monkeypatch):
    p = c.entregado(facturar=False)
    assert c.env.obtener(c.ops.sesion_id, p.id).envio.estado == "ENTREGADO"
    original = c.com.facturar_entrega

    def falla(*args):
        raise BusinessRuleError("falla simulada de facturación")

    c.sync._facturacion = falla
    assert c.facturar(p.id).fallidos == 1
    estado, intentos = estado_entrega(c, p.id)
    assert (estado, intentos) == ("PENDIENTE", 1) and c.facturas(p.id) == []  # nada a medias
    c.sync._facturacion = original
    with c.conns.pg_session() as s:
        s.execute(
            text(
                "UPDATE outbox_entrega SET proximo_intento_en = clock_timestamp() WHERE destino = :d"
                " AND evento_id IN (SELECT id FROM outbox_evento WHERE agregado_id = :e)"
            ),
            {"d": FACTURACION, "e": p.id},
        )
    assert c.facturar(p.id).procesados == 1 and len(c.facturas(p.id)) == 1


# --------------------------------------------------------------------------- facturación manual y anulación
def test_cargo_posterior_a_la_entrega_se_factura_a_mano(c):
    p = c.entregado()
    extra = c.com.crear_cargo(c.comercial, p.id, CargoDatos("Demora en destino", "1", "500", "USD"))
    c.facturar(p.id)  # el disparador ya ocurrió: no lo toma
    assert c.com.cargos(c.comercial, p.id)[-1].estado == "PENDIENTE"
    [f] = c.com.emitir_factura(c.comercial, p.id, [extra.id])
    assert f.importe_total == Decimal("500.00") and len(c.facturas(p.id)) == 2
    with pytest.raises(BusinessRuleError, match="sin cargos"):
        c.com.emitir_factura(c.comercial, p.id)


def test_no_se_factura_a_mano_un_envio_sin_entregar(c):
    e = c.nuevo_envio()
    cargo = c.com.crear_cargo(c.comercial, e.id, CargoDatos("Flete", "1", "10", "USD"))
    with pytest.raises(BusinessRuleError, match="no entregado"):
        c.com.emitir_factura(c.comercial, e.id, [cargo.id])


def test_anular_con_refacturacion_crea_reemplazos_y_no_reutiliza_el_numero(c):
    p = c.entregado()
    [f] = c.facturas(p.id)
    with pytest.raises(ValidationError):
        c.com.anular_factura(c.comercial, f.id, " ", refacturar=True)
    anulada, reemplazos = c.com.anular_factura(c.comercial, f.id, "Precio mal cargado", refacturar=True)
    assert anulada.estado == "ANULADA" and anulada.motivo_anulacion == "Precio mal cargado"
    assert c.cuenta(c.cliente_sa.id).saldo == 0  # la reversa compensa
    cargos = {x.id: x for x in c.com.cargos(c.comercial, p.id)}
    [original] = [x for x in cargos.values() if x.factura_id == f.id]
    assert original.estado == "CANCELADO"  # SUPUESTO C7: queda atado a la factura anulada
    [nuevo] = reemplazos
    assert (nuevo.estado, nuevo.importe, nuevo.factura_id) == ("PENDIENTE", Decimal("12000.00"), None)
    [refacturada] = c.com.emitir_factura(c.comercial, p.id, [nuevo.id])
    assert refacturada.numero != anulada.numero  # un número anulado no se reutiliza
    assert c.cuenta(c.cliente_sa.id).saldo == Decimal("12000.00")
    with pytest.raises(BusinessRuleError):
        c.com.anular_factura(c.comercial, f.id, "otra vez", refacturar=False)


def test_anular_sin_refacturar_no_crea_cargos(c):
    p = c.entregado()
    [f] = c.facturas(p.id)
    _, reemplazos = c.com.anular_factura(c.comercial, f.id, "No correspondía cobrarlo", refacturar=False)
    assert reemplazos == [] and [x.estado for x in c.com.cargos(c.comercial, p.id)] == ["CANCELADO"]


def test_no_se_anula_una_factura_con_pagos_aplicados(c):
    p = c.entregado()
    [f] = c.facturas(p.id)
    pago = c.cobro("1000")
    aplicacion = c.com.aplicar_pago(c.comercial, pago.id, f.id, "1000")
    with pytest.raises(BusinessRuleError, match="pagos"):
        c.com.anular_factura(c.comercial, f.id, "error", refacturar=False)
    c.com.anular_aplicacion(c.comercial, aplicacion.id, "Se aplicó a la factura equivocada")
    assert c.com.anular_factura(c.comercial, f.id, "error", refacturar=False)[0].estado == "ANULADA"
    assert c.com.pagos(c.comercial)[0].disponible == Decimal("1000.00")  # el pago quedó libre


# --------------------------------------------------------------------------- pagos y aplicaciones
def test_aplicacion_parcial_y_multiple_y_saldos_que_reconcilian(c):
    p1 = c.entregado(cargos=(("Flete", "1", "1000", "USD"),))
    p2 = c.entregado(cargos=(("Flete", "1", "500", "USD"),), segundo=True)
    [f1], [f2] = c.facturas(p1.id), c.facturas(p2.id)
    a = c.cobro("700")
    b = c.cobro("900")
    c.com.aplicar_pago(c.comercial, a.id, f1.id, "400")  # un pago en dos facturas
    c.com.aplicar_pago(c.comercial, a.id, f2.id, "300")
    c.com.aplicar_pago(c.comercial, b.id, f1.id, "600")  # dos pagos en una factura
    assert c.com.factura(c.comercial, f1.id).pendiente == 0
    assert c.com.factura(c.comercial, f2.id).pendiente == Decimal("200.00")
    with pytest.raises(BusinessRuleError, match="supera el saldo"):
        c.com.aplicar_pago(c.comercial, b.id, f2.id, "200.01")  # supera el saldo de la factura
    with pytest.raises(BusinessRuleError, match="disponible"):
        c.com.aplicar_pago(c.comercial, a.id, f2.id, "1")  # el pago A ya no tiene disponible
    with pytest.raises(BusinessRuleError):  # ya hay una aplicación vigente del par (pago B, f1)
        c.com.aplicar_pago(c.comercial, b.id, f1.id, "1")
    assert c.cuenta(c.cliente_sa.id).saldo == Decimal("1500") - Decimal("1600")  # saldo a favor de 100
    with c.conns.pg_session() as s:
        derivado = s.execute(
            text(
                "SELECT sum(CASE WHEN m.sentido = 'AUMENTA' THEN m.importe ELSE -m.importe END) FROM "
                "movimiento_cuenta m"
                " JOIN cuenta_corriente k ON k.id = m.cuenta_id WHERE k.tercero_id = :t AND k.moneda_codigo "
                "= 'USD'"
            ),
            {"t": c.cliente_sa.id},
        ).scalar_one()
    assert derivado == Decimal("-100.00")


def test_pago_idempotente_por_clave(c):
    clave = uuid.uuid4()
    a = c.cobro("100", clave=clave)
    assert c.cobro("100", clave=clave).id == a.id  # reintento: el mismo pago
    with pytest.raises(ValidationError, match="clave"):
        c.cobro("101", clave=clave)
    assert c.cuenta(c.cliente_sa.id).saldo == Decimal("-100.00")  # un solo movimiento


def test_aplicacion_exige_mismo_tercero_moneda_y_sentido(c):
    p = c.entregado()
    [f] = c.facturas(p.id)
    with pytest.raises(BusinessRuleError, match="moneda"):
        c.com.aplicar_pago(c.comercial, c.cobro("10", moneda="ARS").id, f.id, "10")
    pago_operador = c.cobro("10", tercero=c.operador_sa, sentido="PAGO")
    with pytest.raises(BusinessRuleError):
        c.com.aplicar_pago(c.comercial, pago_operador.id, f.id, "10")
    with pytest.raises(BusinessRuleError, match="tercero sin rol"):
        c.cobro("10", tercero=c.operador_sa)  # un cobro es de un cliente


def test_anular_pago_solo_sin_aplicaciones_y_con_reversa(c):
    p = c.entregado()
    [f] = c.facturas(p.id)
    pago = c.cobro("100")
    aplic = c.com.aplicar_pago(c.comercial, pago.id, f.id, "100")
    with pytest.raises(BusinessRuleError, match="aplicado"):
        c.com.anular_pago(c.comercial, pago.id, "rebotó")
    c.com.anular_aplicacion(c.comercial, aplic.id, "rebotó")
    assert c.com.anular_pago(c.comercial, pago.id, "Transferencia rechazada").estado == "ANULADO"
    assert c.cuenta(c.cliente_sa.id).saldo == Decimal("12000.00")
    with pytest.raises(BusinessRuleError, match="anulado"):
        c.com.aplicar_pago(c.comercial, pago.id, f.id, "1")


# --------------------------------------------------------------------------- facturas de compra
def test_factura_de_compra_aumenta_lo_que_debemos_al_operador(c):
    ahora = datetime.now(timezone.utc)
    datos = FacturaCompraDatos(
        c.operador_sa.id,
        "A-0001",
        "000123",
        ahora,
        ahora + timedelta(days=15),
        "USD",
        [Renglon("Tramo marítimo", "1", "9000")],
    )
    f = c.com.registrar_factura_compra(c.comercial, datos)
    assert (f.sentido, f.importe_total) == ("COMPRA", Decimal("9000.00"))
    assert c.cuenta(c.operador_sa.id, naturaleza="PAGAR").saldo == Decimal("9000.00")
    with pytest.raises(ValidationError, match="compra duplicada"):
        c.com.registrar_factura_compra(c.comercial, datos)
    with pytest.raises(BusinessRuleError):
        c.com.registrar_factura_compra(
            c.comercial,
            FacturaCompraDatos(c.cliente_sa.id, "A", "1", ahora, ahora, "USD", [Renglon("x", "1", "1")]),
        )
    pago = c.cobro("4000", tercero=c.operador_sa, sentido="PAGO")
    c.com.aplicar_pago(c.comercial, pago.id, f.id, "4000")
    assert c.cuenta(c.operador_sa.id, naturaleza="PAGAR").saldo == Decimal("5000.00")


# --------------------------------------------------------------------------- cierres
def _movimiento_historico(c, tercero_id, fecha, monto, sentido="COBRO"):
    """Un pago y su movimiento en un mes pasado (el servicio siempre fecha con el reloj actual)."""
    with c.conns.pg_session() as s:
        usuario = s.execute(text("SELECT id FROM usuario LIMIT 1")).scalar_one()
        cuenta = s.execute(
            text(
                "SELECT id FROM cuenta_corriente WHERE tercero_id = :t AND naturaleza = 'COBRAR'"
                " AND moneda_codigo = 'USD'"
            ),
            {"t": tercero_id},
        ).scalar_one_or_none()
        if cuenta is None:
            cuenta = uuid.uuid4()
            s.execute(
                text(
                    "INSERT INTO cuenta_corriente (id, tercero_id, naturaleza, moneda_codigo)"
                    " VALUES (:id, :t, 'COBRAR', 'USD')"
                ),
                {"id": cuenta, "t": tercero_id},
            )
        pago = uuid.uuid4()
        s.execute(
            text(
                "INSERT INTO pago (id, tercero_id, sentido, fecha, monto, moneda_codigo, medio,"
                " registrado_por_id, clave_idempotencia) VALUES (:id, :t, :s, :f, :m, 'USD', 'OTRO', :u, :k)"
            ),
            {
                "id": pago,
                "t": tercero_id,
                "s": sentido,
                "f": fecha,
                "m": monto,
                "u": usuario,
                "k": uuid.uuid4(),
            },
        )
        s.execute(
            text(
                "INSERT INTO movimiento_cuenta (id, cuenta_id, fecha, sentido, importe, pago_id, "
                "clave_origen)"
                " VALUES (:id, :c, :f, 'DISMINUYE', :m, :p, :k)"
            ),
            {"id": uuid.uuid4(), "c": cuenta, "f": fecha, "m": monto, "p": pago, "k": f"pago:{pago}"},
        )
        return cuenta


def _mes(delta):
    hoy = date.today().replace(day=1)
    anio, mes = divmod(hoy.year * 12 + hoy.month - 1 + delta, 12)
    return date(anio, mes + 1, 1)


def test_cierre_mensual_idempotente_en_orden_y_sin_movimientos_retroactivos(c):
    hace2, hace1 = _mes(-2), _mes(-1)
    t = c.cliente_sa.id
    cuenta = _movimiento_historico(c, t, datetime(hace2.year, hace2.month, 10, tzinfo=timezone.utc), "100")
    _movimiento_historico(c, t, datetime(hace1.year, hace1.month, 5, tzinfo=timezone.utc), "40")
    k2 = c.com.cerrar_periodo(c.comercial, cuenta, hace2)
    assert (k2.saldo_inicial, k2.total_aumentos, k2.total_disminuciones, k2.saldo_final) == (0, 0, 100, -100)
    assert c.com.cerrar_periodo(c.comercial, cuenta, hace2).id == k2.id  # repetido: el mismo cierre
    with c.conns.pg_session() as s:  # y se audita una sola vez: el repetido no "cierra" de nuevo
        assert (
            s.execute(
                text("SELECT count(*) FROM auditoria WHERE accion = 'cerrar_cuenta' AND entidad_id = :c"),
                {"c": cuenta},
            ).scalar_one()
            == 1
        )
    k1 = c.com.cerrar_periodo(c.comercial, cuenta, hace1)
    assert (k1.saldo_inicial, k1.saldo_final) == (Decimal("-100.00"), Decimal("-140.00"))  # encadena
    with pytest.raises(BusinessRuleError, match="periodo abierto"):
        c.com.cerrar_periodo(c.comercial, cuenta, _mes(0))
    with pytest.raises(BusinessRuleError, match="fuera de orden"):
        c.com.cerrar_periodo(c.comercial, cuenta, _mes(-3))
    with pytest.raises(BusinessRuleError, match="cerrado"):  # un pago fechado en un mes cerrado
        c.cobro("5", fecha=datetime(hace1.year, hace1.month, 20, tzinfo=timezone.utc))
    assert [x.periodo for x in c.com.cierres(c.comercial, cuenta)] == [hace1, hace2]


# --------------------------------------------------------------------------- permisos y alcance
def test_permisos_y_alcance_comercial(c):
    p = c.entregado()
    [f] = c.facturas(p.id)
    ahora = datetime.now(timezone.utc)
    compra = c.com.registrar_factura_compra(
        c.comercial,
        FacturaCompraDatos(c.operador_sa.id, "B", "77", ahora, ahora, "USD", [Renglon("Tramo", "1", "800")]),
    )
    cliente = c.login(c.emails["CLIENTE"]).sesion_id
    operador = c.login(c.emails["OPERADOR_LOGISTICO"]).sesion_id
    with pytest.raises(PermissionDeniedError):  # Operaciones no tiene permisos comerciales
        c.com.crear_cargo(c.ops.sesion_id, p.id, CargoDatos("x", "1", "1", "USD"))
    with pytest.raises(PermissionDeniedError):
        c.com.anular_factura(cliente, f.id, "no", refacturar=False)
    assert [(k.naturaleza, k.tercero_id) for k in c.com.cuentas(cliente)] == [("COBRAR", c.cliente_sa.id)]
    assert [x.id for x in c.com.facturas(cliente)] == [f.id]  # el cliente no ve lo que se le paga al operador
    assert [x.id for x in c.com.facturas(operador)] == [compra.id]  # ni el operador lo cobrado al cliente
    with pytest.raises(NotFoundError):
        c.com.factura(operador, f.id)
    cuenta_cliente = c.cuenta(c.cliente_sa.id).id
    with pytest.raises(NotFoundError):  # ajena = inexistente para un externo
        c.com.movimientos(operador, cuenta_cliente)
    assert len(c.com.movimientos(cliente, cuenta_cliente).movimientos) == 1


def test_movimientos_paginados(c):
    for monto in ("1", "2", "3"):
        c.cobro(monto)
    cuenta = c.cuenta(c.cliente_sa.id).id
    p1 = c.com.movimientos(c.comercial, cuenta, limite=2)
    p2 = c.com.movimientos(c.comercial, cuenta, limite=2, antes=p1.siguiente)
    assert len(p1.movimientos) == 2 and len(p2.movimientos) == 1 and p2.siguiente is None
    assert {m.id for m in p1.movimientos}.isdisjoint({m.id for m in p2.movimientos})


def test_empresa_cliente_y_operador_cada_perfil_ve_solo_su_lado(c):
    """Un tercero puede ser cliente y operador (§8.1). El usuario OPERADOR_LOGISTICO de esa empresa ve lo que
    le debemos (por pagar), nunca lo que la empresa nos debe como cliente (por cobrar)."""
    from logistica.repositories.postgres.terceros import TerceroRepository

    with c.conns.pg_session() as s:
        TerceroRepository().set_roles(s, c.operador_sa.id, {"OPERADOR", "CLIENTE"})
    c.cobro("50", tercero=c.operador_sa)  # crea su cuenta POR COBRAR (como cliente)
    c.cobro("70", tercero=c.operador_sa, sentido="PAGO")  # y su cuenta POR PAGAR (como operador)
    operador = c.login(c.emails["OPERADOR_LOGISTICO"]).sesion_id
    assert [k.naturaleza for k in c.com.cuentas(operador)] == ["PAGAR"]
    assert [x.sentido for x in c.com.pagos(operador)] == ["PAGO"]


# --------------------------------------------------------------------------- hallazgos de la revisión
def test_cuenta_ajena_no_revela_si_existe(c):
    """Para un externo, una cuenta de otro y una inexistente dan la misma respuesta (no se puede sondear)."""
    c.cobro("10")
    cuenta = c.cuenta(c.cliente_sa.id).id
    operador = c.login(c.emails["OPERADOR_LOGISTICO"]).sesion_id
    for cuenta_id in (cuenta, uuid.uuid4()):
        with pytest.raises(NotFoundError):
            c.com.movimientos(operador, cuenta_id)
        with pytest.raises(NotFoundError):
            c.com.cierres(operador, cuenta_id)


def test_clave_de_pago_reutilizada_con_otro_medio_o_fecha_se_rechaza(c):
    clave = uuid.uuid4()
    fecha = datetime.now(timezone.utc)
    datos = PagoDatos(c.cliente_sa.id, "COBRO", fecha, "100", "USD", "TRANSFERENCIA", "TRF-1")
    c.com.registrar_pago(c.comercial, datos, clave)
    for cambio in ({"medio": "EFECTIVO"}, {"referencia": "OTRA"}):
        with pytest.raises(ValidationError, match="clave"):
            c.com.registrar_pago(c.comercial, PagoDatos(**{**datos.__dict__, **cambio}), clave)


COMERCIALES_SIN_BORRADO = (
    "factura",
    "factura_detalle",
    "pago",
    "pago_aplicacion",
    "cargo_envio",
    "cuenta_corriente",
)


@pytest.mark.parametrize("tabla", COMERCIALES_SIN_BORRADO)
def test_la_aplicacion_no_puede_borrar_registros_comerciales(c, tabla):
    """Las facturas y pagos se anulan, nunca se borran (§8.1): la identidad de ejecución no tiene DELETE."""
    with c.conns.pg_session() as s:
        permisos = s.execute(
            text(
                "SELECT has_table_privilege(current_user, :t, 'SELECT'), has_table_privilege(current_user, "
                ":t, 'INSERT'),"
                " has_table_privilege(current_user, :t, 'UPDATE'), has_table_privilege(current_user, :t, "
                "'DELETE')"
            ),
            {"t": tabla},
        ).one()
    assert tuple(permisos) == (True, True, True, False)
