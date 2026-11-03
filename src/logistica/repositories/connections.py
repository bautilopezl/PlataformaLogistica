"""Fábricas de conexión directa a los tres motores.

- PostgreSQL: un Engine (pool) por proceso; cada trabajador abre su propia Session.
- MongoDB: un MongoClient por proceso (es thread-safe).
- Neo4j: un Driver por proceso; cada operación abre su propia sesión.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from neo4j import Driver, GraphDatabase
from pymongo import MongoClient
from pymongo.database import Database
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from logistica.core.config import AppConfig
from logistica.core.errors import DatabaseUnavailableError

log = logging.getLogger(__name__)


class Connections:
    def __init__(self, config: AppConfig):
        self._config = config
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._mongo: MongoClient | None = None
        self._neo4j: Driver | None = None
        self._timeout = config.db_timeout_s
        # Los clientes se crean al primer uso, que puede ocurrir en varios trabajadores a la vez: el lock
        # evita crear dos pools del mismo motor (uno quedaría sin cerrar).
        self._lock = threading.Lock()

    # --- PostgreSQL -------------------------------------------------------
    @property
    def engine(self) -> Engine:
        with self._lock:
            if self._engine is None:
                self._engine = create_engine(
                    self._config.postgres_app.sqlalchemy_url(),
                    pool_size=5,
                    max_overflow=5,
                    pool_pre_ping=True,
                    # Los mensajes de error no incluyen los valores de los parámetros (hashes, datos
                    # personales): solo la sentencia. Así no llegan a los logs.
                    hide_parameters=True,
                    pool_timeout=self._timeout,
                    # libpq solo acepta segundos enteros (mínimo 1).
                    connect_args={"connect_timeout": max(1, round(self._timeout))},
                )
                self._session_factory = sessionmaker(self._engine, expire_on_commit=False)
            return self._engine

    @contextmanager
    def pg_session(self) -> Iterator[Session]:
        """Unidad de trabajo: commit al salir bien, rollback ante error."""
        engine = self.engine
        with self._lock:
            factory = self._session_factory or sessionmaker(engine, expire_on_commit=False)
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # --- MongoDB ----------------------------------------------------------
    @property
    def mongo_db(self) -> Database:
        with self._lock:
            if self._mongo is None:
                self._mongo = MongoClient(
                    self._config.mongo.uri(),
                    uuidRepresentation="standard",
                    tz_aware=True,
                    serverSelectionTimeoutMS=int(self._timeout * 1000),
                    connectTimeoutMS=int(self._timeout * 1000),
                )
            return self._mongo[self._config.mongo.database]

    # --- Neo4j ------------------------------------------------------------
    @property
    def neo4j(self) -> Driver:
        with self._lock:
            if self._neo4j is None:
                n = self._config.neo4j
                self._neo4j = GraphDatabase.driver(
                    n.uri,
                    auth=(n.user, n.password),
                    connection_timeout=self._timeout,
                    connection_acquisition_timeout=self._timeout,
                    # Sin esto, execute_write reintenta hasta 30 s si Neo4j no responde. Los reintentos ya
                    # los hace la outbox (con espera creciente y registro de la causa): acá se falla rápido.
                    max_transaction_retry_time=self._timeout,
                )
            return self._neo4j

    # --- Salud ------------------------------------------------------------
    # Cualquier falla del chequeo (red, credenciales, timeout) se informa como motor no disponible.
    def check_postgres(self) -> None:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise DatabaseUnavailableError("PostgreSQL", str(exc)) from exc

    def check_mongo(self) -> None:
        try:
            self.mongo_db.client.admin.command("ping")
        except Exception as exc:
            raise DatabaseUnavailableError("MongoDB", str(exc)) from exc

    def check_neo4j(self) -> None:
        try:
            self.neo4j.verify_connectivity()
        except Exception as exc:
            raise DatabaseUnavailableError("Neo4j", str(exc)) from exc

    def close(self) -> None:
        """Libera los tres motores. Es seguro llamarlo más de una vez."""
        with self._lock:
            engine, mongo, neo = self._engine, self._mongo, self._neo4j
            self._engine = self._session_factory = self._mongo = self._neo4j = None
        if engine is not None:
            engine.dispose()
        if mongo is not None:
            mongo.close()
        if neo is not None:
            neo.close()
        log.info("Conexiones cerradas")
