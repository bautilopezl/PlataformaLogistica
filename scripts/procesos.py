"""Ejecuta los procesos periódicos (fase 8) fuera de la aplicación: facturación de entregas, sensores sin
comunicación, cierre mensual de cuentas y predicción diaria de atrasos.

Uso (desde la raíz del proyecto, con el venv activado):
    PYTHONPATH=src python scripts/procesos.py              # una vuelta (revisa todo lo pendiente) y termina
    PYTHONPATH=src python scripts/procesos.py --continuo   # en bucle cada --intervalo s (Ctrl+C corta)

Usa la identidad de ejecución (PG_APP_USER), igual que la aplicación, y puede correr a la vez que una o varias
instancias abiertas: cada tarea toma un advisory lock por tarea y período, y además es idempotente.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logistica.core.config import load_config  # noqa: E402
from logistica.core.logging_setup import setup_logging  # noqa: E402
from logistica.repositories.connections import Connections  # noqa: E402
from logistica.services.contenedor import crear_servicios  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--continuo", action="store_true", help="procesar en bucle")
    parser.add_argument(
        "--intervalo", type=float, default=60.0, help="segundos entre vueltas (con --continuo)"
    )
    args = parser.parse_args()
    try:
        config = load_config()
    except RuntimeError as exc:
        print(f"Configuración incompleta: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_dir, config.log_level)
    connections = Connections(config)
    planificador = crear_servicios(config, connections, uuid.uuid4()).procesos
    try:
        while True:
            for r in planificador.ejecutar_pendientes():
                if r.estado != "YA_HECHA":
                    print(f"{r.tarea.value} ({r.periodo}): {r.estado} {r.detalle}".rstrip())
            if not args.continuo:
                break
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        pass
    finally:
        connections.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
