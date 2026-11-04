"""Guiones reproducibles, aislamiento y reenvío del simulador por servicios reales."""

from __future__ import annotations

import os
from threading import Event

import pytest
from sqlalchemy import text

from logistica.core.errors import BusinessRuleError, PermissionDeniedError
from logistica.domain.simulacion_iot import ESCENARIOS, pasos_escenario
from logistica.services.demo_monitoreo_service import reproducir_demo
from logistica.services.monitoreo_service import MonitoreoService
from tests.test_fase4_monitoreo import m  # noqa: F401


@pytest.mark.parametrize("nombre", ESCENARIOS)
def test_guiones_deterministas_sin_esperas(nombre):
    a = pasos_escenario(nombre)
    assert a == pasos_escenario(nombre)
    assert [p.segundo for p in a] == sorted(p.segundo for p in a)
    assert a[0].segundo == 0


def test_cli_continuo_usa_episodios_estables_y_cierra_sesion_al_interrumpir(monkeypatch):
    import importlib.util
    import uuid
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "simulador_prueba", Path(__file__).resolve().parents[1] / "scripts" / "simulador_iot.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    fijo = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)

    class Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return fijo

    sesiones_cerradas, conexiones_cerradas, episodios = [], [], []
    sesion = uuid.uuid4()
    acceso = SimpleNamespace(
        login=lambda *args: SimpleNamespace(sesion_id=sesion),
        logout=lambda sid: sesiones_cerradas.append(sid))
    conexiones = SimpleNamespace(close=lambda: conexiones_cerradas.append(True))
    monkeypatch.setattr(modulo, "datetime", Reloj)
    monkeypatch.setattr(modulo, "load_config", lambda: SimpleNamespace(log_dir=None, log_level="INFO"))
    monkeypatch.setattr(modulo, "setup_logging", lambda *args: None)
    monkeypatch.setattr(modulo, "Connections", lambda *args: conexiones)
    monkeypatch.setattr(modulo, "crear_servicios", lambda *args: SimpleNamespace(acceso=acceso))
    monkeypatch.setattr(modulo, "Autorizador", lambda *args: object())
    monkeypatch.setattr(modulo.getpass, "getpass", lambda *args: "clave-ficticia-de-prueba")

    def episodio(*args, **kwargs):
        episodios.append(kwargs)
        if len(episodios) == 2:
            raise KeyboardInterrupt
        return 2

    monkeypatch.setattr(modulo, "reproducir_demo", episodio)
    monkeypatch.setattr("sys.argv", [
        "simulador_iot.py", "--envio-id", str(uuid.uuid4()), "--email", "demo@test.local",
        "--continuo", "--escenario", "normal"])
    assert modulo.main() == 130
    assert [e["inicio_escenario"] for e in episodios] == [fijo, fijo + timedelta(seconds=120)]
    assert all(not e["reloj_simulado"] for e in episodios)
    assert sesiones_cerradas == [sesion] and conexiones_cerradas == [True]


@pytest.mark.skipif(os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1", reason="requiere los motores locales")
def test_demo_por_servicios_aislada_y_reenviable(m):
    s = m.admin.sesion_id
    with m.conns.pg_session() as tx:
        antes = tx.execute(text(
            "SELECT id, estado, entrega_real_en, entrega_comprometida_en FROM envio ORDER BY id")).all()
        reservas = tx.execute(text("SELECT * FROM envio_contenedor ORDER BY id")).all()
    demo_id = None
    try:
        demo_id = m.mon.preparar_demo(s)
        detalle = m.env.obtener(s, demo_id)
        assert detalle.envio.estado == "EN_TRANSITO"
        assert detalle.envio.descripcion_carga.startswith("DEMO-MON-")
        assert all(a.contenedor_id != m.cont1.id for a in detalle.asignaciones)
        mon = MonitoreoService(m.conns, m.aut)
        lecturas = mon.lecturas(s, campo="envio_id", valor=demo_id).lecturas
        assert len(lecturas) == 12
        assert {l.origen for l in lecturas} == {"SIMULADOR"}
        alertas = mon.alertas(s, envio_id=demo_id, solo_activas=False)
        assert {(a.tipo, a.severidad, a.estado) for a in alertas} == {
            ("TEMPERATURA", "ALTA", "CERRADA"), ("SIN_COMUNICACION", "ALTA", "CERRADA")}
        [incidente] = mon.incidentes(s, envio_id=demo_id)
        assert incidente.tipo == "DESVIACION_TEMPERATURA" and incidente.estado == "ABIERTO"
        assert reproducir_demo(m.conns, m.aut, s, demo_id, "completo", reloj_simulado=True,
                               informar=lambda _: None) == 0
        assert len(mon.lecturas(s, campo="envio_id", valor=demo_id).lecturas) == 12
        assert len(mon.incidentes(s, envio_id=demo_id)) == 1
        with m.conns.pg_session() as tx:
            despues = tx.execute(text(
                "SELECT id, estado, entrega_real_en, entrega_comprometida_en FROM envio"
                " WHERE id <> :id ORDER BY id"), {"id": demo_id}).all()
            reservas_despues = tx.execute(text(
                "SELECT * FROM envio_contenedor WHERE envio_id <> :id ORDER BY id"), {"id": demo_id}).all()
        assert despues == antes and reservas_despues == reservas
    finally:
        if demo_id is not None:
            m.conns.mongo_db["lecturas_sensor"].delete_many({"envio_id": demo_id})
            m.conns.mongo_db["eventos_logisticos"].delete_many({"envio_id": demo_id})


@pytest.mark.skipif(os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1", reason="requiere los motores locales")
def test_simulador_no_reutiliza_viajes_manuales_y_solo_admin_prepara(m):
    with pytest.raises(BusinessRuleError):
        reproducir_demo(m.conns, m.aut, m.ops.sesion_id, m.envio.id, "completo", reloj_simulado=True)
    with pytest.raises(PermissionDeniedError):
        m.mon.preparar_demo(m.ops.sesion_id)
    cliente = m.login(m.emails["CLIENTE"])
    with pytest.raises(PermissionDeniedError):
        m.mon.procesar_ciclo(cliente.sesion_id, Event())


@pytest.mark.skipif(os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1", reason="requiere los motores locales")
def test_procesamiento_cancelado_no_reserva_lecturas(m):
    entrada, _, _ = m.leer(0, evaluar=False)
    detener = Event()
    detener.set()
    resultado = m.mon.procesar_ciclo(m.ops.sesion_id, detener)
    assert resultado.evaluadas == 0
    assert m.evaluacion(entrada.id)["estado"] == "PENDIENTE"
