"""Predicción diaria de atrasos (AGENTS.md §10.5): método FRECUENCIA_HISTORICA 1.0.

`calcular()` es el proceso técnico (la fase 8 lo programará una vez por día con advisory lock). Es
idempotente: la clave (envío, fecha de evaluación, método, versión) es UNIQUE, así que repetirlo el mismo día
no duplica. `calcular_ahora()` deja que un usuario interno lo dispare a mano (SUPUESTO R4).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timezone

from logistica.core.errors import PermissionDeniedError
from logistica.domain import analitica as da
from logistica.domain.acceso import Permiso
from logistica.repositories.connections import Connections
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.analitica import HechosRepository, PrediccionDTO, PrediccionRepository
from logistica.repositories.postgres.monitoreo import ahora as ahora_pg
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador
from logistica.services.envio_service import _alcance

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultadoPrediccion:
    calculadas: int  # nuevas de hoy
    existentes: int  # ya estaban calculadas hoy (idempotencia)


class PrediccionService:
    def __init__(
        self, connections: Connections, autorizador: Autorizador, metodo: da.MetodoPrediccion | None = None
    ):
        self._conn = connections
        self._aut = autorizador
        self._metodo = metodo or da.MetodoPrediccion()
        self._hechos = HechosRepository()
        self._repo = PrediccionRepository()

    def calcular(self) -> ResultadoPrediccion:
        """Evalúa los envíos PLANIFICADOS y EN TRÁNSITO con el historial conocido HASTA AHORA (reloj de
        PostgreSQL): nunca se usan resultados posteriores al cálculo ni se inventan cálculos de días en que no
        corrió."""
        with unit_of_work(self._conn) as tx:
            corte = ahora_pg(tx)
            dia = corte.astimezone(timezone.utc).date()  # día UTC (SUPUESTO R2)
            objetivos = set(self._repo.objetivos(tx))
            historial = self._hechos.envios(tx)
            nuevas = existentes = 0
            for e in historial:
                if e.id not in objetivos:
                    continue
                p = da.predecir(e.ruta_clave, historial, corte, self._metodo, e.ruta_nombre)
                if self._repo.guardar(tx, e.id, dia, self._metodo.codigo, self._metodo.version, p):
                    nuevas += 1
                else:
                    existentes += 1
        log.info("Predicción de atrasos: %d nuevas, %d ya calculadas hoy", nuevas, existentes)
        return ResultadoPrediccion(nuevas, existentes)

    def calcular_ahora(self, sesion_id: uuid.UUID) -> ResultadoPrediccion:
        with self._aut.operacion(sesion_id, Permiso.PREDICCIONES_CONSULTAR) as (tx, p):
            if not p.es_interno:
                raise PermissionDeniedError(f"{p.usuario_id} externo no dispara el cálculo")
            auditoria.registrar(tx, "calcular_predicciones", p)
        return self.calcular()

    def predicciones(self, sesion_id: uuid.UUID) -> list[PrediccionDTO]:
        with self._aut.operacion(sesion_id, Permiso.PREDICCIONES_CONSULTAR) as (tx, p):
            return self._repo.vigentes(tx, alcance=_alcance(p))
