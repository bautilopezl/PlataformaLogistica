"""Fase 6 — concurrencia comercial con DOS conexiones reales y transacciones confirmadas.

Los movimientos de cuenta y la auditoría son inmutables (ni se modifican ni se borran), así que estas pruebas
NO usan la base de desarrollo: crean una base PostgreSQL nueva con nombre UUID, le aplican los scripts de
`db/` y la eliminan al terminar (mismo patrón que `test_fase5_chat_ui.py`). Cada carrera arranca las dos
operaciones juntas con una barrera; los bloqueos de PostgreSQL deciden el orden.

Aceptación (AGENTS.md §17): doble facturación ⇒ una factura; sobre-aplicación ⇒ rechazada; cierre repetido ⇒
uno.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import text

from logistica.core.config import SecurityConfig, load_config
from logistica.core.errors import BusinessRuleError
from logistica.domain.comercial import CargoDatos, PagoDatos
from logistica.repositories.connections import Connections
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.outbox import FACTURACION
from logistica.services.acceso_service import AccesoService
from logistica.services.autorizacion import Autorizador
from logistica.services.comercial_service import ComercialService
from logistica.services.sincronizacion_service import SincronizacionService
from tests.test_fase1_acceso import CLAVE
from tests.test_fase2_logistica import Logistica, h

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="requiere LOGISTICA_TEST_CONNECTIONS=1 y una identidad de administración que pueda crear bases",
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bases():
    """Base PostgreSQL exclusiva (se borra al final) y dos `Connections` independientes hacia ella."""
    cfg = load_config()
    nombre = "test_comercial_" + uuid.uuid4().hex
    pg = cfg.postgres_admin
    admin = psycopg.connect(
        host=pg.host, port=pg.port, dbname=pg.database, user=pg.user, password=pg.password, autocommit=True
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(nombre)))
    config = replace(
        cfg,
        postgres_app=replace(cfg.postgres_app, database=nombre),
        postgres_admin=replace(cfg.postgres_admin, database=nombre),
    )
    c1, c2 = Connections(config), Connections(config)
    try:
        with psycopg.connect(
            host=pg.host, port=pg.port, dbname=nombre, user=pg.user, password=pg.password
        ) as ddl:
            for archivo in sorted((ROOT / "db" / "postgres").glob("*.sql")):
                ddl.execute(archivo.read_text(encoding="utf-8"))
        yield c1, c2
    finally:
        c1.close()
        c2.close()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(nombre)))
        admin.close()


class Dos(Logistica):
    """Entorno de la fase 2 en la conexión 1, y el mismo usuario Comercial con su propia sesión en la 2."""

    def __init__(self, c1, c2):
        super().__init__(c1)
        self.com1 = ComercialService(c1, self.aut)
        self.s1 = self.login(self.emails["COMERCIAL"]).sesion_id
        aut2 = Autorizador(c2)
        self.com2 = ComercialService(c2, aut2)
        acceso2 = AccesoService(c2, self.hasher, SecurityConfig(), uuid.uuid4(), aut2)
        self.s2 = acceso2.login(self.emails["COMERCIAL"], CLAVE, "segunda instancia").sesion_id
        self.c2 = c2

    def entregado(self, monto="1000"):
        s = self.ops.sesion_id
        p = self.planificado()
        self.com1.crear_cargo(self.s1, p.id, CargoDatos("Flete", "1", monto, "USD"))
        t1, t2 = self.env.obtener(s, p.id).tramos
        self.env.iniciar_tramo(s, t1.id, h(0.5))
        self.env.finalizar_tramo(s, t1.id, h(5))
        self.env.iniciar_tramo(s, t2.id, h(6))
        self.env.finalizar_tramo(s, t2.id, h(80))
        self.env.confirmar_entrega(s, p.id, h(81))
        return p


@pytest.fixture()
def d(bases):
    return Dos(*bases)


def a_la_vez(*funciones):
    """Corre las funciones en hilos que arrancan juntos. Devuelve (resultado | excepción) de cada una."""
    barrera = Barrier(len(funciones))

    def correr(f):
        barrera.wait(timeout=10)
        try:
            return f()
        except Exception as exc:  # noqa: BLE001 — la prueba decide qué excepción esperaba
            return exc

    with ThreadPoolExecutor(max_workers=len(funciones)) as pool:
        return [x.result(timeout=30) for x in [pool.submit(correr, f) for f in funciones]]


def contar(conns, consulta, **params):
    with conns.pg_session() as s:
        return s.execute(text(consulta), params).scalar_one()


def test_dos_trabajadores_procesan_la_entrega_y_se_emite_una_sola_factura(d, bases):
    c1, c2 = bases
    p = d.entregado()
    w1 = SincronizacionService(c1, facturacion=d.com1.facturar_entrega)
    w2 = SincronizacionService(c2, facturacion=d.com2.facturar_entrega)
    r = a_la_vez(
        lambda: w1.procesar(FACTURACION, agregado_id=p.id), lambda: w2.procesar(FACTURACION, agregado_id=p.id)
    )
    assert sorted(x.procesados for x in r) == [0, 1]  # SKIP LOCKED: solo uno reclama la entrega
    assert contar(c1, "SELECT count(*) FROM factura_detalle WHERE envio_id = :e", e=p.id) == 1


def test_facturar_el_mismo_envio_a_la_vez_sin_outbox_tampoco_duplica(d, bases):
    """Aunque dos procesos ignoraran la outbox, los cargos bloqueados (FOR UPDATE) serializan: el segundo ya
    no encuentra cargos pendientes."""
    c1, c2 = bases
    p = d.entregado()
    usuario = d.login(d.emails["OPERACIONES"]).usuario_id

    def facturar(conns, com):
        def f():
            with unit_of_work(conns) as tx:
                return com.facturar_entrega(tx, p.id, usuario, uuid.uuid4())

        return f

    r = a_la_vez(facturar(c1, d.com1), facturar(c2, d.com2))
    assert sorted(len(x) for x in r) == [0, 1]
    assert contar(c1, "SELECT count(*) FROM factura_detalle WHERE envio_id = :e", e=p.id) == 1
    assert (
        contar(
            c1,
            "SELECT count(*) FROM movimiento_cuenta m JOIN factura_detalle fd ON fd.factura_id = m.factura_id"
            " WHERE fd.envio_id = :e",
            e=p.id,
        )
        == 1
    )


def test_dos_aplicaciones_simultaneas_no_superan_el_saldo_de_la_factura(d):
    p = d.entregado("1000")
    w = SincronizacionService(d.conns, facturacion=d.com1.facturar_entrega)
    w.procesar(FACTURACION, agregado_id=p.id)
    [f] = d.com1.facturas(d.s1, envio_id=p.id)
    ahora = datetime.now(timezone.utc)
    pagos = [
        d.com1.registrar_pago(
            d.s1, PagoDatos(d.cliente_sa.id, "COBRO", ahora, "600", "USD", "OTRO"), uuid.uuid4()
        )
        for _ in range(2)
    ]
    r = a_la_vez(
        lambda: d.com1.aplicar_pago(d.s1, pagos[0].id, f.id, "600"),
        lambda: d.com2.aplicar_pago(d.s2, pagos[1].id, f.id, "600"),
    )
    errores = [x for x in r if isinstance(x, Exception)]
    assert len(errores) == 1 and isinstance(errores[0], BusinessRuleError)  # 600 + 600 > 1000
    assert d.com1.factura(d.s1, f.id).aplicado == Decimal("600.00")


def test_un_pago_aplicado_a_la_vez_a_dos_facturas_no_supera_su_disponible(d):
    p1, p2 = d.entregado("1000"), None
    w = SincronizacionService(d.conns, facturacion=d.com1.facturar_entrega)
    w.procesar(FACTURACION, agregado_id=p1.id)
    e = d.nuevo_envio(peso="4000", volumen="8")  # segundo envío en el otro contenedor
    d.ruta(e.id)
    d.asignar(e.id, d.cont2, peso="4000", volumen="8")
    p2 = d.env.planificar(d.ops.sesion_id, e.id)
    d.com1.crear_cargo(d.s1, p2.id, CargoDatos("Flete", "1", "1000", "USD"))
    t1, t2 = d.env.obtener(d.ops.sesion_id, p2.id).tramos
    for paso in (
        (d.env.iniciar_tramo, t1.id, h(0.5)),
        (d.env.finalizar_tramo, t1.id, h(5)),
        (d.env.iniciar_tramo, t2.id, h(6)),
        (d.env.finalizar_tramo, t2.id, h(80)),
    ):
        paso[0](d.ops.sesion_id, paso[1], paso[2])
    d.env.confirmar_entrega(d.ops.sesion_id, p2.id, h(81))
    w.procesar(FACTURACION, agregado_id=p2.id)
    [f1], [f2] = d.com1.facturas(d.s1, envio_id=p1.id), d.com1.facturas(d.s1, envio_id=p2.id)
    pago = d.com1.registrar_pago(
        d.s1,
        PagoDatos(d.cliente_sa.id, "COBRO", datetime.now(timezone.utc), "1000", "USD", "OTRO"),
        uuid.uuid4(),
    )
    r = a_la_vez(
        lambda: d.com1.aplicar_pago(d.s1, pago.id, f1.id, "700"),
        lambda: d.com2.aplicar_pago(d.s2, pago.id, f2.id, "700"),
    )
    assert sum(isinstance(x, BusinessRuleError) for x in r) == 1
    assert d.com1.pagos(d.s1, tercero_id=d.cliente_sa.id)[0].aplicado == Decimal("700.00")


def test_cierre_repetido_a_la_vez_deja_un_solo_cierre(d, bases):
    c1, _ = bases
    hoy = date.today().replace(day=1)
    anterior = date(hoy.year - 1, 12, 1) if hoy.month == 1 else date(hoy.year, hoy.month - 1, 1)
    pago = d.com1.registrar_pago(
        d.s1,
        PagoDatos(d.cliente_sa.id, "COBRO", datetime.now(timezone.utc), "10", "USD", "OTRO"),
        uuid.uuid4(),
    )
    [cuenta] = d.com1.cuentas(d.s1, tercero_id=d.cliente_sa.id)
    r = a_la_vez(
        lambda: d.com1.cerrar_periodo(d.s1, cuenta.id, anterior),
        lambda: d.com2.cerrar_periodo(d.s2, cuenta.id, anterior),
    )
    assert not any(isinstance(x, Exception) for x in r), r
    assert r[0].id == r[1].id  # el segundo devuelve el mismo cierre (idempotente)
    assert contar(c1, "SELECT count(*) FROM cierre_cuenta WHERE cuenta_id = :c", c=cuenta.id) == 1
    assert pago.id is not None


def test_emisiones_simultaneas_reciben_numeros_distintos_y_correlativos(d, bases):
    c1, c2 = bases
    envios = [d.entregado("100")]
    e = d.nuevo_envio(peso="4000", volumen="8")
    d.ruta(e.id)
    d.asignar(e.id, d.cont2, peso="4000", volumen="8")
    p2 = d.env.planificar(d.ops.sesion_id, e.id)
    d.com1.crear_cargo(d.s1, p2.id, CargoDatos("Flete", "1", "100", "USD"))
    t1, t2 = d.env.obtener(d.ops.sesion_id, p2.id).tramos
    for f, t, x in (
        (d.env.iniciar_tramo, t1.id, h(0.5)),
        (d.env.finalizar_tramo, t1.id, h(5)),
        (d.env.iniciar_tramo, t2.id, h(6)),
        (d.env.finalizar_tramo, t2.id, h(80)),
    ):
        f(d.ops.sesion_id, t, x)
    d.env.confirmar_entrega(d.ops.sesion_id, p2.id, h(81))
    envios.append(p2)
    w1 = SincronizacionService(c1, facturacion=d.com1.facturar_entrega)
    w2 = SincronizacionService(c2, facturacion=d.com2.facturar_entrega)
    r = a_la_vez(
        lambda: w1.procesar(FACTURACION, agregado_id=envios[0].id),
        lambda: w2.procesar(FACTURACION, agregado_id=envios[1].id),
    )
    assert [x.procesados for x in r] == [1, 1]  # las DOS se emitieron (ninguna chocó con el índice único)
    with c1.pg_session() as s:
        # Solo las de estos dos envíos: la base temporal es una para todo el módulo.
        numeros = (
            s.execute(
                text(
                    "SELECT DISTINCT f.numero FROM factura f JOIN factura_detalle d ON d.factura_id = f.id"
                    " WHERE d.envio_id = ANY(:e) ORDER BY f.numero"
                ),
                {"e": [x.id for x in envios]},
            )
            .scalars()
            .all()
        )
    assert len(numeros) == 2 and len(set(numeros)) == 2  # dos facturas, nunca el mismo número
    enteros = [int(n) for n in numeros]
    assert enteros == list(range(enteros[0], enteros[0] + len(enteros)))  # sin huecos
