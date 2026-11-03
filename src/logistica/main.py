"""Punto de entrada de la aplicación de escritorio.

Ejecutar desde la raíz del proyecto:  python -m logistica.main   (con PYTHONPATH=src)
"""

from __future__ import annotations

import logging
import platform
import sys
import uuid

from PySide6.QtWidgets import QApplication, QMessageBox

from logistica.core.config import load_config
from logistica.core.logging_setup import setup_logging
from logistica.repositories.connections import Connections
from logistica.services.contenedor import crear_servicios
from logistica.ui.app_controller import AppController
from logistica.ui.gc_guard import GarbageCollectorGuard
from logistica.ui.theme import apply_theme

log = logging.getLogger("logistica")


def main() -> int:
    try:
        config = load_config()
    except RuntimeError as exc:
        print(f"Configuración incompleta: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_dir, config.log_level)
    instancia_id = uuid.uuid4()  # identifica esta ejecución en las sesiones (no es credencial)
    log.info("Iniciando aplicación (instancia %s)", instancia_id)

    app = QApplication(sys.argv)
    apply_theme(app)
    gc_guard = GarbageCollectorGuard(app)  # la basura cíclica se libera solo en el hilo gráfico
    app.aboutToQuit.connect(gc_guard.detener)
    connections = Connections(config)
    app.aboutToQuit.connect(connections.close)

    def excepthook(exc_type, exc, tb):
        log.error("Excepción no controlada", exc_info=(exc_type, exc, tb))
        QMessageBox.critical(None, "Error", "Ocurrió un error inesperado. Ver logs.")

    sys.excepthook = excepthook

    controller = AppController(
        app,
        crear_servicios(config, connections, instancia_id),
        cliente_descripcion=f"Escritorio {platform.node()}",
    )
    if not controller.iniciar():
        connections.close()
        return 0
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
