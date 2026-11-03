"""Prueba del script opcional de particionamiento (db/postgres/opcional/950_particionar_hash.sql).

Comprueba que el proyecto está PREPARADO para particionar: el script convierte lectura_procesada y
outbox_entrega a particionadas por hash conservando datos, claves e idempotencia. No lo activa en la base:
corre dentro de una transacción que se revierte. Requiere LOGISTICA_TEST_PG (ver test_postgres_integrity.py).
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import psycopg
import pytest

CONNINFO = os.environ.get("LOGISTICA_TEST_PG")
pytestmark = pytest.mark.skipif(not CONNINFO, reason="definir LOGISTICA_TEST_PG para correr pruebas de integración")

SCRIPT = Path(__file__).resolve().parents[1] / "db" / "postgres" / "opcional" / "950_particionar_hash.sql"


def _sql() -> str:
    # El script trae su propio BEGIN/COMMIT; en la prueba se ejecuta dentro de nuestra transacción.
    return SCRIPT.read_text(encoding="utf-8").replace("BEGIN;", "").replace("COMMIT;", "")


@pytest.fixture()
def conn():
    c = psycopg.connect(CONNINFO)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def relkind(conn, tabla: str) -> str:
    return conn.execute("SELECT relkind FROM pg_class WHERE relname = %s AND relnamespace = 'public'::regnamespace",
                        (tabla,)).fetchone()[0]


def _sembrar(conn):
    lecturas = [uuid.uuid4() for _ in range(20)]
    for lid in lecturas:
        conn.execute("INSERT INTO lectura_procesada (lectura_id, version_reglas) VALUES (%s, 'v1')", (lid,))
    evento = uuid.uuid4()
    conn.execute("INSERT INTO outbox_evento (id, agregado_tipo, agregado_id, agregado_version, evento_tipo, payload) "
                 "VALUES (%s, 'envio', %s, 1, 'envio.creado', %s)", (evento, uuid.uuid4(), json.dumps({})))
    for destino in ("MONGO_EVENTOS", "NEO4J_LOGISTICA"):
        conn.execute("INSERT INTO outbox_entrega (evento_id, destino) VALUES (%s, %s)", (evento, destino))
    return lecturas, evento


def test_script_particiona_conservando_datos_e_idempotente(conn):
    if relkind(conn, "lectura_procesada") != "r":
        pytest.skip("la base ya tiene el particionamiento activado")
    lecturas, evento = _sembrar(conn)

    conn.execute(_sql())
    assert relkind(conn, "lectura_procesada") == "p" and relkind(conn, "outbox_entrega") == "p"
    parts = conn.execute("SELECT count(*) FROM pg_inherits WHERE inhparent = 'lectura_procesada'::regclass").fetchone()[0]
    assert parts == 4
    assert conn.execute("SELECT count(*) FROM lectura_procesada").fetchone()[0] == 20
    assert conn.execute("SELECT count(*) FROM outbox_entrega WHERE evento_id = %s", (evento,)).fetchone()[0] == 2
    # los datos se reparten entre varias particiones
    usadas = conn.execute("SELECT count(DISTINCT tableoid) FROM lectura_procesada").fetchone()[0]
    assert usadas > 1

    conn.execute(_sql())  # segunda ejecución: no hace nada y no falla
    assert conn.execute("SELECT count(*) FROM lectura_procesada").fetchone()[0] == 20


def test_las_claves_siguen_garantizando_unicidad_y_fk(conn):
    if relkind(conn, "lectura_procesada") != "r":
        pytest.skip("la base ya tiene el particionamiento activado")
    lecturas, evento = _sembrar(conn)
    conn.execute(_sql())

    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            conn.execute("INSERT INTO lectura_procesada (lectura_id, version_reglas) VALUES (%s, 'v1')", (lecturas[0],))
    # otra versión de reglas de la misma lectura sí es válida
    conn.execute("INSERT INTO lectura_procesada (lectura_id, version_reglas) VALUES (%s, 'v2')", (lecturas[0],))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            conn.execute("INSERT INTO outbox_entrega (evento_id, destino) VALUES (%s, 'MONGO_EVENTOS')", (evento,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            conn.execute("INSERT INTO outbox_entrega (evento_id, destino) VALUES (%s, 'X')", (uuid.uuid4(),))
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("UPDATE outbox_entrega SET estado = 'COMPLETADA' WHERE evento_id = %s", (evento,))


def test_una_consulta_por_lectura_toca_una_sola_particion(conn):
    if relkind(conn, "lectura_procesada") != "r":
        pytest.skip("la base ya tiene el particionamiento activado")
    lecturas, _ = _sembrar(conn)
    conn.execute(_sql())
    plan = "\n".join(r[0] for r in conn.execute(
        "EXPLAIN SELECT * FROM lectura_procesada WHERE lectura_id = %s", (lecturas[0],)).fetchall())
    assert len(set(re.findall(r"lectura_procesada_p\d", plan))) == 1, plan  # poda: solo una partición
