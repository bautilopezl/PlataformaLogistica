"""Repositorios de MongoDB (lecturas de sensores, eventos logísticos y mensajes)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from pymongo.collection import Collection

from logistica.repositories.base import translate_errors
from logistica.repositories.connections import Connections

ENGINE = "MongoDB"


class MongoRepository:
    """Base de los repositorios de MongoDB. Cada subclase define `COLLECTION`.

    El `MongoClient` es único por proceso (es thread-safe); las operaciones deben ser idempotentes por `_id`.
    """

    COLLECTION: str = ""

    def __init__(self, connections: Connections):
        self._conn = connections

    @contextmanager
    def _collection(self) -> Iterator[Collection]:
        if not self.COLLECTION:
            raise NotImplementedError("la subclase debe definir COLLECTION")
        with translate_errors(ENGINE):
            yield self._conn.mongo_db[self.COLLECTION]
