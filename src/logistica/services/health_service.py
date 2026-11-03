"""Servicio de estado de los motores (capa de servicios).

Consulta los tres motores en paralelo: el tiempo total es el del más lento y no la suma. Nunca lanza: un
motor caído, colgado o con un error inesperado se informa como estado, para que la app siga funcionando
con lo que sí responde.

Cada chequeo corre en un hilo daemon y se espera como máximo `timeout_s` más un margen. Los timeouts de los
drivers cubren la conexión, pero no un socket ya abierto que deja de responder; sin este límite, un motor
colgado dejaría la ventana en "Verificando…" y trabaría el cierre de la app. Un hilo daemon abandonado no
impide que el proceso termine.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from logistica.core.errors import DatabaseUnavailableError

log = logging.getLogger(__name__)


class HealthChecks(Protocol):
    """Lo que el servicio necesita de `Connections` (permite probarlo con un doble)."""

    def check_postgres(self) -> None: ...
    def check_mongo(self) -> None: ...
    def check_neo4j(self) -> None: ...


@dataclass(frozen=True)
class EngineStatus:
    engine: str
    ok: bool
    detail: str = ""
    latency_ms: int = 0


# Margen sobre el timeout del driver: deja que el driver informe su propio error antes de cortar.
_GRACE_S = 1.0


class HealthService:
    def __init__(self, connections: HealthChecks, timeout_s: float = 5.0):
        self._conn = connections
        self._timeout_s = timeout_s

    def check_all(self) -> list[EngineStatus]:
        checks: tuple[tuple[str, Callable[[], None]], ...] = (
            ("PostgreSQL", self._conn.check_postgres),
            ("MongoDB", self._conn.check_mongo),
            ("Neo4j", self._conn.check_neo4j),
        )
        results: dict[str, EngineStatus] = {}

        def run(name: str, check: Callable[[], None]) -> None:
            results[name] = self._check_one(name, check)

        threads = [threading.Thread(target=run, args=c, name=f"health-{c[0]}", daemon=True) for c in checks]
        for t in threads:
            t.start()
        deadline = time.monotonic() + self._timeout_s + _GRACE_S
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        # Mismo orden que `checks`; lo que no terminó a tiempo se informa como colgado.
        return [results.get(name) or self._timed_out(name) for name, _ in checks]

    def _timed_out(self, name: str) -> EngineStatus:
        log.warning("%s no respondió en %.1f s", name, self._timeout_s + _GRACE_S)
        return EngineStatus(
            name,
            False,
            f"Tiempo de espera agotado: {name} no respondió.",
            round((self._timeout_s + _GRACE_S) * 1000),
        )

    @staticmethod
    def _check_one(name: str, check: Callable[[], None]) -> EngineStatus:
        start = time.monotonic()
        try:
            check()
        except DatabaseUnavailableError as exc:
            log.warning("%s no disponible: %s", name, exc)
            return EngineStatus(name, False, exc.user_message, _elapsed_ms(start))
        except Exception:
            log.exception("Error inesperado al verificar %s", name)
            return EngineStatus(
                name, False, f"Error inesperado al verificar {name}. Ver logs.", _elapsed_ms(start)
            )
        return EngineStatus(name, True, "", _elapsed_ms(start))


def _elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)
