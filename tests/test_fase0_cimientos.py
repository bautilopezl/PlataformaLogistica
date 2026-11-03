"""Pruebas de la fase 0 (cimientos): capas, errores, logs, salud de los motores y UI sin bloqueos.

La mayoría no necesita servidores. `test_conexiones_reales_*` usa el `.env` del proyecto y solo corre si se
define LOGISTICA_TEST_CONNECTIONS=1 (con los tres motores levantados con Docker Compose).
"""

from __future__ import annotations

import ast
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # la UI se prueba sin pantalla

from logistica.core import errors  # noqa: E402
from logistica.core.config import (  # noqa: E402
    AppConfig,
    MongoConfig,
    Neo4jConfig,
    PostgresConfig,
    load_config,
)
from logistica.core.logging_setup import RedactSecretsFilter  # noqa: E402
from logistica.repositories.base import translate_errors  # noqa: E402
from logistica.repositories.connections import Connections  # noqa: E402
from logistica.services.health_service import EngineStatus, HealthService  # noqa: E402


# --------------------------------------------------------------------------- estructura y capas
def test_estructura_de_paquetes_por_motor():
    import importlib

    for modulo in (
        "logistica.domain",
        "logistica.repositories.postgres",
        "logistica.repositories.mongo",
        "logistica.repositories.neo4j",
        "logistica.services",
        "logistica.ui",
    ):
        importlib.import_module(modulo)


def _imports_de(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nombres: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            nombres.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            nombres.add(node.module)
    return nombres


PROHIBIDOS_EN_UI = ("logistica.repositories", "sqlalchemy", "psycopg", "pymongo", "neo4j", "bson")


def test_la_ui_no_importa_repositorios_ni_drivers():
    archivos = sorted((SRC / "logistica" / "ui").rglob("*.py"))
    assert archivos
    for archivo in archivos:
        malos = [m for m in _imports_de(archivo) if m.startswith(PROHIBIDOS_EN_UI)]
        assert not malos, f"{archivo.name} importa {malos}: la UI solo habla con services"


def test_servicios_y_dominio_no_importan_drivers():
    for capa in ("services", "domain"):
        for archivo in sorted((SRC / "logistica" / capa).rglob("*.py")):
            malos = [
                m for m in _imports_de(archivo) if m.startswith(("sqlalchemy", "psycopg", "pymongo", "neo4j"))
            ]
            assert not malos, f"{capa}/{archivo.name} importa drivers {malos}"


# --------------------------------------------------------------------------- logs sin secretos
@pytest.mark.parametrize(
    "mensaje, secreto",
    [
        ("host=localhost port=5434 user=app password=S3cr3t0 dbname=x", "S3cr3t0"),
        ("mongodb://logistica_admin:Cl4veMongo@localhost:27017/?authSource=admin", "Cl4veMongo"),
        ("postgresql+psycopg://app:Cl4vePg@localhost:5434/logistica", "Cl4vePg"),
        ("PG_ADMIN_PASSWORD=OtraClave123", "OtraClave123"),
        ('password="clave con espacios"', "con espacios"),
        ("postgresql://u:p4ss@w0rd@host/db", "w0rd"),
        ("password: ClaveDosPuntos", "ClaveDosPuntos"),
        ("{'user': 'x', 'password': 'ClaveDict'}", "ClaveDict"),
        ("ALTER ROLE logistica_app LOGIN PASSWORD 'ClaveSql'", "ClaveSql"),
        ("[parameters: {'hash': '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'}]", "$argon2id$v=19$m=65536"),
    ],
)
def test_filtro_de_logs_oculta_credenciales(mensaje, secreto):
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "falló: %s", (mensaje,), None)
    assert RedactSecretsFilter().filter(record) is True
    texto = record.getMessage()
    assert secreto not in texto
    assert "***" in texto


def test_filtro_de_logs_oculta_credenciales_en_excepciones():
    try:
        raise RuntimeError("connection failed: password=Clave99 rejected")
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "error", (), sys.exc_info())
    RedactSecretsFilter().filter(record)
    formatted = logging.Formatter().format(record)
    assert "Clave99" not in formatted


def test_filtro_de_logs_no_lanza_con_argumentos_mal_formados():
    # Un log mal escrito no debe romper al código que lo emitió (logging normalmente lo traga).
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "faltan %s %s", ("uno",), None)
    assert RedactSecretsFilter().filter(record) is True
    assert "faltan" in record.getMessage()


def test_filtro_de_logs_oculta_credenciales_en_stack_info():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "ok", (), None, sinfo="password=EnPila1")
    RedactSecretsFilter().filter(record)
    assert "EnPila1" not in record.stack_info


# --------------------------------------------------------------------------- traducción de errores
def _errores_de_driver():
    import neo4j.exceptions as nx
    import pymongo.errors as mx
    import sqlalchemy.exc as sx

    return [
        (sx.OperationalError("SELECT 1", {}, Exception("server closed")), errors.DatabaseUnavailableError),
        (sx.IntegrityError("INSERT", {}, Exception("duplicate key")), errors.IntegrityError),
        (mx.ServerSelectionTimeoutError("timeout"), errors.DatabaseUnavailableError),
        (mx.DuplicateKeyError("E11000 duplicate key"), errors.IntegrityError),
        (mx.WriteError("Document failed validation", code=121), errors.ValidationError),
        (nx.ServiceUnavailable("down"), errors.DatabaseUnavailableError),
        (nx.ConstraintError("already exists"), errors.IntegrityError),
    ]


@pytest.mark.parametrize("original, esperado", _errores_de_driver())
def test_repositorios_traducen_errores_de_driver(original, esperado):
    with pytest.raises(esperado) as info:
        with translate_errors("PostgreSQL"):
            raise original
    assert info.value.__cause__ is original  # el detalle técnico se conserva para el log


def test_deadlock_o_conflicto_no_se_informa_como_base_caida():
    # Un deadlock o un conflicto de serialización no significan que PostgreSQL esté caído: se propagan
    # tal cual para que el servicio decida (reintentar o informar).
    import psycopg
    import sqlalchemy.exc as sx

    for orig in (
        psycopg.errors.DeadlockDetected(),
        psycopg.errors.SerializationFailure(),
        psycopg.errors.QueryCanceled(),
    ):
        original = sx.OperationalError("UPDATE", {}, orig)
        with pytest.raises(sx.OperationalError):
            with translate_errors("PostgreSQL"):
                raise original


def test_falla_de_conexion_postgres_si_es_base_no_disponible():
    import psycopg
    import sqlalchemy.exc as sx

    for orig in (psycopg.OperationalError("connection failed"), psycopg.errors.AdminShutdown()):
        with pytest.raises(errors.DatabaseUnavailableError):
            with translate_errors("PostgreSQL"):
                raise sx.OperationalError("SELECT 1", {}, orig)


@pytest.mark.parametrize("codigo, esperado", [(11000, errors.IntegrityError), (121, errors.ValidationError)])
def test_errores_de_escritura_masiva_mongo_se_traducen(codigo, esperado):
    import pymongo.errors as mx

    original = mx.BulkWriteError({"writeErrors": [{"index": 0, "code": codigo, "errmsg": "x"}]})
    with pytest.raises(esperado):
        with translate_errors("MongoDB"):
            raise original


def test_dato_fuera_de_formato_se_traduce_a_validacion():
    import psycopg
    import sqlalchemy.exc as sx

    with pytest.raises(errors.ValidationError):
        with translate_errors("PostgreSQL"):
            raise sx.DataError("INSERT", {}, psycopg.errors.StringDataRightTruncation())


def test_integrity_error_sin_orig_no_rompe_la_traduccion():
    import sqlalchemy.exc as sx

    with pytest.raises(errors.IntegrityError):
        with translate_errors("PostgreSQL"):
            raise sx.IntegrityError("INSERT", {}, None)


def test_errores_propios_pasan_sin_cambios():
    propio = errors.NotFoundError("envío inexistente")
    with pytest.raises(errors.NotFoundError) as info:
        with translate_errors("PostgreSQL"):
            raise propio
    assert info.value is propio


# --------------------------------------------------------------------------- salud y degradación
class _FakeConnections:
    """Doble de prueba: cada chequeo duerme `demora` segundos y opcionalmente falla."""

    def __init__(self, caidos=(), demora=0.0, inesperado=()):
        self.caidos, self.demora, self.inesperado = set(caidos), demora, set(inesperado)

    def _check(self, nombre):
        time.sleep(self.demora)
        if nombre in self.caidos:
            raise errors.DatabaseUnavailableError(nombre, "sin respuesta")
        if nombre in self.inesperado:
            raise RuntimeError("bug inesperado")

    def check_postgres(self):
        self._check("PostgreSQL")

    def check_mongo(self):
        self._check("MongoDB")

    def check_neo4j(self):
        self._check("Neo4j")


def test_un_motor_caido_no_tumba_el_chequeo():
    estados = HealthService(_FakeConnections(caidos={"MongoDB"})).check_all()
    assert [(s.engine, s.ok) for s in estados] == [("PostgreSQL", True), ("MongoDB", False), ("Neo4j", True)]
    assert "MongoDB" in estados[1].detail


def test_un_error_inesperado_tampoco_tumba_el_chequeo():
    estados = HealthService(_FakeConnections(inesperado={"Neo4j"})).check_all()
    assert [s.ok for s in estados] == [True, True, False]
    assert "bug" not in estados[2].detail  # al usuario no se le muestran detalles técnicos


def test_los_tres_chequeos_corren_en_paralelo():
    inicio = time.monotonic()
    HealthService(_FakeConnections(demora=1.0)).check_all()
    assert time.monotonic() - inicio < 2.5  # en serie tardaría >= 3 s


def test_un_chequeo_colgado_se_corta_por_timeout():
    # Simula un socket medio muerto: el driver no responde aunque ya conectó.
    inicio = time.monotonic()
    estados = HealthService(_FakeConnections(demora=30), timeout_s=0.5).check_all()
    assert time.monotonic() - inicio < 3
    assert [s.ok for s in estados] == [False, False, False]
    assert all("tiempo de espera" in s.detail.lower() for s in estados)


def _config_inalcanzable(timeout_s: float) -> AppConfig:
    # Puerto 1: nadie escucha → falla real del driver, sin tocar los contenedores.
    pg = PostgresConfig("127.0.0.1", 1, "logistica", "nadie", "Clave-De-Prueba-1")
    return AppConfig(
        postgres_admin=pg,
        postgres_app=pg,
        mongo=MongoConfig("127.0.0.1", 1, "logistica", "nadie", "Clave-De-Prueba-1"),
        neo4j=Neo4jConfig("bolt://127.0.0.1:1", "neo4j", "Clave-De-Prueba-1"),
        log_level="INFO",
        log_dir=ROOT / "logs",
        db_timeout_s=timeout_s,
    )


def test_motores_inalcanzables_se_informan_dentro_del_timeout():
    conns = Connections(_config_inalcanzable(timeout_s=1.0))
    try:
        inicio = time.monotonic()
        estados = HealthService(conns).check_all()
        demora = time.monotonic() - inicio
    finally:
        conns.close()
    assert [s.ok for s in estados] == [False, False, False]
    assert demora < 8, f"tardó {demora:.1f}s"
    for s in estados:
        assert "Clave-De-Prueba-1" not in s.detail


def test_timeout_configurable_por_entorno(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("DB_TIMEOUT_S=9\n", encoding="utf-8")
    # load_dotenv escribe en os.environ y no se revierte solo: las claves se fijan con monkeypatch para que
    # no contaminen a las pruebas que usan el .env real.
    for clave in ("PG_ADMIN_PASSWORD", "MONGO_ADMIN_PASSWORD", "NEO4J_PASSWORD"):
        monkeypatch.setenv(clave, "x")
    monkeypatch.setenv("DB_TIMEOUT_S", "2.5")  # el entorno gana sobre el archivo
    assert load_config(env).db_timeout_s == 2.5
    monkeypatch.delenv("DB_TIMEOUT_S")
    assert load_config(env).db_timeout_s == 9.0
    monkeypatch.delenv("DB_TIMEOUT_S")  # load_dotenv lo volvió a cargar desde el archivo
    env.write_text("", encoding="utf-8")
    assert load_config(env).db_timeout_s == 5.0


# --------------------------------------------------------------------------- UI
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _esperar(qapp, condicion, segundos=10.0):
    limite = time.monotonic() + segundos
    while not condicion() and time.monotonic() < limite:
        qapp.processEvents()
        time.sleep(0.01)
    return condicion()


def test_la_ventana_no_se_congela_durante_un_chequeo_lento(qapp):
    from PySide6.QtCore import QTimer

    from logistica.ui.estado_view import EstadoView

    ventana = EstadoView(HealthService(_FakeConnections(demora=1.0, caidos={"Neo4j"})))
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(50)
    ventana.refresh()
    assert ventana.is_busy()
    assert _esperar(qapp, lambda: not ventana.is_busy())
    timer.stop()
    # Si el hilo gráfico se hubiera bloqueado 1 s no habría ticks durante la espera.
    assert len(ticks) >= 10, f"solo {len(ticks)} ticks: el hilo gráfico se bloqueó"
    assert ventana.engine_text("PostgreSQL").startswith("OK")
    assert ventana.engine_text("Neo4j").startswith("Sin conexión")
    ventana.cerrar()


def test_el_pool_suelta_cada_trabajador_al_terminar(qapp):
    from PySide6.QtCore import QObject, Slot

    from logistica.ui.worker import WorkerPool

    class Receptor(QObject):
        def __init__(self):
            super().__init__()
            self.ok, self.errores = [], []

        @Slot(object)
        def on_ok(self, r):
            self.ok.append(r)

        @Slot(str)
        def on_error(self, m):
            self.errores.append(m)

    def falla():
        raise errors.BusinessRuleError(user_message="Regla violada")

    receptor = Receptor()
    pool = WorkerPool()
    for i in range(5):
        pool.submit(lambda x=i: x * 2, receptor.on_ok, receptor.on_error)
    pool.submit(falla, receptor.on_ok, receptor.on_error)
    assert _esperar(qapp, lambda: len(receptor.ok) + len(receptor.errores) == 6)
    assert _esperar(qapp, lambda: pool.active_count() == 0)
    assert sorted(receptor.ok) == [0, 2, 4, 6, 8]
    assert receptor.errores == ["Regla violada"]  # al usuario le llega el mensaje propio, no la traza


def test_cerrar_la_ventana_espera_a_los_trabajadores(qapp):
    from logistica.ui.estado_view import EstadoView

    ventana = EstadoView(HealthService(_FakeConnections(demora=0.5)))
    ventana.refresh()
    inicio = time.monotonic()
    ventana.cerrar()
    assert time.monotonic() - inicio >= 0.3  # esperó al chequeo en curso
    assert not ventana.is_busy()
    ventana.refresh()  # después de cerrar no se acepta trabajo nuevo
    assert not ventana.is_busy()


def test_tema_visual_se_aplica(qapp):
    from logistica.ui.theme import apply_theme

    apply_theme(qapp)
    assert "QPushButton" in qapp.styleSheet()


# --------------------------------------------------------------------------- servidores reales
REALES = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y los tres motores levantados)",
)


@REALES
def test_conexiones_reales_los_tres_motores_responden():
    conns = Connections(load_config())
    try:
        estados = HealthService(conns).check_all()
    finally:
        conns.close()
    assert estados == [
        EngineStatus(e, True, "", s.latency_ms) for e, s in zip(("PostgreSQL", "MongoDB", "Neo4j"), estados)
    ]


@REALES
def test_conexiones_reales_la_app_usa_la_identidad_limitada():
    from sqlalchemy import text

    cfg = load_config()
    conns = Connections(cfg)
    try:
        with conns.pg_session() as s:
            assert s.execute(text("SELECT current_user")).scalar_one() == cfg.postgres_app.user
    finally:
        conns.close()
    assert cfg.postgres_app.user != cfg.postgres_admin.user


@REALES
def test_conexiones_reales_un_motor_caido_degrada_solo_ese():
    cfg = load_config()
    roto = replace(cfg, neo4j=replace(cfg.neo4j, uri="bolt://127.0.0.1:1"), db_timeout_s=1.0)
    conns = Connections(roto)
    try:
        estados = HealthService(conns).check_all()
    finally:
        conns.close()
    assert [s.ok for s in estados] == [True, True, False]
