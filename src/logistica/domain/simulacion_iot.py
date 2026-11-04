"""Guiones deterministas de demostración: segundos desde la salida, sin esperas ni I/O."""

from __future__ import annotations

from dataclasses import dataclass

from logistica.core.errors import ValidationError

ESCENARIOS = ("normal", "temperatura", "recuperacion", "desconexion", "completo")


@dataclass(frozen=True)
class PasoSimulacion:
    segundo: int
    temperatura: int | None  # None = silencio: solamente revisar comunicación


def pasos_escenario(nombre: str) -> list[PasoSimulacion]:
    if nombre not in ESCENARIOS:
        raise ValidationError(
            f"Escenario desconocido: {nombre}", user_message="Escenario de simulación inválido.")
    normales = [PasoSimulacion(s, 5) for s in (0, 60)]
    temperatura = [PasoSimulacion(s, 12) for s in range(120, 421, 60)]
    recuperacion = [PasoSimulacion(s, 5) for s in (480, 540, 600)]
    desconexion = [PasoSimulacion(781, None), PasoSimulacion(1201, None), PasoSimulacion(1260, 5)]
    return {
        "normal": normales,
        "temperatura": normales + temperatura,
        "recuperacion": normales + temperatura + recuperacion,
        "desconexion": normales + [
            PasoSimulacion(241, None), PasoSimulacion(661, None), PasoSimulacion(720, 5)],
        "completo": normales + temperatura + recuperacion + desconexion,
    }[nombre]
