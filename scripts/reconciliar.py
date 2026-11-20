#!/usr/bin/env python3
"""Reconciliación de diagnóstico entre PostgreSQL/MongoDB/Neo4j y de integridad comercial (AGENTS.md §13/§14).

Uso (desde la raíz del proyecto, con el venv activado):
    PYTHONPATH=src python scripts/reconciliar.py

Solo lee y compara; nunca repara, nunca borra ni reconstruye nada (para eso existen
`scripts/sincronizar.py --reintentar-fallidas` / `--reconstruir-grafo`, y las acciones manuales de un
usuario con permiso sobre pagos/facturas). Usa la identidad de ejecución (PG_APP_USER), igual que la
aplicación; corre también periódicamente dentro de la ventana (planificador de la fase 8, tarea
RECONCILIACION, una vez al día) y con `scripts/procesos.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logistica.core.config import load_config  # noqa: E402
from logistica.core.logging_setup import setup_logging  # noqa: E402
from logistica.repositories.connections import Connections  # noqa: E402
from logistica.services.reconciliacion_service import ReconciliacionService  # noqa: E402


def main() -> int:
    try:
        config = load_config()
    except RuntimeError as exc:
        print(f"Configuración incompleta: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_dir, config.log_level)
    connections = Connections(config)
    try:
        resultado = ReconciliacionService(connections).ejecutar()
    finally:
        connections.close()
    print(resultado.resumen_texto())
    return 1 if (resultado.discrepancias or resultado.entregas_fallidas) else 0


if __name__ == "__main__":
    sys.exit(main())
