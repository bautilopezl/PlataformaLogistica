"""Fixtures compartidas.

`tx_connections`: un `Connections` real (identidad de ejecución PG_APP_USER, del `.env`) cuyas unidades de
trabajo corren dentro de UNA transacción externa que se revierte al final de cada prueba. Los servicios
hacen `commit` normalmente, pero SQLAlchemy lo convierte en un SAVEPOINT (`join_transaction_mode`), así que
las pruebas no dejan datos en la base. Requiere LOGISTICA_TEST_CONNECTIONS=1.
"""

from __future__ import annotations

import gc
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True, scope="session")
def _recoleccion_solo_en_el_hilo_principal():
    """Misma política que la app (ui/gc_guard.py): sin recolección automática, que podría correr en un hilo
    trabajador y destruir ahí widgets de pruebas anteriores (Qt aborta)."""
    gc.disable()
    yield
    gc.enable()


@pytest.fixture(autouse=True)
def _recolectar_al_terminar_cada_prueba():
    yield
    gc.collect()  # en el hilo principal


@pytest.fixture()
def tx_connections():
    from sqlalchemy.orm import Session

    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections

    class TxConnections(Connections):
        def __init__(self, config):
            super().__init__(config)
            self._outer_conn = self.engine.connect()
            self._outer_tx = self._outer_conn.begin()

        @contextmanager
        def pg_session(self):
            session = Session(
                bind=self._outer_conn, join_transaction_mode="create_savepoint", expire_on_commit=False
            )
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        def rollback_all(self):
            self._outer_tx.rollback()
            self._outer_conn.close()

    conns = TxConnections(load_config())
    try:
        yield conns
    finally:
        conns.rollback_all()
        conns.close()
