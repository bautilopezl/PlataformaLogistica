"""Recolección de basura de Python solo en el hilo gráfico.

Problema: el recolector cíclico de Python corre en el hilo que esté reservando memoria en ese momento. Si
ese hilo es un trabajador del pool y la basura incluye un widget (los widgets suelen quedar en ciclos con
sus señales), el widget se destruye fuera del hilo gráfico y Qt aborta la aplicación. Se reprodujo en las
pruebas (ver tests/test_fase1_ui.py y tests/conftest.py).

Solución: se desactiva la recolección automática y un QTimer la ejecuta periódicamente en el hilo gráfico.
El conteo de referencias normal sigue liberando al instante todo lo que no forma ciclos.
"""

from __future__ import annotations

import gc

from PySide6.QtCore import QObject, QTimer

INTERVALO_MS = 1_000


class GarbageCollectorGuard(QObject):
    def __init__(self, parent: QObject | None = None, intervalo_ms: int = INTERVALO_MS):
        super().__init__(parent)
        gc.disable()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._recolectar)
        self._timer.start(intervalo_ms)

    def _recolectar(self) -> None:
        # Solo la generación joven en cada tick (barato); el resto cuando sus umbrales lo indican.
        umbral0, umbral1, umbral2 = gc.get_threshold()
        cuenta0, cuenta1, cuenta2 = gc.get_count()
        if cuenta0 > umbral0:
            generacion = 2 if cuenta2 > umbral2 else 1 if cuenta1 > umbral1 else 0
            gc.collect(generacion)

    def detener(self) -> None:
        self._timer.stop()
        gc.enable()
