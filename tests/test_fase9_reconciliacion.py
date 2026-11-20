"""Fase 9 — pruebas de `ReconciliacionService` (AGENTS.md §13/§14).

Corre contra PostgreSQL real dentro de una transacción que se revierte (`tx_connections`) y, para los casos
de Mongo/Neo4j, contra los motores reales con limpieza por `envio_id` al terminar — mismo patrón que
`tests/test_fase3_sincronizacion.py` y `tests/test_fase9_seed_demo.py`. Nunca corre contra la base de
desarrollo ni dejar nada en ella.

Las aserciones se enfocan en LO QUE CADA PRUEBA CREÓ (por código de envío / id), no en "cero discrepancias
en toda la base": la base real ya tiene datos del equipo y de la siembra de demostración, así que afirmar
"nada en absoluto" sería frágil. Reconciliación es de solo lectura: ninguna prueba espera que repare algo.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.outbox import FACTURACION, MONGO, NEO4J, OutboxRepository
from logistica.services.comercial_service import ComercialService
from logistica.services.reconciliacion_service import ReconciliacionService
from logistica.services.sincronizacion_service import SincronizacionService
from tests.test_fase1_acceso import CLAVE
from tests.test_fase2_logistica import Logistica, h

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y los tres motores levantados)",
)


@pytest.fixture()
def g(tx_connections):
    entorno = Logistica(tx_connections)
    entorno.envios_creados = []
    entorno.com = ComercialService(tx_connections, entorno.aut)
    entorno.com_sesion = entorno.login(entorno.emails["COMERCIAL"], CLAVE).sesion_id
    yield entorno
    _limpiar(entorno)


def _limpiar(g) -> None:
    ids = [str(x) for x in g.envios_creados]
    if not ids:
        return
    with g.conns.neo4j.session() as s:
        s.run("MATCH (t:Tramo) WHERE t.envio_id IN $ids DETACH DELETE t", ids=ids).consume()
        s.run("MATCH (e:Envio) WHERE e.id IN $ids DETACH DELETE e", ids=ids).consume()
    g.conns.mongo_db["eventos_logisticos"].delete_many({"envio_id": {"$in": [uuid.UUID(i) for i in ids]}})


def _entregado(g, *, contenedor=None, peso="8000", volumen="20") -> "EnvioDTO":  # noqa: F821
    s = g.ops.sesion_id
    e = g.nuevo_envio(peso=peso, volumen=volumen)
    g.ruta(e.id)
    g.asignar(e.id, contenedor or g.cont1, peso=peso, volumen=volumen)
    p = g.env.planificar(s, e.id)
    g.envios_creados.append(p.id)
    t1, t2 = g.env.obtener(s, p.id).tramos
    g.env.iniciar_tramo(s, t1.id, h(0.5))
    g.env.finalizar_tramo(s, t1.id, h(5))
    g.env.iniciar_tramo(s, t2.id, h(6))
    g.env.finalizar_tramo(s, t2.id, h(80))
    g.env.confirmar_entrega(s, p.id, h(81))
    return g.env.obtener(s, p.id).envio


def _por_codigo(resultado, codigo: str) -> list:
    return [d for d in resultado.discrepancias if d.entidad_id == codigo]


# ============================================================================ Neo4j
def test_detecta_envio_sin_proyeccion_en_neo4j(g):
    """Un envío recién creado, nunca sincronizado: Neo4j no tiene su nodo. Es exactamente lo que
    reconciliación debe señalar (no reparar: la reparación es correr la sincronización normal)."""
    envio = _entregado(g)
    reconciliacion = ReconciliacionService(g.conns)

    resultado = reconciliacion.ejecutar(limite_envios=500)

    hallazgos = _por_codigo(resultado, envio.codigo)
    assert any(d.categoria == "NEO4J" and "no tiene nodo" in d.detalle for d in hallazgos)
    assert resultado.motores_disponibles.get("NEO4J") is True


def test_no_reporta_envio_correctamente_proyectado(g):
    """Después de sincronizar de verdad, el mismo envío NO debe aparecer como discrepancia de Neo4j."""
    envio = _entregado(g)
    SincronizacionService(g.conns).procesar(NEO4J, agregado_id=envio.id)
    reconciliacion = ReconciliacionService(g.conns)

    resultado = reconciliacion.ejecutar(limite_envios=500)

    hallazgos = [d for d in _por_codigo(resultado, envio.codigo) if d.categoria == "NEO4J"]
    assert hallazgos == []


# ============================================================================ Mongo
def test_detecta_outbox_mongo_completada_sin_documento(g):
    """Simula el caso que la reconciliación debe atrapar: la outbox dice COMPLETADA pero no hay documento
    en `eventos_logisticos` (p. ej., una falla entre escribir en Mongo y confirmar la entrega). Se simula
    marcando una entrega COMPLETADA a mano, sin pasar por el flujo normal — es la única forma de provocar
    ese estado de manera determinística para la prueba; nunca se usa en el script de siembra ni en la app."""
    envio = _entregado(g)
    outbox = OutboxRepository()
    with unit_of_work(g.conns) as tx:
        evento_id = outbox.registrar(
            tx, agregado_tipo="ENVIO", agregado_id=envio.id, agregado_version=envio.version,
            evento_tipo="EVENTO_DE_PRUEBA", payload={"envio_id": str(envio.id)}, destinos=(MONGO,),
        )
        # Reclamar + completar (el flujo real) sin escribir nunca el documento en Mongo: así queda
        # exactamente el estado que se quiere probar, respetando el CHECK de la tabla.
        propietario = uuid.uuid4()
        [entrega] = outbox.reclamar(tx, MONGO, propietario_id=propietario, limite=1, reserva_s=60,
                                     agregado_id=envio.id)
        assert outbox.completar(tx, entrega.evento_id, MONGO, propietario, entrega.version_reclamo)
    reconciliacion = ReconciliacionService(g.conns)

    resultado = reconciliacion.ejecutar(limite_envios=500)

    hallazgos = [d for d in _por_codigo(resultado, envio.codigo) if d.categoria == "MONGO"]
    assert hallazgos, "debía detectar la entrega COMPLETADA sin documento en Mongo"
    assert resultado.motores_disponibles.get("MONGO") is True


def test_no_reporta_mongo_cuando_el_documento_existe(g):
    envio = _entregado(g)
    SincronizacionService(g.conns).procesar(MONGO, agregado_id=envio.id)
    reconciliacion = ReconciliacionService(g.conns)

    resultado = reconciliacion.ejecutar(limite_envios=500)

    hallazgos = [d for d in _por_codigo(resultado, envio.codigo) if d.categoria == "MONGO"]
    assert hallazgos == []


# ============================================================================ motor no disponible
def test_reporta_neo4j_no_disponible_sin_romperse(g, monkeypatch):
    """Con Neo4j inalcanzable, la corrida debe seguir (reporta `motores_disponibles["NEO4J"] = False`,
    nunca lanza) — igual que la fase 3 ya prueba para la sincronización."""
    from dataclasses import replace as _replace

    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    _entregado(g)
    reconciliacion = ReconciliacionService(g.conns)
    # Se rompe el driver de Neo4j DESPUÉS de construir el servicio (apunta a un puerto sin servidor: falla
    # de conexión real, no una excepción fabricada).
    from neo4j import GraphDatabase

    roto = GraphDatabase.driver("bolt://localhost:1", auth=("neo4j", "x"))
    monkeypatch.setattr(reconciliacion, "_proyeccion", ProyeccionRepository(_DriverFalso(roto)))

    resultado = reconciliacion.ejecutar(limite_envios=10)

    assert resultado.motores_disponibles.get("NEO4J") is False
    roto.close()


class _DriverFalso:
    """Conexión mínima para que `ProyeccionRepository` (que solo usa `.neo4j`) hable con un Neo4j roto."""

    def __init__(self, driver):
        self.neo4j = driver


# ============================================================================ comercial
def test_detecta_pago_sobreaplicado(g):
    """No puede pasar por la vía normal (los servicios lo impiden con bloqueos); se fuerza con una
    aplicación insertada directo para probar que la verificación independiente la atrapa igual."""
    from datetime import datetime, timezone

    from logistica.domain.comercial import CargoDatos, PagoDatos, SentidoPago

    envio = _entregado(g)
    g.com.crear_cargo(g.com_sesion, envio.id, CargoDatos("Flete", 1, 1000, "USD"))
    [factura1] = g.com.emitir_factura(g.com_sesion, envio.id)
    # Dos facturas distintas (uq_paplic_activa solo impide dos aplicaciones activas para el MISMO par
    # pago-factura): la segunda es la que, sumada a la primera, supera el disponible del pago.
    envio2 = _entregado(g, contenedor=g.cont2, peso="2000", volumen="5")
    g.com.crear_cargo(g.com_sesion, envio2.id, CargoDatos("Flete", 1, 1, "USD"))
    [factura2] = g.com.emitir_factura(g.com_sesion, envio2.id)
    pago = g.com.registrar_pago(
        g.com_sesion,
        # Los envíos de esta base de pruebas usan fechas futuras (T0 = +2 días, ver h()); un pago, en
        # cambio, no puede fecharse en el futuro (exigir_fecha): se usa el reloj real, no h(...).
        PagoDatos(g.cliente_sa.id, SentidoPago.COBRO, datetime.now(timezone.utc), 1000, "USD",
                  "TRANSFERENCIA"),
        uuid.uuid4(),
    )
    with unit_of_work(g.conns) as tx:
        tx.execute(
            text(
                "INSERT INTO pago_aplicacion (id, pago_id, factura_id, monto_aplicado)"
                " VALUES (:id, :pago, :factura, :monto)"
            ),
            {"id": uuid.uuid4(), "pago": pago.id, "factura": factura1.id, "monto": 1000},
        )
        # Una segunda aplicación "colada" a OTRA factura que, sumada a la primera, supera el disponible.
        tx.execute(
            text(
                "INSERT INTO pago_aplicacion (id, pago_id, factura_id, monto_aplicado)"
                " VALUES (:id, :pago, :factura, :monto)"
            ),
            {"id": uuid.uuid4(), "pago": pago.id, "factura": factura2.id, "monto": 1},
        )
    reconciliacion = ReconciliacionService(g.conns)

    resultado = reconciliacion.ejecutar(limite_envios=10)

    assert any(
        d.categoria == "PAGO_SOBREAPLICADO" and d.entidad_id == str(pago.id) for d in resultado.discrepancias
    )


# ============================================================================ sin discrepancias de más
def test_un_envio_consistente_no_genera_discrepancias_propias(g):
    """Sincronizado de punta a punta (Neo4j, Mongo y facturación): ningún hallazgo debe mencionar a ESTE
    envío en particular, aunque la base tenga otros datos con sus propios estados."""
    from logistica.domain.comercial import CargoDatos

    envio = _entregado(g)
    g.com.crear_cargo(g.com_sesion, envio.id, CargoDatos("Flete", 1, 500, "USD"))
    for destino in (NEO4J, MONGO, FACTURACION):
        SincronizacionService(g.conns, facturacion=g.com.facturar_entrega).procesar(
            destino, agregado_id=envio.id
        )
    reconciliacion = ReconciliacionService(g.conns)

    resultado = reconciliacion.ejecutar(limite_envios=500)

    assert _por_codigo(resultado, envio.codigo) == []
