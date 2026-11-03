"""Ejecución de servicios fuera del hilo gráfico.

Patrón para toda la UI: el widget usa un `WorkerPool` y llama a `pool.submit(funcion_del_servicio, on_ok,
on_error)`. El trabajador nunca toca widgets; devuelve el resultado o el mensaje de error por señales, que Qt
entrega en el hilo gráfico. `on_ok`/`on_error` deben ser métodos de un QObject (no lambdas), para que Qt los
ejecute en el hilo del widget.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from logistica.core.errors import LogisticaError, SessionExpiredError

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    finished = Signal(object)  # resultado de la función
    failed = Signal(str)  # mensaje apto para mostrar al usuario
    session_expired = Signal(str)  # la sesión ya no sirve: la UI vuelve al login


class Worker(QRunnable):
    """Tarea para un `QThreadPool`. Qt la destruye sola al terminar `run()` (autoDelete, por defecto).

    Las señales NO viven en el `Worker`: las crea y las posee el `WorkerPool` (un QObject del hilo gráfico),
    así siguen existiendo después de que Qt destruya la tarea y se liberan en su propio hilo.
    """

    def __init__(self, fn: Callable[..., Any], signals: WorkerSignals, *args: Any, **kwargs: Any):
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs
        self.signals = signals

    @Slot()
    def run(self) -> None:
        # Se sueltan las referencias del trabajador ANTES de emitir: la función suele ser una closure o un
        # método que retiene a la vista. El `WorkerPool` guarda su propia referencia en el hilo gráfico, así
        # que
        # la última referencia (y la posible destrucción del widget) nunca ocurre en este hilo: Qt aborta si
        # un QWidget se destruye fuera del hilo gráfico.
        fn, args, kwargs = self._fn, self._args, self._kwargs
        self._fn = self._args = self._kwargs = None
        try:
            resultado = ("finished", fn(*args, **kwargs))
        except SessionExpiredError as exc:
            log.info("Sesión no vigente: %s", exc)
            resultado = ("session_expired", exc.user_message)
        except LogisticaError as exc:
            log.warning("Operación rechazada: %s", exc)
            resultado = ("failed", exc.user_message)
        except Exception:  # un trabajador nunca debe propagar al hilo gráfico
            log.exception("Error inesperado en un trabajador")
            resultado = ("failed", "Ocurrió un error inesperado. Ver logs.")
        del fn, args, kwargs
        senal, valor = resultado
        getattr(self.signals, senal).emit(valor)


class WorkerPool(QObject):
    """Pool de trabajadores de un widget.

    Reparto de responsabilidades sobre la memoria (importante, ver AGENTS.md §6):
    - El `Worker` (QRunnable) lo destruye Qt en el hilo del pool cuando termina `run()`. No se destruye desde
      Python: hacerlo apenas llega la señal provocaba un acceso a memoria liberada, porque Qt todavía usa la
      tarea después de `run()`.
    - Las `WorkerSignals` son hijas de este pool (hilo gráfico) y se liberan con `deleteLater` cuando ya
      entregaron su resultado. Así nunca se destruyen desde el hilo trabajador.
    - La función y sus argumentos se retienen acá hasta que llega el resultado, y se sueltan en el hilo
      gráfico (pueden ser closures que retienen widgets).
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        # Por cada tarea en curso: sus señales y una referencia a la función y argumentos (ver Worker.run).
        self._active: dict[WorkerSignals, tuple[Any, ...]] = {}

    def submit(
        self,
        fn: Callable[..., Any],
        on_ok: Callable[[Any], None],
        on_error: Callable[[str], None],
        *args: Any,
        on_session_expired: Callable[[str], None] | None = None,
        retener: Any = None,
    ) -> None:
        """Ejecuta `fn(*args)` en otro hilo. Sin `on_session_expired`, una sesión vencida va a `on_error`.

        `retener`: objeto que debe vivir hasta que llegue el resultado (por ejemplo, el receptor de las
        señales);
        se suelta en el hilo gráfico junto con la función.
        """
        signals = WorkerSignals(self)
        signals.finished.connect(on_ok)
        signals.failed.connect(on_error)
        signals.session_expired.connect(on_session_expired or on_error)
        # Slots de este QObject (no lambdas): Qt los corre en el hilo gráfico, después de los del widget.
        for signal in (signals.finished, signals.failed, signals.session_expired):
            signal.connect(self._release)
        self._active[signals] = (fn, args, retener)
        self._pool.start(Worker(fn, signals, *args))

    def active_count(self) -> int:
        return len(self._active)

    @Slot(object)
    def _release(self, payload: object) -> None:  # noqa: ARG002 - el valor lo consume el widget
        del payload
        signals = self.sender()
        if signals in self._active:
            del self._active[signals]  # última referencia a fn/args: se suelta en el hilo gráfico
            signals.deleteLater()

    def wait_for_done(self, msecs: int) -> bool:
        return self._pool.waitForDone(msecs)
