"""Fase 8 — advisory locks y planificador de procesos periódicos contra PostgreSQL real.

Los locks se prueban con conexiones reales (son de sesión, no de transacción). Las tareas reales corren sobre
`tx_connections` (transacción revertida). Las carreras entre dos instancias con datos confirmados están en
`test_fase8_concurrencia.py` (base temporal).
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from logistica.core.errors import PermissionDeniedError
from logistica.domain.procesos import Tarea
from logistica.repositories.postgres import locks
from logistica.repositories.postgres.locks import advisory_lock
from logistica.services.autorizacion import Autorizador
from logistica.services.monitoreo_service import MonitoreoService, ResultadoEvaluacion
from logistica.services.prediccion_service import PrediccionService, ResultadoPrediccion
from logistica.services.proceso_service import Planificador
from logistica.services.sincronizacion_service import ResultadoLote
from tests.test_fase6_comercial import Comercial, _mes, _movimiento_historico

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y PostgreSQL levantado)",
)


@pytest.fixture()
def conns():
    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections

    c = Connections(load_config())
    yield c
    c.close()


def clave():
    return f"prueba:{uuid.uuid4()}"


# --------------------------------------------------------------------------- advisory lock
def test_un_lock_tomado_no_lo_toma_otra_conexion_hasta_liberarlo(conns):
    k = clave()
    with advisory_lock(conns.engine, k) as primero:
        assert primero
        with advisory_lock(conns.engine, k) as segundo:  # otra conexión dedicada del mismo pool
            assert not segundo
    with advisory_lock(conns.engine, k) as despues:
        assert despues


def test_el_lock_se_libera_aunque_la_tarea_falle(conns):
    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections

    k = clave()
    with pytest.raises(RuntimeError):
        with advisory_lock(conns.engine, k) as tomado:
            assert tomado
            raise RuntimeError("la tarea falló")
    otro = Connections(load_config())  # otro engine: el pool no puede ocultar un lock que quedó tomado
    try:
        with advisory_lock(otro.engine, k) as otra_vez:
            assert otra_vez
    finally:
        otro.close()


def test_si_no_se_puede_liberar_la_conexion_no_vuelve_al_pool(conns, monkeypatch):
    """Si el unlock falla, la conexión se invalida: al cerrarse de verdad, PostgreSQL suelta el lock de esa
    sesión, y no queda en el pool una conexión que lo conserve."""
    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections

    k = clave()
    monkeypatch.setattr(locks, "_SOLTAR", text("SELECT false"))
    with advisory_lock(conns.engine, k) as tomado:
        assert tomado
    monkeypatch.undo()
    # Se verifica desde OTRO engine: si la conexión hubiera vuelto al pool con el lock, este mismo pool podría
    # devolverla y, como el lock de sesión es reentrante, "tomarlo" de nuevo ocultaría el problema.
    otro = Connections(load_config())
    try:
        with advisory_lock(otro.engine, k) as desde_otro_proceso:
            assert desde_otro_proceso
    finally:
        otro.close()


# --------------------------------------------------------------------------- planificador (tareas simuladas)
class Falso:
    def __init__(self, respuesta="ok", error=None):
        self.llamadas, self.respuesta, self.error = 0, respuesta, error

    def __call__(self, *a, **k):
        self.llamadas += 1
        if self.error:
            raise self.error
        return self.respuesta


def planificador(conns, *, reloj=None, **tareas):
    falsos = {
        "facturacion": Falso(ResultadoLote()),
        "sensores": Falso(0),
        "cierre": Falso(0),
        "prediccion": Falso(ResultadoPrediccion(0, 0)),
        "lecturas": Falso(ResultadoEvaluacion()),
        **tareas,
    }
    p = Planificador(
        conns,
        Autorizador(conns),
        reloj=reloj,
        sincronizacion=SimpleNamespace(procesar=falsos["facturacion"]),
        monitoreo=SimpleNamespace(
            evaluar_pendientes=falsos["lecturas"], evaluar_comunicacion=falsos["sensores"]
        ),
        comercial=SimpleNamespace(cerrar_pendientes=falsos["cierre"]),
        predicciones=SimpleNamespace(calcular=falsos["prediccion"]),
    )
    return p, falsos


def estados(resultados):
    return {r.tarea: r.estado for r in resultados}


def test_una_vuelta_ejecuta_las_cuatro_tareas_y_las_diarias_no_se_repiten(conns):
    p, f = planificador(conns)
    assert set(estados(p.ejecutar_pendientes()).values()) == {"EJECUTADA"}
    segunda = estados(p.ejecutar_pendientes())
    assert segunda[Tarea.PREDICCION_DIARIA] == segunda[Tarea.CIERRE_MENSUAL] == "YA_HECHA"
    assert segunda[Tarea.FACTURACION] == segunda[Tarea.SENSORES_INACTIVOS] == "EJECUTADA"  # cada minuto
    assert (f["prediccion"].llamadas, f["cierre"].llamadas, f["facturacion"].llamadas) == (1, 1, 2)


def test_al_cambiar_el_dia_la_prediccion_vuelve_a_tocar(conns):
    reloj = {"t": datetime(2026, 11, 4, 23, 59, tzinfo=timezone.utc)}
    p, f = planificador(conns, reloj=lambda: reloj["t"])
    p.ejecutar_pendientes()
    reloj["t"] = datetime(2026, 11, 5, 0, 1, tzinfo=timezone.utc)
    assert estados(p.ejecutar_pendientes())[Tarea.PREDICCION_DIARIA] == "EJECUTADA"
    assert f["prediccion"].llamadas == 2 and f["cierre"].llamadas == 1  # el mes no cambió


def test_si_otra_instancia_tiene_el_lock_la_tarea_se_omite(conns):
    reloj = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    p, f = planificador(conns, reloj=lambda: reloj)
    with advisory_lock(conns.engine, "proceso:CIERRE_MENSUAL:2026-10") as tomado:  # "otra instancia"
        assert tomado
        r = estados(p.ejecutar_pendientes())
    assert r[Tarea.CIERRE_MENSUAL] == "OMITIDA" and f["cierre"].llamadas == 0
    assert estados(p.ejecutar_pendientes())[Tarea.CIERRE_MENSUAL] == "EJECUTADA"  # la próxima vuelta lo hace


def test_una_tarea_que_falla_no_frena_a_las_demas_y_se_reintenta(conns):
    p, f = planificador(conns, cierre=Falso(error=RuntimeError("PostgreSQL lento")))
    assert estados(p.ejecutar_pendientes())[Tarea.CIERRE_MENSUAL] == "ERROR"
    assert f["prediccion"].llamadas == 1 and f["facturacion"].llamadas == 1
    assert estados(p.ejecutar_pendientes())[Tarea.CIERRE_MENSUAL] == "ERROR"  # no quedó marcada como hecha
    assert f["cierre"].llamadas == 2


def test_solo_una_instancia_interna_procesa_desde_la_ventana(tx_connections):
    c = Comercial(tx_connections)
    p, _ = planificador(tx_connections)
    cliente = c.login(c.emails["CLIENTE"]).sesion_id
    with pytest.raises(PermissionDeniedError):
        p.ejecutar_con_sesion(cliente)
    assert p.ejecutar_con_sesion(c.ops.sesion_id)


# --------------------------------------------------------------------------- tareas reales y recuperación
@pytest.fixture()
def c(tx_connections):
    return Comercial(tx_connections)


def real(c):
    return Planificador(
        c.conns,
        c.aut,
        sincronizacion=c.sync,
        monitoreo=MonitoreoService(c.conns, c.aut),
        comercial=c.com,
        predicciones=PrediccionService(c.conns, c.aut),
    )


def test_al_reiniciar_se_factura_lo_pendiente(c):
    p = c.entregado(facturar=False)  # la instancia se "cayó" antes de facturar
    assert c.facturas(p.id) == []
    r = estados(real(c).ejecutar_pendientes())  # una instancia nueva arranca
    assert r[Tarea.FACTURACION] == "EJECUTADA" and len(c.facturas(p.id)) == 1


def test_el_cierre_automatico_recupera_los_meses_que_faltan_y_es_idempotente(c):
    t = c.cliente_sa.id
    hace2, hace1 = _mes(-2), _mes(-1)
    cuenta = _movimiento_historico(c, t, datetime(hace2.year, hace2.month, 3, tzinfo=timezone.utc), "100")
    _movimiento_historico(c, t, datetime(hace1.year, hace1.month, 3, tzinfo=timezone.utc), "40")
    assert c.com.cerrar_pendientes() >= 2
    cierres = {k.periodo: k for k in c.com.cierres(c.comercial, cuenta)}
    assert (cierres[hace2].saldo_final, cierres[hace1].saldo_final) == (
        Decimal("-100.00"),
        Decimal("-140.00"),
    )
    assert c.com.cerrar_pendientes() == 0  # repetirlo no crea nada
    with c.conns.pg_session() as s:
        n = s.execute(
            text("SELECT count(*) FROM auditoria WHERE accion = 'cierre_automatico' AND entidad_id = :c"),
            {"c": cuenta},
        ).scalar_one()
    assert n == 2
    assert date.today().replace(day=1) not in cierres  # el mes en curso nunca se cierra


def test_la_prediccion_diaria_del_planificador_es_idempotente(c):
    p = c.planificado()
    real(c).ejecutar_pendientes()
    real(c).ejecutar_pendientes()  # otra instancia el mismo día: no duplica
    with c.conns.pg_session() as s:
        n = s.execute(
            text("SELECT count(*) FROM prediccion_riesgo WHERE envio_id = :e"), {"e": p.id}
        ).scalar_one()
    assert n == 1


# --------------------------------------------------------------------------- hallazgos de la revisión
def test_una_cuenta_que_falla_no_impide_cerrar_las_demas(c, monkeypatch):
    from logistica.core.errors import BusinessRuleError
    from logistica.repositories.postgres.comercial import CuentaRepository

    hace1 = _mes(-1)
    fecha = datetime(hace1.year, hace1.month, 3, tzinfo=timezone.utc)
    rota = _movimiento_historico(c, c.cliente_sa.id, fecha, "10")
    sana = _movimiento_historico(c, c.operador_sa.id, fecha, "20")
    original = CuentaRepository.resumen_periodo

    def falla_una(self, tx, cuenta_id, desde, hasta):
        if cuenta_id == rota:
            raise RuntimeError("fila corrupta")
        return original(self, tx, cuenta_id, desde, hasta)

    monkeypatch.setattr(CuentaRepository, "resumen_periodo", falla_una)
    with pytest.raises(BusinessRuleError, match="1 cuenta"):  # se informa, para que el planificador reintente
        c.com.cerrar_pendientes()
    assert [k.periodo for k in c.com.cierres(c.comercial, sana)] == [hace1]  # la otra se cerró igual
    assert c.com.cierres(c.comercial, rota) == []


def test_un_lock_tomado_cuando_falla_la_lectura_del_resultado_no_vuelve_al_pool(conns, monkeypatch):
    """El servidor ya tomó el lock pero el cliente recibe un error ANTES de saberlo (al leer el resultado): la
    conexión se invalida igual, porque esta instancia no puede saber si el lock quedó tomado."""
    from sqlalchemy.engine import CursorResult

    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections

    k = clave()
    original = CursorResult.scalar_one
    fallas = []

    def lectura_que_falla(self):
        if not fallas:
            fallas.append(1)
            raise ConnectionError("se cortó la red justo después de tomar el lock")
        return original(self)

    monkeypatch.setattr(CursorResult, "scalar_one", lectura_que_falla)
    with pytest.raises(ConnectionError):
        with advisory_lock(conns.engine, k):
            pass
    monkeypatch.undo()
    otro = Connections(load_config())
    try:
        with advisory_lock(otro.engine, k) as libre:
            assert libre
    finally:
        otro.close()


def test_sensores_inactivos_evalua_primero_las_lecturas_recibidas(conns):
    orden = []
    p, _ = planificador(
        conns,
        lecturas=lambda: orden.append("lecturas") or ResultadoEvaluacion(),
        sensores=lambda: orden.append("comunicacion") or 0,
    )
    p.ejecutar_pendientes()
    assert orden == ["lecturas", "comunicacion"]
