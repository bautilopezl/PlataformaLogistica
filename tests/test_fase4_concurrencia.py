"""Reclamos Mongo reales, recuperación entre motores y descarte tras revocación/cambio de alcance."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import text

from logistica.core.errors import DatabaseUnavailableError, PermissionDeniedError, SessionExpiredError, ValidationError
from logistica.domain.logistica import EnvioDatos
from logistica.repositories.mongo.lecturas import LecturasRepository
from logistica.repositories.postgres.monitoreo import EstadoSensorRepository
from logistica.services.monitoreo_service import MonitoreoService
from tests.test_fase4_monitoreo import SALIDA, m  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1", reason="requiere PostgreSQL y MongoDB locales")


def test_dos_evaluadores_reclaman_una_lectura_una_sola_vez(m):
    entrada, _, _ = m.leer(0, evaluar=False)
    barrera = Barrier(2)
    otro = MonitoreoService(m.conns, m.aut, reloj=m.reloj)

    def reclamar(mon):
        barrera.wait(timeout=5)
        return mon._lecturas.reservar(propietario_id=mon.propietario_id, ahora=m.reloj(),
                                     reserva_s=60, sensor_ids=m.sensores)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(reclamar, m.mon)
        b = pool.submit(reclamar, otro)
        docs = [a.result(timeout=10), b.result(timeout=10)]
    [doc] = [d for d in docs if d is not None]
    assert doc["_id"] == entrada.id
    assert m.mon._evaluar(doc) == "evaluadas"
    assert m.mon._evaluar(doc) == "repetidas"
    with m.conns.pg_session() as tx:
        assert tx.execute(text("SELECT count(*) FROM lectura_procesada WHERE lectura_id = :id"),
                          {"id": entrada.id}).scalar_one() == 1


def test_reclamo_vencido_no_puede_marcar_el_reclamo_nuevo(m):
    entrada, _, _ = m.leer(0, evaluar=False)
    repo = LecturasRepository(m.conns)
    primero = repo.reservar(propietario_id=uuid.uuid4(), ahora=m.reloj(), reserva_s=60, sensor_ids=m.sensores)
    m.reloj.t += timedelta(seconds=61)
    segundo = repo.reservar(propietario_id=uuid.uuid4(), ahora=m.reloj(), reserva_s=60, sensor_ids=m.sensores)
    assert primero["_id"] == segundo["_id"] == entrada.id
    assert segundo["evaluacion"]["version_reclamo"] == primero["evaluacion"]["version_reclamo"] + 1
    assert not repo.marcar_procesada(primero, version_reglas="1.0", ahora=m.reloj(), resultado="EVALUAR")
    assert m.evaluacion(entrada.id)["estado"] == "EN_PROCESO"
    assert repo.marcar_procesada(segundo, version_reglas="1.0", ahora=m.reloj(), resultado="EVALUAR")


def test_fallo_al_marcar_el_escalamiento_no_duplica_incidente_ni_outbox(m, monkeypatch):
    for segundo in (0, 60, 120, 180, 240):
        m.leer(segundo, temperatura_c=12)
    original = LecturasRepository.marcar_procesada
    caidas = []

    def fallar_una_vez(self, doc, **kwargs):
        if not caidas:
            caidas.append(doc["_id"])
            raise ConnectionError("interrupción después de confirmar el incidente")
        return original(self, doc, **kwargs)

    monkeypatch.setattr(LecturasRepository, "marcar_procesada", fallar_una_vez)
    _, _, resultado = m.leer(300, temperatura_c=12)
    assert resultado.reintentos == 1
    [incidente] = m.incidentes()
    m.reloj.t += timedelta(seconds=31)
    assert m.mon.evaluar_pendientes(sensor_ids=m.sensores).repetidas == 1
    assert [i.id for i in m.incidentes()] == [incidente.id]
    with m.conns.pg_session() as tx:
        assert tx.execute(text("SELECT count(*) FROM outbox_evento WHERE agregado_id = :id"),
                          {"id": incidente.id}).scalar_one() == 1


def test_revocar_sesion_mientras_mongo_consulta_descarta_resultados(m, monkeypatch):
    m.leer(0)
    original = LecturasRepository.historial

    def revocar(self, **kwargs):
        docs = original(self, **kwargs)
        with m.conns.pg_session() as tx:
            tx.execute(text("UPDATE sesion SET estado = 'REVOCADA', fin_en = clock_timestamp() WHERE id = :id"),
                       {"id": m.ops.sesion_id})
        return docs

    monkeypatch.setattr(LecturasRepository, "historial", revocar)
    with pytest.raises(SessionExpiredError):
        m.mon.lecturas(m.ops.sesion_id, campo="envio_id", valor=m.envio.id)


def test_cambio_de_alcance_mientras_mongo_consulta_descarta_resultados(m, monkeypatch):
    m.leer(0)
    operador = m.login(m.emails["OPERADOR_LOGISTICO"])
    original = LecturasRepository.historial

    def reasignar(self, **kwargs):
        docs = original(self, **kwargs)
        with m.conns.pg_session() as tx:
            tx.execute(text("UPDATE tramo SET operador_id = :o WHERE id = :t"),
                       {"o": m.operador2.id, "t": m.t1.id})
        return docs

    monkeypatch.setattr(LecturasRepository, "historial", reasignar)
    with pytest.raises(PermissionDeniedError):
        m.mon.lecturas(operador.sesion_id, campo="envio_id", valor=m.envio.id)


def test_operador_no_ve_estado_actual_de_otro_tramo_ni_alertas_ajenas(m):
    m.leer(0, vibracion_g=5)
    operador = m.login(m.emails["OPERADOR_LOGISTICO"])
    assert m.mon.estado_sensores(operador.sesion_id, envio_id=m.envio.id)[0].alertas_activas == 1
    m.env.finalizar_tramo(m.ops.sesion_id, m.t1.id, SALIDA + timedelta(seconds=100))
    with m.conns.pg_session() as tx:
        tx.execute(text("UPDATE tramo SET operador_id = :o, vehiculo_id = :v WHERE id = :t"),
                   {"o": m.operador2.id, "v": m.camion.id, "t": m.t2.id})
    assert m.mon.estado_sensores(operador.sesion_id, envio_id=m.envio.id) == []
    assert len(m.mon.ultimas_lecturas(operador.sesion_id, m.envio.id)) == 1


@pytest.mark.parametrize("campo,desde,hasta", [
    ("otro", None, None), ("envio_id", SALIDA, SALIDA),
    ("sensor_id", SALIDA + timedelta(seconds=1), SALIDA)])
def test_filtros_invalidos_fallan_explicito(m, campo, desde, hasta):
    with pytest.raises(ValidationError):
        m.mon.lecturas(m.ops.sesion_id, campo=campo, valor=m.envio.id, desde=desde, hasta=hasta)


def test_ultimas_mediciones_son_las_mas_nuevas_y_respetan_alcance(m):
    for segundo in range(25):
        m.leer(segundo)
    ultimas = m.mon.ultimas_lecturas(m.ops.sesion_id, m.envio.id)
    assert len(ultimas) == 20
    assert [l.fecha_medicion for l in ultimas] == [
        SALIDA + timedelta(seconds=s) for s in range(24, 4, -1)]
    cliente = m.login(m.emails["CLIENTE"])
    assert len(m.mon.ultimas_lecturas(cliente.sesion_id, m.envio.id)) == 20
    otro_cliente = m.terceros.crear(m.admin.sesion_id, m._tercero("Otro cliente"), {"CLIENTE"})
    otro_envio = m.env.crear(m.ops.sesion_id, EnvioDatos(
        otro_cliente.id, m.A.id, m.C.id, "Otra empresa", "1", "1"))
    with pytest.raises(PermissionDeniedError):
        m.mon.ultimas_lecturas(cliente.sesion_id, otro_envio.id)
    with pytest.raises(PermissionDeniedError):
        m.mon.posiciones(cliente.sesion_id, otro_envio.id)
    assert m.mon.alertas(cliente.sesion_id, envio_id=otro_envio.id) == []
    assert m.mon.incidentes(cliente.sesion_id, envio_id=otro_envio.id) == []


def test_contacto_confirmado_mientras_se_esperaba_lock_evade_falsa_desconexion(m, monkeypatch):
    m.leer(0)
    m.reloj.t = SALIDA + timedelta(seconds=181)
    original = EstadoSensorRepository.bloquear
    ingreso = []

    def actualizar_antes_del_lock(self, tx, sensor_id):
        if not ingreso:
            ingreso.append(True)
            m.leer(181)  # la instantánea inicial ya se tomó; otro evaluador confirmó un contacto
        return original(self, tx, sensor_id)

    monkeypatch.setattr(EstadoSensorRepository, "bloquear", actualizar_antes_del_lock)
    assert m.mon.evaluar_comunicacion(sensor_ids=m.sensores) == 0
    assert m.alertas("SIN_COMUNICACION") == []


def test_mongo_caido_incluso_al_reencolar_se_recupera_por_reserva_vencida(m, monkeypatch):
    for segundo in (0, 60, 120, 180, 240):
        m.leer(segundo, temperatura_c=12)
    entrada, _, _ = m.leer(300, temperatura_c=12, evaluar=False)

    def caido(*args, **kwargs):
        raise DatabaseUnavailableError("MongoDB", "caído después del commit")

    with monkeypatch.context() as contexto:
        contexto.setattr(LecturasRepository, "marcar_procesada", caido)
        contexto.setattr(LecturasRepository, "reintentar", caido)
        with pytest.raises(DatabaseUnavailableError):
            m.mon.evaluar_pendientes(sensor_ids=m.sensores)
    assert m.evaluacion(entrada.id)["estado"] == "EN_PROCESO"
    [incidente] = m.incidentes()
    m.reloj.t += timedelta(seconds=61)
    assert m.mon.evaluar_pendientes(sensor_ids=m.sensores).repetidas == 1
    assert [i.id for i in m.incidentes()] == [incidente.id]
