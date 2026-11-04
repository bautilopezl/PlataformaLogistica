"""Planificador de procesos periódicos (AGENTS.md §12, fase 8).

Cuatro tareas, cada una una estrategia (función) sobre servicios que YA son idempotentes:
- FACTURACION: consume las entregas `FACTURACION` de la outbox (facturas de las entregas confirmadas, §16.3).
- SENSORES_INACTIVOS: abre/escala alertas de falta de comunicación (§10.4, cada 60 s).
- CIERRE_MENSUAL: cierra los meses terminados de cada cuenta.
- PREDICCION_DIARIA: predicción de atrasos del día.

Coordinación entre instancias, en dos capas:
1. Advisory lock por TAREA y PERÍODO (`proceso:<tarea>:<período>`), en una conexión dedicada, liberado en
  `finally`.
   Dos instancias a la vez: una ejecuta y la otra omite.
2. Idempotencia por clave de negocio (cargo UNIQUE en factura_detalle, UNIQUE cuenta+período, UNIQUE
  envío+fecha):
   aunque una tarea se repitiera (otra instancia más tarde, un reinicio), el efecto es uno solo.

Al iniciar, la primera vuelta revisa todo lo pendiente (entregas sin facturar, meses sin cerrar, la predicción
de HOY). No se inventan cálculos de los días en que nada corrió: la predicción es solo la del día; los cierres
sí se recuperan porque se calculan con movimientos reales. SUPUESTOS P1..P4: ver AGENTS.md §16 ("Supuestos de
la fase 8").
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Callable

from logistica.core.errors import LogisticaError, PermissionDeniedError
from logistica.core.logging_setup import redact
from logistica.domain.procesos import FRECUENCIA, Frecuencia, ResultadoTarea, Tarea, clave_lock, periodo
from logistica.repositories.connections import Connections
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.locks import advisory_lock
from logistica.repositories.postgres.monitoreo import ahora as ahora_pg
from logistica.repositories.postgres.outbox import FACTURACION
from logistica.services.autorizacion import Autorizador

log = logging.getLogger(__name__)

EJECUTADA, OMITIDA, YA_HECHA, ERROR = "EJECUTADA", "OMITIDA", "YA_HECHA", "ERROR"


class Planificador:
    def __init__(
        self,
        connections: Connections,
        autorizador: Autorizador,
        *,
        sincronizacion,
        monitoreo,
        comercial,
        predicciones,
        reloj: Callable[[], datetime] | None = None,
    ):
        self._conn = connections
        self._aut = autorizador
        self._reloj = reloj  # None = reloj de PostgreSQL (referencia común a todas las instancias)
        self._tareas: dict[Tarea, Callable[[], str]] = {
            Tarea.FACTURACION: lambda: _texto_lote(sincronizacion.procesar(FACTURACION)),
            Tarea.SENSORES_INACTIVOS: lambda: _sensores(monitoreo),
            Tarea.CIERRE_MENSUAL: lambda: f"{comercial.cerrar_pendientes()} cierre(s)",
            Tarea.PREDICCION_DIARIA: lambda: _texto_prediccion(predicciones.calcular()),
        }
        # Período ya completado POR ESTA INSTANCIA (las diarias y mensuales no se repiten en cada vuelta). Es
        # solo un ahorro: la garantía de "un solo efecto" la dan el lock y las claves de negocio.
        self._hechas: dict[Tarea, str] = {}

    def ejecutar_pendientes(self) -> list[ResultadoTarea]:
        """Una vuelta: cada tarea que toca en este período. Un error en una no impide las demás."""
        ahora = self._ahora()
        resultados = []
        for tarea, funcion in self._tareas.items():
            clave_periodo = periodo(tarea, ahora)
            if FRECUENCIA[tarea] != Frecuencia.CADA_MINUTO and self._hechas.get(tarea) == clave_periodo:
                resultados.append(ResultadoTarea(tarea, clave_periodo, YA_HECHA))
                continue
            resultados.append(self._correr(tarea, clave_periodo, funcion))
        return resultados

    def ejecutar_con_sesion(self, sesion_id: uuid.UUID) -> list[ResultadoTarea]:
        """Desde la ventana: solo una instancia abierta por un usuario INTERNO procesa (SUPUESTO P1). La
        sesión se valida antes de cada vuelta: si se revocó, esta instancia deja de procesar."""
        with self._aut.operacion(sesion_id) as (_tx, p):
            if not p.es_interno:
                raise PermissionDeniedError("los procesos periódicos solo los ejecutan instancias internas")
        return self.ejecutar_pendientes()

    def _correr(self, tarea: Tarea, clave_periodo: str, funcion: Callable[[], str]) -> ResultadoTarea:
        try:
            with advisory_lock(self._conn.engine, clave_lock(tarea, clave_periodo)) as tomado:
                if not tomado:
                    log.info("Proceso %s (%s): lo está ejecutando otra instancia", tarea.value, clave_periodo)
                    return ResultadoTarea(tarea, clave_periodo, OMITIDA, "otra instancia lo está ejecutando")
                detalle = funcion()
        except Exception as exc:  # noqa: BLE001 — se registra y se sigue con las demás tareas
            if not isinstance(exc, LogisticaError):
                log.exception("Proceso %s (%s) falló", tarea.value, clave_periodo)
            else:
                log.warning("Proceso %s (%s) falló: %s", tarea.value, clave_periodo, exc)
            mensaje = getattr(exc, "user_message", None) or f"{type(exc).__name__}: {exc}"
            return ResultadoTarea(tarea, clave_periodo, ERROR, redact(str(mensaje)))
        if FRECUENCIA[tarea] != Frecuencia.CADA_MINUTO:
            self._hechas[tarea] = clave_periodo
        log.info("Proceso %s (%s): %s", tarea.value, clave_periodo, detalle)
        return ResultadoTarea(tarea, clave_periodo, EJECUTADA, detalle)

    def _ahora(self) -> datetime:
        if self._reloj is not None:
            return self._reloj()
        with unit_of_work(self._conn) as tx:
            return ahora_pg(tx)


def _sensores(monitoreo) -> str:
    """Primero evalúa las lecturas que ya llegaron (así el "último contacto" está al día, aunque en esta
    instancia no corra el ciclo de monitoreo de la ventana) y después revisa quién está sin comunicación."""
    evaluacion = monitoreo.evaluar_pendientes()
    cambios = monitoreo.evaluar_comunicacion()
    evaluadas = evaluacion.evaluadas
    return f"{evaluadas} lectura(s) evaluadas · {cambios} alerta(s) de comunicación nuevas o escaladas"


def _texto_lote(r) -> str:
    return f"{r.procesados} entrega(s) facturadas · {r.fallidos} con error · {r.pospuestos} pospuestas"


def _texto_prediccion(r) -> str:
    return f"{r.calculadas} predicción(es) nuevas · {r.existentes} ya calculadas hoy"
