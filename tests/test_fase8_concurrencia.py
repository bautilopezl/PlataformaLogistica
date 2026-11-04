"""Fase 8 — aceptación (AGENTS.md §17): dos instancias simultáneas ⇒ un solo efecto; reinicio ⇒ recupera lo
pendiente.

Dos planificadores con conexiones PROPIAS corren a la vez sobre una base PostgreSQL temporal (se crea y se
borra: los cierres, movimientos y auditoría son inmutables y no deben quedar en la base de desarrollo). La
tarea de cierre se demora a propósito mientras tiene el lock, para que las dos corridas se superpongan de
verdad.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from logistica.domain.procesos import Tarea
from logistica.services.autorizacion import Autorizador
from logistica.services.comercial_service import ComercialService
from logistica.services.monitoreo_service import MonitoreoService
from logistica.services.prediccion_service import PrediccionService
from logistica.services.proceso_service import Planificador
from logistica.services.sincronizacion_service import SincronizacionService
from tests.test_fase6_concurrencia import Dos, a_la_vez, bases, contar  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="requiere LOGISTICA_TEST_CONNECTIONS=1 y una identidad de administración que pueda crear bases",
)


def planificador(conns, *, demora=0.0):
    aut = Autorizador(conns)
    comercial = ComercialService(conns, aut)
    if demora:
        original = comercial.cerrar_pendientes

        def lento():
            resultado = original()
            time.sleep(demora)  # sigue con el lock tomado: la otra instancia llega mientras tanto
            return resultado

        comercial.cerrar_pendientes = lento
    return Planificador(
        conns,
        aut,
        sincronizacion=SincronizacionService(conns, facturacion=comercial.facturar_entrega),
        monitoreo=MonitoreoService(conns, aut),
        comercial=comercial,
        predicciones=PrediccionService(conns, aut),
    )


@pytest.fixture()
def d(bases):  # noqa: F811
    return Dos(*bases)


def _movimiento_del_mes_pasado(conns, tercero_id):
    hoy = date.today().replace(day=1)
    anterior = date(hoy.year - 1, 12, 1) if hoy.month == 1 else date(hoy.year, hoy.month - 1, 1)
    fecha = datetime(anterior.year, anterior.month, 10, tzinfo=timezone.utc)
    with conns.pg_session() as s:
        usuario = s.execute(text("SELECT id FROM usuario LIMIT 1")).scalar_one()
        cuenta, pago = uuid.uuid4(), uuid.uuid4()
        s.execute(
            text(
                "INSERT INTO cuenta_corriente (id, tercero_id, naturaleza, moneda_codigo)"
                " VALUES (:id, :t, 'COBRAR', 'EUR')"
            ),
            {"id": cuenta, "t": tercero_id},
        )
        s.execute(
            text(
                "INSERT INTO pago (id, tercero_id, sentido, fecha, monto, moneda_codigo, medio,"
                " registrado_por_id, clave_idempotencia) VALUES (:id, :t, 'COBRO', :f, 10, 'EUR', 'OTRO', "
                ":u, :k)"
            ),
            {"id": pago, "t": tercero_id, "f": fecha, "u": usuario, "k": uuid.uuid4()},
        )
        s.execute(
            text(
                "INSERT INTO movimiento_cuenta (id, cuenta_id, fecha, sentido, importe, pago_id, "
                "clave_origen)"
                " VALUES (:id, :c, :f, 'DISMINUYE', 10, :p, :k)"
            ),
            {"id": uuid.uuid4(), "c": cuenta, "f": fecha, "p": pago, "k": f"pago:{pago}"},
        )
    return cuenta


def test_dos_instancias_a_la_vez_cierran_el_mes_una_sola_vez(d, bases):  # noqa: F811
    c1, c2 = bases
    cuenta = _movimiento_del_mes_pasado(c1, d.cliente_sa.id)
    p1, p2 = planificador(c1, demora=1.5), planificador(c2, demora=1.5)
    r1, r2 = a_la_vez(p1.ejecutar_pendientes, p2.ejecutar_pendientes)
    cierre = sorted(next(x.estado for x in r if x.tarea == Tarea.CIERRE_MENSUAL) for r in (r1, r2))
    assert cierre == ["EJECUTADA", "OMITIDA"]  # el advisory lock dejó pasar a una sola
    assert contar(c1, "SELECT count(*) FROM cierre_cuenta WHERE cuenta_id = :c", c=cuenta) == 1


def test_dos_instancias_a_la_vez_facturan_y_predicen_una_sola_vez(d, bases):  # noqa: F811
    c1, c2 = bases
    p = d.entregado("700")  # entrega confirmada, factura pendiente
    e = d.nuevo_envio(peso="4000", volumen="8")  # objetivo de la predicción del día (en el otro contenedor)
    d.ruta(e.id)
    d.asignar(e.id, d.cont2, peso="4000", volumen="8")
    plan = d.env.planificar(d.ops.sesion_id, e.id)
    r1, r2 = a_la_vez(planificador(c1).ejecutar_pendientes, planificador(c2).ejecutar_pendientes)
    assert all(not any(x.estado == "ERROR" for x in r) for r in (r1, r2)), (r1, r2)
    assert contar(c1, "SELECT count(*) FROM factura_detalle WHERE envio_id = :e", e=p.id) == 1
    assert contar(c1, "SELECT count(*) FROM prediccion_riesgo WHERE envio_id = :e", e=plan.id) == 1


def test_una_instancia_que_arranca_despues_recupera_lo_pendiente(d, bases):  # noqa: F811
    c1, _ = bases
    p = d.entregado("300")  # nadie procesó todavía (la "instancia" estaba apagada)
    assert contar(c1, "SELECT count(*) FROM factura_detalle WHERE envio_id = :e", e=p.id) == 0
    resultados = planificador(c1).ejecutar_pendientes()  # primera vuelta al iniciar
    assert {x.tarea: x.estado for x in resultados}[Tarea.FACTURACION] == "EJECUTADA"
    assert contar(c1, "SELECT count(*) FROM factura_detalle WHERE envio_id = :e", e=p.id) == 1


def test_el_lock_no_queda_tomado_si_la_instancia_falla_a_mitad(bases):  # noqa: F811
    c1, c2 = bases
    p1 = planificador(c1)
    p1._tareas[Tarea.PREDICCION_DIARIA] = lambda: (_ for _ in ()).throw(RuntimeError("caída a mitad"))
    assert {x.tarea: x.estado for x in p1.ejecutar_pendientes()}[Tarea.PREDICCION_DIARIA] == "ERROR"
    hecho = threading.Event()
    p2 = planificador(c2)
    p2._tareas[Tarea.PREDICCION_DIARIA] = lambda: hecho.set() or "ok"
    assert {x.tarea: x.estado for x in p2.ejecutar_pendientes()}[Tarea.PREDICCION_DIARIA] == "EJECUTADA"
    assert hecho.is_set()  # la otra instancia pudo tomar el lock: la que falló lo liberó
