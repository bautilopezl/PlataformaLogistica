"""Pruebas estáticas de los scripts de creación (no requieren servidores)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db" / "mongo"))


def test_scripts_postgres_son_sql_valido():
    pglast = pytest.importorskip("pglast")
    files = sorted((ROOT / "db" / "postgres").glob("*.sql"))
    assert files, "no hay scripts SQL"
    for f in files:
        pglast.parse_sql(f.read_text(encoding="utf-8"))  # lanza si hay error de sintaxis


def test_catalogo_postgres_declara_38_tablas():
    ddl = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "db" / "postgres").glob("0[1-5]0_*.sql"))
    assert ddl.count("CREATE TABLE IF NOT EXISTS") == 38  # 37 + auditoria


def test_validadores_mongo_son_serializables_y_cubren_las_colecciones():
    bson = pytest.importorskip("bson")
    import setup_mongo

    assert set(setup_mongo.COLECCIONES) == {"lecturas_sensor", "eventos_logisticos", "mensajes"}
    for validator in setup_mongo.COLECCIONES.values():
        bson.encode(validator)
        assert "$jsonSchema" in validator


def test_indices_mongo_exigidos_por_el_diseno():
    import setup_mongo

    nombres = {n for idx in setup_mongo.INDICES.values() for _, n, _ in idx}
    assert "ix_lectura_posicion_2dsphere" in nombres      # visualización geográfica
    assert "ix_lectura_sensor_fecha" in nombres           # consulta histórica por sensor
    assert "ix_mensaje_conversacion_fecha_id" in nombres  # paginación estable del chat


def test_cypher_declara_unicidad_para_los_nodos_principales():
    text = (ROOT / "db" / "neo4j" / "constraints.cypher").read_text(encoding="utf-8")
    for label in ("Tercero", "Envio", "Tramo", "Ubicacion", "Contenedor"):
        assert f"(n:{label}) REQUIRE n.id IS UNIQUE" in text


def test_sharding_de_lecturas_solo_se_aplica_en_cluster():
    from unittest.mock import MagicMock

    import setup_mongo

    assert list(setup_mongo.SHARD_KEYS["lecturas_sensor"]) == ["sensor_id", "fecha_medicion"]
    # La clave de sharding debe estar respaldada por un índice con el mismo prefijo.
    claves = [dict(k) if not isinstance(k, str) else {k: 1} for k, _, _ in setup_mongo.INDICES["lecturas_sensor"]]
    assert any(list(c)[:2] == ["sensor_id", "fecha_medicion"] for c in claves)

    db = MagicMock()
    db.name = "logistica"
    db.client.admin.command.return_value = {"msg": "isdbgrid"}
    assert setup_mongo.aplicar_sharding(db) is True
    llamadas = [c.args[0] for c in db.client.admin.command.call_args_list]
    assert llamadas == ["hello", "enableSharding", "shardCollection"]

    db.client.admin.command.reset_mock()
    db.client.admin.command.return_value = {"isWritablePrimary": True}
    assert setup_mongo.aplicar_sharding(db) is False
    assert [c.args[0] for c in db.client.admin.command.call_args_list] == ["hello"]
