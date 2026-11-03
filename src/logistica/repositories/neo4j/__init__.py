"""Repositorios de Neo4j (proyección reconstruible de rutas y relaciones desde PostgreSQL)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from neo4j import Session

from logistica.repositories.base import translate_errors
from logistica.repositories.connections import Connections

ENGINE = "Neo4j"


class Neo4jRepository:
    """Base de los repositorios de Neo4j.

    El driver es único por proceso; cada operación abre su propia sesión. Las escrituras usan `MERGE` por
    clave estable (Community no tiene unicidad sobre relaciones).
    """

    def __init__(self, connections: Connections):
        self._conn = connections

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with translate_errors(ENGINE), self._conn.neo4j.session() as session:
            yield session
