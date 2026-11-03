"""Pestaña "Estado": salud de los tres motores, verificada sin bloquear el hilo gráfico."""

from __future__ import annotations

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QFormLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from logistica.services.health_service import EngineStatus, HealthService
from logistica.ui.worker import WorkerPool

ENGINES = ("PostgreSQL", "MongoDB", "Neo4j")
# HealthService ya se corta solo a DB_TIMEOUT_S + 1 s; esto es una red de seguridad para el cierre.
SHUTDOWN_WAIT_MS = 10_000


class EstadoView(QWidget):
    def __init__(self, health_service: HealthService, parent: QWidget | None = None):
        super().__init__(parent)
        self._service = health_service
        self._pool = WorkerPool(self)
        self._busy = False
        self._closing = False

        titulo = QLabel("Estado de las bases de datos")
        titulo.setObjectName("titulo")
        self._labels: dict[str, QLabel] = {}
        form = QFormLayout()
        for engine in ENGINES:
            label = QLabel("Sin verificar")
            _set_state(label, "pendiente")
            self._labels[engine] = label
            form.addRow(f"{engine}:", label)
        self._button = QPushButton("Verificar conexiones")
        self._button.clicked.connect(self.refresh)

        layout = QVBoxLayout(self)
        layout.addWidget(titulo)
        layout.addLayout(form)
        layout.addStretch()
        layout.addWidget(self._button)

    def refresh(self) -> None:
        if self._busy or self._closing:
            return
        self._busy = True
        self._button.setEnabled(False)
        for label in self._labels.values():
            label.setText("Verificando…")
            _set_state(label, "pendiente")
        self._pool.submit(self._service.check_all, self._on_result, self._on_error)

    def is_busy(self) -> bool:
        return self._busy

    def engine_text(self, engine: str) -> str:
        return self._labels[engine].text()

    def cerrar(self) -> None:
        """No acepta trabajo nuevo y espera al chequeo en curso."""
        self._closing = True
        self._button.setEnabled(False)
        self._pool.wait_for_done(SHUTDOWN_WAIT_MS)
        self._busy = False

    @Slot(object)
    def _on_result(self, statuses: list[EngineStatus]) -> None:
        for status in statuses:
            label = self._labels[status.engine]
            if status.ok:
                label.setText(f"OK ({status.latency_ms} ms)")
                _set_state(label, "ok")
            else:
                label.setText(f"Sin conexión: {status.detail}")
                _set_state(label, "error")
        self._finish()

    @Slot(str)
    def _on_error(self, message: str) -> None:
        for label in self._labels.values():
            label.setText(message)
            _set_state(label, "error")
        self._finish()

    def _finish(self) -> None:
        self._busy = False
        self._button.setEnabled(not self._closing)


def _set_state(label: QLabel, state: str) -> None:
    label.setProperty("estado", state)
    label.style().unpolish(label)  # reaplica la hoja de estilos con la nueva propiedad
    label.style().polish(label)
