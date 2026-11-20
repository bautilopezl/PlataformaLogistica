#!/usr/bin/env python3
"""Empaqueta la aplicación de escritorio con PyInstaller (AGENTS.md §15: "entregable" el ejecutable).

Uso (desde la raíz del proyecto, con el venv activado):
    python scripts/empaquetar.py             # build --onedir (carpeta dist/PlataformaLogistica/)
    python scripts/empaquetar.py --onefile   # un solo .exe (arranca más lento, más simple de repartir)

El ejecutable sigue necesitando PostgreSQL, MongoDB y Neo4j accesibles (por `docker compose up -d` o
servidores ya levantados) y un archivo `.env` junto al ejecutable (o en el directorio de trabajo) con las
credenciales: empaquetar la aplicación NO empaqueta las bases de datos ni reemplaza `docker-compose.yml`.

No se commitea el `.spec` generado (ver `.gitignore`): este script es la única fuente de verdad del build,
así no queda desactualizado respecto de los recursos reales del proyecto.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRADA = ROOT / "src" / "logistica" / "main.py"
RECURSOS_MAPA = ROOT / "src" / "logistica" / "ui" / "recursos"
SEPARADOR_ADD_DATA = ";" if sys.platform.startswith("win") else ":"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--onefile", action="store_true", help="un solo ejecutable (por defecto: carpeta)")
    parser.add_argument("--limpio", action="store_true", help="--clean de PyInstaller antes de compilar")
    args = parser.parse_args()

    if not RECURSOS_MAPA.exists():
        print(f"Falta {RECURSOS_MAPA} (mapa base del GPS); no se puede empaquetar.", file=sys.stderr)
        return 1

    comando = [
        sys.executable, "-m", "PyInstaller",
        "--name", "PlataformaLogistica",
        "--onefile" if args.onefile else "--onedir",
        "--windowed",
        "--paths", str(ROOT / "src"),
        "--add-data", f"{RECURSOS_MAPA}{SEPARADOR_ADD_DATA}logistica/ui/recursos",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if args.limpio:
        comando.append("--clean")
    comando.append(str(ENTRADA))

    print("Ejecutando:", " ".join(comando))
    resultado = subprocess.run(comando, cwd=ROOT)
    if resultado.returncode != 0:
        return resultado.returncode

    print()
    print("Build terminado. El ejecutable quedó en dist/PlataformaLogistica/")
    print("IMPORTANTE: copiá tu .env junto al ejecutable y levantá las tres bases "
          "(docker compose up -d) antes de abrirlo: empaquetar la app no empaqueta las bases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
