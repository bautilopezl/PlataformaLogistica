"""Qué se proyecta de un envío en el grafo (función pura: de los DTO de PostgreSQL a parámetros de Cypher).

El grafo guarda ids, el contexto necesario para las consultas de red y la versión del agregado; no decide
permisos, saldos ni estados (la fuente principal es PostgreSQL, AGENTS.md §7 y §8.3). Los valores se pasan
como texto/números simples: Neo4j no tiene UUID ni Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from logistica.domain.logistica_dtos import AsignacionDTO, EnvioDTO, TramoDTO, UbicacionDTO


@dataclass(frozen=True)
class EnvioGrafo:
    envio: dict[str, Any]
    cliente: dict[str, Any]
    ubicaciones: list[dict[str, Any]] = field(default_factory=list)
    tramos: list[dict[str, Any]] = field(default_factory=list)
    siguientes: list[list[str]] = field(default_factory=list)  # pares [tramo_id, siguiente_id]
    usos: list[dict[str, Any]] = field(default_factory=list)


def armar_envio_grafo(
    envio: EnvioDTO,
    tramos: list[TramoDTO],
    asignaciones: list[AsignacionDTO],
    ubicaciones: dict[Any, UbicacionDTO],
) -> EnvioGrafo:
    """Arma la proyección de un envío. Las asignaciones canceladas no se proyectan (no son un uso)."""
    ordenados = sorted(tramos, key=lambda t: t.orden)
    return EnvioGrafo(
        envio={
            "id": str(envio.id),
            "codigo": envio.codigo,
            "estado": envio.estado,
            "prioridad": envio.prioridad,
            "version": envio.version,
            "cliente_id": str(envio.cliente_id),
            "origen_id": str(envio.origen_id),
            "destino_id": str(envio.destino_id),
            "peso_kg": float(envio.peso_kg),
            "volumen_m3": float(envio.volumen_m3),
            "creado_en": _iso(envio.creado_en),
            "entrega_comprometida_en": _iso(envio.entrega_comprometida_en),
            "entrega_real_en": _iso(envio.entrega_real_en),
        },
        cliente={"id": str(envio.cliente_id), "razon_social": envio.cliente_razon_social},
        ubicaciones=[
            {"id": str(u.id), "nombre": u.nombre, "pais": u.pais_codigo, "ciudad": u.ciudad}
            for u in ubicaciones.values()
        ],
        tramos=[
            {
                "id": str(t.id),
                "origen_id": str(t.origen_id),
                "destino_id": str(t.destino_id),
                "operador_id": str(t.operador_id),
                "operador_razon_social": t.operador_razon_social,
                "propiedades": {
                    "orden": t.orden,
                    "medio": t.medio,
                    "estado": t.estado,
                    "vehiculo_id": str(t.vehiculo_id) if t.vehiculo_id else None,
                    "vehiculo": t.vehiculo_identificacion,
                    "salida_estimada_en": _iso(t.salida_estimada_en),
                    "llegada_estimada_en": _iso(t.llegada_estimada_en),
                    "salida_real_en": _iso(t.salida_real_en),
                    "llegada_real_en": _iso(t.llegada_real_en),
                },
            }
            for t in ordenados
        ],
        siguientes=[[str(a.id), str(b.id)] for a, b in zip(ordenados, ordenados[1:])],
        usos=[
            {
                "id": str(a.id),
                "contenedor_id": str(a.contenedor_id),
                "codigo": a.contenedor_codigo,
                "estado": a.estado,
                "desde": _iso(a.uso_desde or a.reserva_desde),
                "hasta": _iso(a.uso_hasta if a.uso_desde else a.reserva_hasta),
            }
            for a in asignaciones
            if a.estado != "CANCELADA"
        ],
    )


def _iso(valor: datetime | None) -> str | None:
    return valor.isoformat() if valor is not None else None
