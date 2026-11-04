"""Procesa la outbox (MongoDB y Neo4j) o reconstruye el grafo, fuera de la aplicación.

Uso (desde la raíz del proyecto, con el venv activado):
    PYTHONPATH=src python scripts/sincronizar.py              # procesa lo pendiente una vez y termina
    PYTHONPATH=src python scripts/sincronizar.py --continuo   # en bucle cada --intervalo s (Ctrl+C corta)
    PYTHONPATH=src python scripts/sincronizar.py --reconstruir-grafo   # reproyecta todos los envíos en Neo4j
    PYTHONPATH=src python scripts/sincronizar.py --estado     # muestra la cola por destino y estado
    PYTHONPATH=src python scripts/sincronizar.py --reintentar-fallidas   # reencola las FALLIDAS

Usa la identidad de ejecución (PG_APP_USER), igual que la aplicación. Puede correr a la vez que la app: el
reclamo de entregas es atómico y dos trabajadores no procesan la misma.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logistica.core.config import load_config  # noqa: E402
from logistica.core.logging_setup import setup_logging  # noqa: E402
from logistica.repositories.connections import Connections  # noqa: E402
from logistica.services.autorizacion import Autorizador  # noqa: E402
from logistica.services.comercial_service import ComercialService  # noqa: E402
from logistica.services.sincronizacion_service import SincronizacionService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--continuo", action="store_true", help="procesar en bucle")
    grupo.add_argument("--reconstruir-grafo", action="store_true", help="reproyectar todos los envíos")
    grupo.add_argument("--estado", action="store_true", help="mostrar la cola")
    grupo.add_argument("--reintentar-fallidas", action="store_true", help="reencolar las FALLIDAS")
    parser.add_argument(
        "--intervalo", type=float, default=10.0, help="segundos entre vueltas (con --continuo)"
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except RuntimeError as exc:
        print(f"Configuración incompleta: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_dir, config.log_level)
    connections = Connections(config)
    # Incluye la facturación automática (fase 6): este proceso también emite las facturas de las entregas.
    comercial = ComercialService(connections, Autorizador(connections))
    servicio = SincronizacionService(connections, facturacion=comercial.facturar_entrega)
    try:
        if args.estado:
            _mostrar(servicio.resumen())
        elif args.reintentar_fallidas:
            print(f"Reencoladas: {servicio.reintentar_fallidas()}")
        elif args.reconstruir_grafo:
            print(f"Envíos proyectados: {servicio.reconstruir_grafo()}")
        else:
            while True:
                for destino, r in servicio.procesar_todo().items():
                    if r.procesados or r.fallidos or r.omitidos or r.pospuestos:
                        print(
                            f"{destino}: {r.procesados} procesados, {r.fallidos} fallidos, {r.omitidos} "
                            f"omitidos, {r.pospuestos} pospuestos (destino caído)"
                        )
                if not args.continuo:
                    break
                time.sleep(args.intervalo)
    except KeyboardInterrupt:
        pass
    finally:
        connections.close()
    return 0


def _mostrar(resumen: dict[str, dict[str, int]]) -> None:
    for destino, estados in resumen.items():
        print(f"{destino}: " + ", ".join(f"{estado.lower()} {n}" for estado, n in estados.items()))


if __name__ == "__main__":
    sys.exit(main())
