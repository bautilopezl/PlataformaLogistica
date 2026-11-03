"""Fase 2 — casos que encontró la revisión con contexto fresco (cada uno reproduce un defecto corregido).

Requiere LOGISTICA_TEST_CONNECTIONS=1. Usa el entorno de tests/test_fase2_logistica.py.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from logistica.core.errors import BusinessRuleError, PermissionDeniedError, SessionExpiredError
from logistica.domain.logistica import EnvioDatos, SensorDatos, TramoPlan
from tests.test_fase2_logistica import Logistica, h

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env, PostgreSQL y MongoDB levantados)",
)


@pytest.fixture()
def g(tx_connections):
    return Logistica(tx_connections)


def test_uso_real_de_un_contenedor_retrasado_se_informa_claro(g):
    """A reserva K hasta h50 pero se atrasa; B reserva K desde h50 e intenta salir con K todavía en uso."""
    s = g.ops.sesion_id
    a = g.nuevo_envio()
    g.ruta(a.id)
    g.asignar(a.id, g.cont1, desde=-1, hasta=50)
    g.env.planificar(s, a.id)
    g.env.iniciar_tramo(s, g.env.obtener(s, a.id).tramos[0].id, h(0))
    b = g.env.crear(s, EnvioDatos(g.cliente_sa.id, g.A.id, g.B.id, "x", "100", "1"))
    t = TramoPlan(g.A.id, g.B.id, "TERRESTRE", g.operador_sa.id, h(51), h(55), g.camion.id)
    tb = g.env.definir_tramos(s, b.id, [t]).tramos[0]
    g.asignar(b.id, g.cont1, peso="100", volumen="1", desde=50, hasta=60)
    g.env.planificar(s, b.id)
    with pytest.raises(BusinessRuleError, match="en uso"):  # y no un IntegrityError genérico
        g.env.iniciar_tramo(s, tb.id, h(55))


def test_no_se_planifica_con_entrega_estimada_anterior_a_la_creacion(g):
    e = g.nuevo_envio()
    pasado = datetime.now(timezone.utc) - timedelta(days=3)
    t1 = TramoPlan(g.A.id, g.B.id, "TERRESTRE", g.operador_sa.id, pasado, pasado + timedelta(hours=2))
    t2 = TramoPlan(g.B.id, g.C.id, "MARITIMO", g.operador_sa.id, pasado + timedelta(hours=3),
                   pasado + timedelta(hours=4))
    g.env.definir_tramos(g.ops.sesion_id, e.id, [t1, t2])
    g.asignar(e.id, g.cont1)
    with pytest.raises(BusinessRuleError, match="anterior a la creación"):
        g.env.planificar(g.ops.sesion_id, e.id)


def test_entrega_debe_ser_posterior_al_inicio_del_uso(g):
    s = g.ops.sesion_id
    p = g.planificado()
    t1, t2 = g.env.obtener(s, p.id).tramos
    g.env.iniciar_tramo(s, t1.id, h(1))
    g.env.finalizar_tramo(s, t1.id, h(1))  # tramo instantáneo: permitido
    g.env.iniciar_tramo(s, t2.id, h(1))
    g.env.finalizar_tramo(s, t2.id, h(1))
    with pytest.raises(BusinessRuleError, match="posterior al inicio"):
        g.env.confirmar_entrega(s, p.id, h(1))  # el uso del contenedor duraría cero


def test_tramo_no_sale_antes_de_llegar_el_anterior(g):
    s = g.ops.sesion_id
    p = g.planificado()
    t1, t2 = g.env.obtener(s, p.id).tramos
    g.env.iniciar_tramo(s, t1.id, h(0))
    g.env.finalizar_tramo(s, t1.id, h(5))
    with pytest.raises(BusinessRuleError, match="antes de la llegada"):
        g.env.iniciar_tramo(s, t2.id, h(4))


def test_cancelar_dos_veces_la_misma_asignacion_falla_la_segunda(g):
    e = g.nuevo_envio()
    a = g.asignar(e.id, g.cont1)
    g.env.cancelar_asignacion(g.ops.sesion_id, a.id)
    antes = len(g.outbox(e.id))
    with pytest.raises(BusinessRuleError, match="no reservada"):
        g.env.cancelar_asignacion(g.ops.sesion_id, a.id)
    assert len(g.outbox(e.id)) == antes  # sin evento falso para Neo4j


def test_planificado_sin_contenedores_vuelve_a_borrador(g):
    p = g.planificado()
    a = g.env.obtener(g.ops.sesion_id, p.id).asignaciones[0]
    g.env.cancelar_asignacion(g.ops.sesion_id, a.id)
    assert g.env.obtener(g.ops.sesion_id, p.id).envio.estado == "BORRADOR"


def test_contenedor_con_reservas_activas_no_se_da_de_baja(g):
    e = g.nuevo_envio()
    g.asignar(e.id, g.cont1)
    with pytest.raises(BusinessRuleError, match="asignaciones activas"):
        g.cat.set_estado_contenedor(g.ops.sesion_id, g.cont1.id, "BAJA")


def test_vehiculo_asignado_a_tramos_pendientes_no_se_da_de_baja(g):
    e = g.nuevo_envio()
    g.ruta(e.id)
    with pytest.raises(BusinessRuleError, match="tramos sin terminar"):
        g.cat.set_estado_vehiculo(g.ops.sesion_id, g.camion.id, "BAJA")


def test_sensor_instalado_no_se_da_de_baja(g):
    s = g.ops.sesion_id
    sensor = g.cat.crear_sensor(s, SensorDatos(f"SEN-I{g.sufijo}", "GPS", "Acme", capacidades=frozenset({"GPS"})))
    g.cat.instalar_sensor(s, sensor.id, g.cont1.id, datetime.now(timezone.utc) - timedelta(hours=1))
    with pytest.raises(BusinessRuleError, match="instalado"):
        g.cat.set_estado_sensor(s, sensor.id, "BAJA")


def test_retirar_dos_veces_falla(g):
    s = g.ops.sesion_id
    sensor = g.cat.crear_sensor(s, SensorDatos(f"SEN-R{g.sufijo}", "GPS", "Acme", capacidades=frozenset({"GPS"})))
    inst = g.cat.instalar_sensor(s, sensor.id, g.cont1.id, h(0))
    g.cat.retirar_sensor(s, inst.id, h(5))
    with pytest.raises(BusinessRuleError, match="ya retirado"):
        g.cat.retirar_sensor(s, inst.id, h(9))


def test_evento_de_envio_ajeno_no_revela_si_existe(g):
    operador1 = g.login(g.emails["OPERADOR_LOGISTICO"])
    ajeno = g.nuevo_envio()  # sin tramos del operador 1
    with pytest.raises(PermissionDeniedError):
        g.env.registrar_evento(operador1.sesion_id, ajeno.id, "RETRASO", h(1), {"motivo": "x"})
    with pytest.raises(PermissionDeniedError):  # inexistente: mismo error, no NotFound
        g.env.registrar_evento(operador1.sesion_id, uuid.uuid4(), "RETRASO", h(1), {"motivo": "x"})


def test_eventos_del_envio_respetan_el_alcance(g):
    e = g.nuevo_envio()
    cliente = g.login(g.emails["CLIENTE"])
    assert [ev.evento_tipo for ev in g.env.eventos(cliente.sesion_id, e.id)] == ["ENVIO_CREADO"]
    operador1 = g.login(g.emails["OPERADOR_LOGISTICO"])
    with pytest.raises(PermissionDeniedError):
        g.env.eventos(operador1.sesion_id, e.id)


def test_sesion_invalida_se_informa_antes_que_datos_invalidos(g):
    # Orden de AGENTS.md §6: sesión → permiso → alcance → reglas.
    with pytest.raises(SessionExpiredError):
        g.env.registrar_evento(uuid.uuid4(), uuid.uuid4(), "RETRASO", h(1), {})
    with pytest.raises(SessionExpiredError):
        g.env.iniciar_tramo(uuid.uuid4(), uuid.uuid4(), datetime(2026, 1, 1))


def test_cancelacion_lleva_los_contenedores_que_tenia(g):
    p = g.planificado()
    g.env.cancelar(g.ops.sesion_id, p.id, "desistió")
    cancelado = next(ev for ev in g.outbox(p.id) if ev.evento_tipo == "ENVIO_CANCELADO")
    assert cancelado.payload["contenedor_ids"] == [str(g.cont1.id)]


def test_tramos_definidos_llevan_sus_ids(g):
    e = g.nuevo_envio()
    det = g.ruta(e.id)
    definido = next(ev for ev in g.outbox(e.id) if ev.evento_tipo == "TRAMOS_DEFINIDOS")
    assert [t["tramo_id"] for t in definido.payload["tramos"]] == [str(t.id) for t in det.tramos]


def test_los_eventos_para_mongo_cumplen_el_validador_real(g):
    """Contrato con el consumidor de la fase 3: el payload, convertido, entra en eventos_logisticos."""
    from logistica.repositories.mongo.eventos import documento_evento

    s = g.ops.sesion_id
    p = g.planificado()
    t1 = g.env.obtener(s, p.id).tramos[0]
    g.env.iniciar_tramo(s, t1.id, h(0.5))
    g.env.registrar_evento(s, p.id, "RETRASO", h(1), {"motivo": "Lluvia", "demora_estimada_min": 30},
                           tramo_id=t1.id)
    g.env.registrar_evento(s, p.id, "ADUANA", h(2), {"etapa": "INGRESO", "referencia": 123})
    a_mongo = [ev for ev in g.outbox(p.id) if "tipo" in ev.payload]
    assert len(a_mongo) == 3
    coleccion = g.conns.mongo_db["eventos_logisticos"]
    ids = []
    try:
        for ev in a_mongo:
            doc = documento_evento(ev.payload, fecha_registro=datetime.now(timezone.utc))
            coleccion.insert_one(doc)  # el $jsonSchema real rechazaría un documento inválido
            ids.append(doc["_id"])
        guardado = coleccion.find_one({"_id": ids[-1]})
        assert guardado["detalles"] == {"etapa": "INGRESO", "referencia": "123"}
        assert guardado["fecha_ocurrencia"].replace(tzinfo=timezone.utc) == h(2)
    finally:
        coleccion.delete_many({"_id": {"$in": ids}})
