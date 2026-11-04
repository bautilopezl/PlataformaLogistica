"""Procesos periódicos (AGENTS.md §12): qué tareas hay, cada cuánto tocan y qué período cubre cada corrida.

Funciones puras. La coordinación entre instancias (advisory lock por tarea y período) y la idempotencia por
clave de negocio están en los repositorios y servicios; acá solo se decide "qué período es ahora" y "qué meses
faltan cerrar".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum


class Frecuencia(StrEnum):
    CADA_MINUTO = "CADA_MINUTO"  # §10.4: la comunicación se revisa cada 60 s
    DIARIA = "DIARIA"
    MENSUAL = "MENSUAL"


class Tarea(StrEnum):
    FACTURACION = "FACTURACION"  # facturas de las entregas confirmadas (outbox FACTURACION)
    SENSORES_INACTIVOS = "SENSORES_INACTIVOS"  # alertas de falta de comunicación
    CIERRE_MENSUAL = "CIERRE_MENSUAL"  # cierre de cuentas de los meses terminados
    PREDICCION_DIARIA = "PREDICCION_DIARIA"  # predicción de atrasos del día


FRECUENCIA = {
    Tarea.FACTURACION: Frecuencia.CADA_MINUTO,
    Tarea.SENSORES_INACTIVOS: Frecuencia.CADA_MINUTO,
    Tarea.CIERRE_MENSUAL: Frecuencia.MENSUAL,
    Tarea.PREDICCION_DIARIA: Frecuencia.DIARIA,
}


def periodo(tarea: Tarea, ahora: datetime) -> str:
    """Clave del período que cubre una corrida, en UTC (SUPUESTO P2). Dos instancias que corren la misma tarea
    en el mismo período compiten por el MISMO lock: solo una la ejecuta."""
    t = ahora.astimezone(timezone.utc)
    frecuencia = FRECUENCIA[tarea]
    if frecuencia == Frecuencia.MENSUAL:
        anterior = mes_anterior(t.date())
        return f"{anterior:%Y-%m}"  # el cierre de un mes se hace cuando ese mes terminó
    if frecuencia == Frecuencia.DIARIA:
        return f"{t:%Y-%m-%d}"
    return f"{t:%Y-%m-%dT%H:%M}"


def clave_lock(tarea: Tarea, periodo_clave: str) -> str:
    return f"proceso:{tarea.value}:{periodo_clave}"


def mes_anterior(d: date) -> date:
    return date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)


def mes_siguiente(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def meses_a_cerrar(
    primer_movimiento: datetime | None, ultimo_cierre: date | None, ahora: datetime
) -> list[date]:
    """Meses TERMINADOS que una cuenta todavía no cerró, en orden (los cierres avanzan). Desde el mes de su
    primer movimiento (o el siguiente al último cierre) hasta el mes anterior al actual (UTC). Si la instancia
    estuvo apagada, recupera los meses que faltan: se calculan con movimientos reales, no se inventa nada."""
    if primer_movimiento is None:
        return []
    actual = ahora.astimezone(timezone.utc).date().replace(day=1)
    desde = (
        mes_siguiente(ultimo_cierre)
        if ultimo_cierre
        else primer_movimiento.astimezone(timezone.utc).date().replace(day=1)
    )
    meses = []
    while desde < actual:
        meses.append(desde)
        desde = mes_siguiente(desde)
    return meses


@dataclass(frozen=True)
class ResultadoTarea:
    tarea: Tarea
    periodo: str
    estado: (
        str  # EJECUTADA | OMITIDA (otra instancia la tiene) | YA_HECHA (esta instancia, este período) | ERROR
    )
    detalle: str = ""
