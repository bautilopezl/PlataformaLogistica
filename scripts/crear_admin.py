"""Crea el primer administrador del sistema (solo si no existe ninguno activo).

Uso (desde la raíz del proyecto, con el venv activado):
    PYTHONPATH=src python scripts/crear_admin.py --email admin@empresa.com --nombre Ana --apellido Pérez

La contraseña se pide por teclado (no se muestra ni queda en el historial de la terminal). Usa la identidad
de ejecución (PG_APP_USER), igual que la aplicación.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logistica.core.config import load_config  # noqa: E402
from logistica.core.errors import LogisticaError  # noqa: E402
from logistica.domain.acceso import UsuarioDatos  # noqa: E402
from logistica.domain.passwords import Hasher  # noqa: E402
from logistica.repositories.connections import Connections  # noqa: E402
from logistica.services.autorizacion import Autorizador  # noqa: E402
from logistica.services.usuario_service import UsuarioService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--nombre", required=True)
    parser.add_argument("--apellido", required=True)
    args = parser.parse_args()

    password = getpass.getpass("Contraseña del administrador: ")
    if password != getpass.getpass("Repetir contraseña: "):
        print("Las contraseñas no coinciden.", file=sys.stderr)
        return 1

    try:
        config = load_config()
    except RuntimeError as exc:
        print(f"Configuración incompleta: {exc}", file=sys.stderr)
        return 2
    s = config.security
    connections = Connections(config)
    try:
        servicio = UsuarioService(
            connections,
            Hasher(s.argon2_time_cost, s.argon2_memory_kib, s.argon2_parallelism),
            Autorizador(connections),
        )
        usuario = servicio.crear_administrador_inicial(
            UsuarioDatos(args.nombre, args.apellido, args.email), password
        )
    except LogisticaError as exc:
        print(f"No se creó el administrador: {exc.user_message}", file=sys.stderr)
        return 1
    finally:
        connections.close()
    print(f"Administrador creado: {usuario.email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
