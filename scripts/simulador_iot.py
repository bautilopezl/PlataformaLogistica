"""Simulador IoT por servicios, sin API. Solo admite viajes DEMO-MON independientes.

--preparar-demo crea un viaje y reproduce 21 minutos con reloj SIMULADOR explícito (sin esperas).
--envio-id reenvía un escenario del viaje; los UUID son estables. Sin --reloj-simulado respeta el reloj real:
si la medición ya pasó, se recibe tarde y queda histórica según las reglas normales.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logistica.core.config import load_config  # noqa: E402
from logistica.core.errors import LogisticaError  # noqa: E402
from logistica.core.logging_setup import setup_logging  # noqa: E402
from logistica.domain.simulacion_iot import ESCENARIOS, pasos_escenario  # noqa: E402
from logistica.repositories.connections import Connections  # noqa: E402
from logistica.services.autorizacion import Autorizador  # noqa: E402
from logistica.services.contenedor import crear_servicios  # noqa: E402
from logistica.services.demo_monitoreo_service import reproducir_demo  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--preparar-demo", action="store_true", help="crear demo NUEVA con reloj simulado")
    grupo.add_argument("--envio-id", type=uuid.UUID, help="reenviar sobre una demo existente en tránsito")
    parser.add_argument("--email", required=True, help="usuario de la aplicación (la clave se pide sin eco)")
    parser.add_argument("--escenario", choices=ESCENARIOS, default="completo")
    parser.add_argument("--reloj-simulado", action="store_true",
                        help="recepción SIMULADOR, sin esperas reales")
    parser.add_argument("--continuo", action="store_true",
                        help="episodios en tiempo real desde ahora, cada lectura a 60 s; Ctrl+C detiene")
    parser.add_argument(
        "--inicio", type=datetime.fromisoformat,
        help="inicio ISO con zona para reenviar un episodio específico (se informa al empezar)")
    args = parser.parse_args()
    if args.continuo and (args.reloj_simulado or args.preparar_demo):
        parser.error("--continuo requiere --envio-id y reloj real (sin --reloj-simulado)")
    if args.inicio is not None and args.inicio.tzinfo is None:
        parser.error("--inicio debe incluir zona horaria")
    config = load_config()
    setup_logging(config.log_dir, config.log_level)
    conexiones = Connections(config)
    servicios = crear_servicios(config, conexiones, uuid.uuid4())
    principal = None
    detener = Event()
    try:
        principal = servicios.acceso.login(args.email, getpass.getpass("Contraseña: "), "Simulador IoT")
        if args.preparar_demo:
            envio_id = servicios.monitoreo.preparar_demo(principal.sesion_id, args.escenario)
            print(f"Demo SIMULADOR creada. Seleccioná el envío {envio_id} en Monitoreo.")
        else:
            inicio = args.inicio
            if args.continuo and inicio is None:
                inicio = datetime.now(timezone.utc).replace(microsecond=0)
            while not detener.is_set():
                if inicio is not None:
                    print(f"Inicio de episodio: {inicio.isoformat()} (conservalo para reenviarlo)")
                nuevas = reproducir_demo(
                    conexiones, Autorizador(conexiones), principal.sesion_id, args.envio_id, args.escenario,
                    reloj_simulado=args.reloj_simulado, detener=detener,
                    inicio_escenario=inicio, informar=print)
                print(f"Escenario terminado: {nuevas} lecturas nuevas; los reenvíos no se duplican.")
                if not args.continuo:
                    break
                inicio += timedelta(seconds=pasos_escenario(args.escenario)[-1].segundo + 60)
    except KeyboardInterrupt:
        detener.set()
        print("Simulación interrumpida; se conservan los pasos confirmados.")
        return 130
    except LogisticaError as exc:
        logging.getLogger(__name__).exception("Falló el simulador")
        print(exc.user_message, file=sys.stderr)
        return 1
    finally:
        try:
            if principal is not None:
                servicios.acceso.logout(principal.sesion_id)
        finally:
            conexiones.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
