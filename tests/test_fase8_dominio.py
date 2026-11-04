"""Fase 8 — períodos de las tareas y meses a cerrar (sin base)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from logistica.domain.procesos import Tarea, clave_lock, meses_a_cerrar, periodo

AHORA = datetime(2026, 11, 4, 12, 30, 45, tzinfo=timezone.utc)


def test_periodo_de_cada_tarea_en_utc():
    assert periodo(Tarea.PREDICCION_DIARIA, AHORA) == "2026-11-04"
    assert periodo(Tarea.CIERRE_MENSUAL, AHORA) == "2026-10"  # se cierra el mes que terminó
    assert periodo(Tarea.SENSORES_INACTIVOS, AHORA) == "2026-11-04T12:30"
    menos3 = datetime(2026, 11, 4, 22, 0, tzinfo=timezone(timedelta(hours=-3)))  # = 5/11 01:00 UTC
    assert periodo(Tarea.PREDICCION_DIARIA, menos3) == "2026-11-05"
    assert periodo(Tarea.CIERRE_MENSUAL, datetime(2026, 1, 3, tzinfo=timezone.utc)) == "2025-12"
    assert clave_lock(Tarea.CIERRE_MENSUAL, "2026-10") == "proceso:CIERRE_MENSUAL:2026-10"


def test_meses_a_cerrar_desde_el_primer_movimiento_o_el_ultimo_cierre():
    primero = datetime(2026, 7, 20, tzinfo=timezone.utc)
    assert meses_a_cerrar(primero, None, AHORA) == [
        date(2026, 7, 1),
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
    ]
    assert meses_a_cerrar(primero, date(2026, 9, 1), AHORA) == [date(2026, 10, 1)]
    assert meses_a_cerrar(primero, date(2026, 10, 1), AHORA) == []  # al día
    assert meses_a_cerrar(None, None, AHORA) == []  # cuenta sin movimientos: nada que cerrar
    assert meses_a_cerrar(datetime(2026, 11, 2, tzinfo=timezone.utc), None, AHORA) == []  # mes en curso: no


def test_cruce_de_anio():
    assert meses_a_cerrar(
        datetime(2025, 12, 5, tzinfo=timezone.utc), None, datetime(2026, 2, 1, tzinfo=timezone.utc)
    ) == [date(2025, 12, 1), date(2026, 1, 1)]
