#!/usr/bin/env python3
"""Crea (o actualiza) las tres bases de datos. Idempotente.

    python scripts/setup_databases.py                 # las tres
    python scripts/setup_databases.py --only postgres
    python scripts/setup_databases.py --reset --yes   # BORRA todo y recrea
    python scripts/setup_databases.py --only postgres --particionar   # activa el particionamiento (opcional)

Usa las credenciales de administración del .env (nunca las de ejecución).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "db" / "mongo"))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from pymongo import MongoClient  # noqa: E402
from neo4j import GraphDatabase  # noqa: E402

import setup_mongo  # noqa: E402
from logistica.core.config import AppConfig, load_config  # noqa: E402

log = logging.getLogger("setup_databases")

PG_DIR = ROOT / "db" / "postgres"
CYPHER_FILE = ROOT / "db" / "neo4j" / "constraints.cypher"
PG_OPCIONAL_DIR = PG_DIR / "opcional"   # scripts que NO se ejecutan por defecto (particionamiento)


# ---------------------------------------------------------------- PostgreSQL
def setup_postgres(cfg: AppConfig, reset: bool, particionar: bool = False) -> None:
    admin = cfg.postgres_admin
    with psycopg.connect(admin.conninfo(), autocommit=True) as conn:
        if reset:
            log.warning("Reiniciando esquema public de PostgreSQL")
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        for path in sorted(PG_DIR.glob("*.sql")):
            log.info("PostgreSQL: ejecutando %s", path.name)
            conn.execute(path.read_text(encoding="utf-8"))
        if particionar:
            for path in sorted(PG_OPCIONAL_DIR.glob("*.sql")):
                log.info("PostgreSQL: ejecutando opcional %s", path.name)
                conn.execute(path.read_text(encoding="utf-8"))
        _provision_app_role(conn, cfg)


def _provision_app_role(conn: psycopg.Connection, cfg: AppConfig) -> None:
    app = cfg.postgres_app
    if app.user == cfg.postgres_admin.user:
        log.warning("PG_APP_USER es igual al de administración: se omite el rol de ejecución (solo desarrollo)")
        return
    user = sql.Identifier(app.user)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app.user,)).fetchone()
    verb = "ALTER" if exists else "CREATE"
    conn.execute(sql.SQL("{} ROLE {} LOGIN PASSWORD {}").format(sql.SQL(verb), user, sql.Literal(app.password)))
    conn.execute(sql.SQL("GRANT logistica_app_rw TO {}").format(user))
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(app.database), user))
    log.info("PostgreSQL: usuario de ejecución %s listo", app.user)


# ---------------------------------------------------------------- MongoDB
def setup_mongodb(cfg: AppConfig, reset: bool) -> None:
    client = MongoClient(cfg.mongo.uri(), uuidRepresentation="standard", serverSelectionTimeoutMS=5000)
    try:
        db = client[cfg.mongo.database]
        if reset:
            log.warning("Eliminando colecciones de MongoDB")
            setup_mongo.drop_all(db)
        setup_mongo.apply(db)
    finally:
        client.close()


# ---------------------------------------------------------------- Neo4j
def _cypher_statements(text: str) -> list[str]:
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("//")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def setup_neo4j(cfg: AppConfig, reset: bool) -> None:
    n = cfg.neo4j
    driver = GraphDatabase.driver(n.uri, auth=(n.user, n.password))
    try:
        with driver.session() as session:
            if reset:
                log.warning("Eliminando todos los nodos de Neo4j")
                session.run("MATCH (n) DETACH DELETE n").consume()
            for stmt in _cypher_statements(CYPHER_FILE.read_text(encoding="utf-8")):
                log.info("Neo4j: %s", stmt.splitlines()[0][:90])
                session.run(stmt).consume()
    finally:
        driver.close()


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=["postgres", "mongo", "neo4j"], action="append",
                        help="limitar a un motor (se puede repetir)")
    parser.add_argument("--reset", action="store_true", help="borra los datos antes de crear")
    parser.add_argument("--yes", action="store_true", help="confirma --reset")
    parser.add_argument("--particionar", action="store_true",
                        help="PostgreSQL: particiona por hash lectura_procesada y outbox_entrega (db/postgres/opcional)")
    args = parser.parse_args()
    if args.reset and not args.yes:
        parser.error("--reset borra todos los datos; confirmar con --yes")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = load_config()
    targets = args.only or ["postgres", "mongo", "neo4j"]
    steps = {"postgres": setup_postgres, "mongo": setup_mongodb, "neo4j": setup_neo4j}
    failed = False
    for name in targets:
        try:
            if name == "postgres":
                steps[name](cfg, args.reset, args.particionar)
            else:
                steps[name](cfg, args.reset)
            log.info("OK %s", name)
        except Exception:
            failed = True
            log.exception("Falló la creación de %s", name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
