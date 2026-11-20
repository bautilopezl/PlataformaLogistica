"""Reconciliación entre motores y de integridad comercial (AGENTS.md §13 y §14).

Solo DIAGNOSTICA: nunca repara, nunca borra, nunca reconstruye nada global. Cada corrida produce un
`ResultadoReconciliacion` con lo que encontró; quien lo lee decide qué hacer (reintentar una entrega
fallida ya es cosa de `scripts/sincronizar.py --reintentar-fallidas`, anular un pago mal aplicado es cosa
de un usuario con permiso). Es el mismo espíritu que la outbox: visibilidad primero, nunca "arreglos"
automáticos que finjan una atomicidad que el sistema no tiene (AGENTS.md §9).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Discrepancia:
    """Un hallazgo puntual: categoría amplia (para agrupar en el reporte), qué entidad y el detalle."""

    categoria: str  # "NEO4J" | "MONGO" | "PAGO_SOBREAPLICADO" | "FACTURA_SOBREAPLICADA" | "CIERRE_INCONSISTENTE"
    entidad: str  # "envio" | "pago" | "factura" | "cierre_cuenta"
    entidad_id: str
    detalle: str


@dataclass(frozen=True)
class EntregaFallida:
    """Una entrega de la outbox que agotó sus reintentos: ya la señala la fase 3, acá se la reporta de
    nuevo porque reconciliación es, justamente, el lugar donde se espera verla."""

    evento_id: uuid.UUID
    agregado_id: uuid.UUID
    destino: str
    evento_tipo: str
    intentos: int


@dataclass(frozen=True)
class ResultadoReconciliacion:
    ejecutado_en: datetime
    motores_disponibles: dict[str, bool]  # {"MONGO": True, "NEO4J": False}
    resumen_outbox: dict[str, dict[str, int]]  # {destino: {estado: cantidad}}
    entregas_fallidas: list[EntregaFallida] = field(default_factory=list)
    discrepancias: list[Discrepancia] = field(default_factory=list)
    envios_revisados: int = 0
    cuentas_revisadas: int = 0

    @property
    def sin_hallazgos(self) -> bool:
        return not self.entregas_fallidas and not self.discrepancias

    def resumen_texto(self) -> str:
        """Una línea por hallazgo, pensada para loguear o imprimir (scripts/reconciliar.py)."""
        lineas = [
            f"Reconciliación {self.ejecutado_en.isoformat()}: {self.envios_revisados} envío(s) y "
            f"{self.cuentas_revisadas} cuenta(s) revisadas."
        ]
        for motor, disponible in self.motores_disponibles.items():
            if not disponible:
                lineas.append(f"  {motor}: NO DISPONIBLE en esta corrida (se omitieron sus controles).")
        for destino, estados in self.resumen_outbox.items():
            lineas.append(f"  outbox {destino}: " + ", ".join(f"{e}={n}" for e, n in estados.items()))
        for f in self.entregas_fallidas:
            lineas.append(
                f"  FALLIDA: evento {f.evento_tipo} del envío {f.agregado_id} hacia {f.destino} "
                f"({f.intentos} intentos)"
            )
        for d in self.discrepancias:
            lineas.append(f"  [{d.categoria}] {d.entidad} {d.entidad_id}: {d.detalle}")
        if self.sin_hallazgos:
            lineas.append("  Sin discrepancias ni entregas fallidas.")
        return "\n".join(lineas)
