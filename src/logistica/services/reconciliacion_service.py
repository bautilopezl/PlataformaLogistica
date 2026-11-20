"""Reconciliación entre motores y de integridad comercial (AGENTS.md §13/§14, fase 9).

Proceso TÉCNICO (como `SincronizacionService`/`PrediccionService`): sin sesión de usuario, pensado para
correr periódicamente (el planificador de la fase 8, tarea RECONCILIACION) o a mano
(`scripts/reconciliar.py`). Solo lee y compara; nunca repara ni reconstruye nada.
"""

from __future__ import annotations

import logging

from logistica.core.errors import DatabaseUnavailableError
from logistica.domain.reconciliacion import Discrepancia, EntregaFallida, ResultadoReconciliacion
from logistica.repositories.connections import Connections
from logistica.repositories.mongo.eventos import EventosRepository
from logistica.repositories.neo4j.proyeccion import ProyeccionRepository
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.monitoreo import ahora as ahora_pg
from logistica.repositories.postgres.outbox import OutboxRepository
from logistica.repositories.postgres.reconciliacion import ReconciliacionRepository

log = logging.getLogger(__name__)

# SUPUESTO (AGENTS.md §16.6, igual criterio que la fase 8): acota la corrida para que sea rápida; el
# volumen académico no necesita revisar todo el historial para detectar un patrón de desincronización.
LIMITE_ENVIOS = 200


class ReconciliacionService:
    def __init__(self, connections: Connections):
        self._conn = connections
        self._repo = ReconciliacionRepository()
        self._outbox = OutboxRepository()
        self._eventos = EventosRepository(connections)
        self._proyeccion = ProyeccionRepository(connections)

    def ejecutar(self, *, limite_envios: int = LIMITE_ENVIOS) -> ResultadoReconciliacion:
        with unit_of_work(self._conn) as tx:
            ahora = ahora_pg(tx)
            resumen_outbox = self._outbox.resumen(tx)
            entregas_fallidas = self._entregas_fallidas(tx)
            envios = self._repo.envios_recientes(tx, limite=limite_envios)
            mongo_completadas = self._repo.completadas_mongo_por_envio(tx, [e[0] for e in envios])
            cuentas_discrepancias = self._discrepancias_comerciales(tx)
            cuentas_revisadas = len(self._repo.cuentas_activas(tx))

        motores: dict[str, bool] = {}
        discrepancias: list[Discrepancia] = list(cuentas_discrepancias)
        discrepancias += self._discrepancias_neo4j(envios, motores)
        discrepancias += self._discrepancias_mongo(envios, mongo_completadas, motores)

        resultado = ResultadoReconciliacion(
            ejecutado_en=ahora,
            motores_disponibles=motores,
            resumen_outbox=resumen_outbox,
            entregas_fallidas=entregas_fallidas,
            discrepancias=discrepancias,
            envios_revisados=len(envios),
            cuentas_revisadas=cuentas_revisadas,
        )
        log.info("Reconciliación: %d discrepancia(s), %d entrega(s) fallida(s)",
                 len(discrepancias), len(entregas_fallidas))
        return resultado

    def _entregas_fallidas(self, tx) -> list[EntregaFallida]:
        """Los eventos que agotaron sus reintentos (AGENTS.md §9): la fase 3 ya los deja en FALLIDA, acá
        se listan en detalle para el reporte de reconciliación."""
        return [EntregaFallida(*fila) for fila in self._repo.entregas_fallidas(tx)]

    def _discrepancias_comerciales(self, tx) -> list[Discrepancia]:
        discrepancias = []
        for p in self._repo.pagos_sobreaplicados(tx):
            discrepancias.append(
                Discrepancia(
                    "PAGO_SOBREAPLICADO", "pago", str(p["id"]),
                    f"monto {p['monto']}, aplicado {p['aplicado']} (supera el disponible)",
                )
            )
        for f in self._repo.facturas_sobreaplicadas(tx):
            discrepancias.append(
                Discrepancia(
                    "FACTURA_SOBREAPLICADA", "factura", str(f["id"]),
                    f"total {f['importe_total']}, aplicado {f['aplicado']} (supera el saldo)",
                )
            )
        for c in self._repo.cierres_inconsistentes(tx):
            discrepancias.append(
                Discrepancia(
                    "CIERRE_INCONSISTENTE", "cierre_cuenta", str(c["id"]),
                    f"período {c['periodo']}: guardado {c['guardado']} vs. recalculado desde los "
                    f"movimientos {c['recalculado']}",
                )
            )
        return discrepancias

    def _discrepancias_neo4j(self, envios, motores: dict[str, bool]) -> list[Discrepancia]:
        discrepancias: list[Discrepancia] = []
        try:
            for envio_id, codigo, version in envios:
                grafo = self._proyeccion.subgrafo(envio_id)
                if grafo["nodos_envio"] == 0:
                    discrepancias.append(
                        Discrepancia(
                            "NEO4J", "envio", codigo,
                            "no tiene nodo :Envio en Neo4j (¿la proyección nunca corrió para este envío?)",
                        )
                    )
                elif grafo["version"] is not None and grafo["version"] < version:
                    discrepancias.append(
                        Discrepancia(
                            "NEO4J", "envio", codigo,
                            f"versión proyectada {grafo['version']} < versión actual {version} "
                            "(outbox NEO4J_LOGISTICA pendiente o fallida explica el atraso; revisar "
                            "resumen_outbox antes de tratarlo como error)",
                        )
                    )
            motores["NEO4J"] = True
        except DatabaseUnavailableError:
            log.warning("Reconciliación: Neo4j no disponible, se omiten sus controles")
            motores["NEO4J"] = False
        return discrepancias

    def _discrepancias_mongo(self, envios, completadas: dict, motores: dict[str, bool]) -> list[Discrepancia]:
        discrepancias: list[Discrepancia] = []
        try:
            for envio_id, codigo, _version in envios:
                if completadas.get(envio_id, 0) > 0 and len(self._eventos.de_envio(envio_id)) == 0:
                    discrepancias.append(
                        Discrepancia(
                            "MONGO", "envio", codigo,
                            f"la outbox marca {completadas[envio_id]} entrega(s) MONGO_EVENTOS como "
                            "COMPLETADA pero no hay ningún documento en eventos_logisticos para este envío",
                        )
                    )
            motores["MONGO"] = True
        except DatabaseUnavailableError:
            log.warning("Reconciliación: Mongo no disponible, se omiten sus controles")
            motores["MONGO"] = False
        return discrepancias
