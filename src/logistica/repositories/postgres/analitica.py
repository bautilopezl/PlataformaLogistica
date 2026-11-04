"""Repositorios de analítica (PostgreSQL): hechos para reportes, registro de reportes y predicciones de
riesgo.

Los reportes leen HECHOS (una fila por envío con sus tramos, una por incidente, una por alerta) y el dominio
los agrega (`domain/analitica.py`): cada número se puede recalcular a mano. SUPUESTO R3: con el volumen
académico se agrega en Python; con millones de envíos convendría mover los agregados a SQL (misma regla, otra
capa).
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.analitica import AlertaHecho, EnvioHecho, IncidenteHecho, Prediccion, TramoHecho
from logistica.repositories.postgres.envios import AlcanceConsulta


class HechosRepository:
    def envios(
        self, tx: Session, *, cliente_id: uuid.UUID | None = None, ids: list[uuid.UUID] | None = None
    ) -> list[EnvioHecho]:
        filtro = {"c": cliente_id, "ids": [str(i) for i in ids] if ids is not None else None}
        tramos: dict[uuid.UUID, list[TramoHecho]] = defaultdict(list)
        for r in tx.execute(
            text(
                """
                SELECT tr.envio_id, tr.orden, tr.origen_id, uo.nombre, tr.destino_id, ud.nombre, tr.medio,
                    tr.estado,
                       tr.salida_real_en, tr.llegada_real_en, tr.costo, tr.moneda_codigo
                FROM tramo tr
                JOIN envio e ON e.id = tr.envio_id
                JOIN ubicacion uo ON uo.id = tr.origen_id
                JOIN ubicacion ud ON ud.id = tr.destino_id
                WHERE (CAST(:c AS uuid) IS NULL OR e.cliente_id = :c)
                  AND (CAST(:ids AS uuid[]) IS NULL OR e.id = ANY(CAST(:ids AS uuid[])))
                ORDER BY tr.envio_id, tr.orden
                """
            ),
            filtro,
        ):
            tramos[r[0]].append(TramoHecho(*r))
        rows = tx.execute(
            text(
                """
                SELECT e.id, e.codigo, e.cliente_id, t.razon_social, e.estado, e.creado_en, e.entrega_real_en,
                       e.entrega_comprometida_en, e.volumen_m3, e.peso_kg, uo.pais_codigo, ud.pais_codigo,
                       (SELECT count(*) FROM incidente i WHERE i.envio_id = e.id),
                       (SELECT count(*) FROM incidente i WHERE i.envio_id = e.id AND i.estado <> 'RESUELTO')
                FROM envio e
                JOIN tercero t ON t.id = e.cliente_id
                JOIN ubicacion uo ON uo.id = e.origen_id
                JOIN ubicacion ud ON ud.id = e.destino_id
                WHERE (CAST(:c AS uuid) IS NULL OR e.cliente_id = :c)
                  AND (CAST(:ids AS uuid[]) IS NULL OR e.id = ANY(CAST(:ids AS uuid[])))
                ORDER BY e.creado_en, e.id
                """
            ),
            filtro,
        )
        return [EnvioHecho(*r, tramos=tuple(tramos.get(r[0], ()))) for r in rows]

    def incidentes(self, tx: Session, *, cliente_id: uuid.UUID | None = None) -> list[IncidenteHecho]:
        rows = tx.execute(
            text(
                """
                SELECT i.id, e.codigo, e.cliente_id, t.razon_social, i.tipo, i.severidad, i.estado,
                    i.ocurrido_en,
                       u.email
                FROM incidente i
                JOIN envio e ON e.id = i.envio_id
                JOIN tercero t ON t.id = e.cliente_id
                LEFT JOIN usuario u ON u.id = i.responsable_id
                WHERE (CAST(:c AS uuid) IS NULL OR e.cliente_id = :c)
                """
            ),
            {"c": cliente_id},
        )
        return [IncidenteHecho(*r) for r in rows]

    def alertas_temperatura(self, tx: Session, *, cliente_id: uuid.UUID | None = None) -> list[AlertaHecho]:
        rows = tx.execute(
            text(
                """
                SELECT e.codigo, e.cliente_id, s.codigo, a.severidad, a.estado, a.primera_deteccion_en,
                       a.ultima_deteccion_en, a.normalizada_en,
                       EXISTS (SELECT 1 FROM incidente_alerta ia WHERE ia.alerta_id = a.id), a.cerrada_en
                FROM alerta a
                JOIN envio e ON e.id = a.envio_id
                LEFT JOIN sensor s ON s.id = a.sensor_id
                WHERE a.tipo = 'TEMPERATURA' AND (CAST(:c AS uuid) IS NULL OR e.cliente_id = :c)
                """
            ),
            {"c": cliente_id},
        )
        return [AlertaHecho(*r) for r in rows]


# --------------------------------------------------------------------------- registro de reportes
@dataclass(frozen=True)
class ReporteDTO:
    id: uuid.UUID
    solicitado_por_id: uuid.UUID
    solicitado_por: str
    tipo: str
    parametros: dict[str, Any]
    formato: str
    estado: str
    solicitado_en: datetime
    finalizado_en: datetime | None
    archivo_clave: str | None
    error_codigo: str | None


_SELECT_REPORTE = """
    SELECT r.id, r.solicitado_por_id, u.email, r.tipo, r.parametros, r.formato, r.estado, r.solicitado_en,
           r.finalizado_en, r.archivo_clave, r.error_codigo
    FROM reporte r
    JOIN usuario u ON u.id = r.solicitado_por_id
"""


class ReporteRepository:
    def crear(
        self,
        tx: Session,
        reporte_id: uuid.UUID,
        usuario_id: uuid.UUID,
        tipo: str,
        parametros: dict[str, Any],
        formato: str,
    ) -> None:
        tx.execute(
            text(
                "INSERT INTO reporte (id, solicitado_por_id, tipo, parametros, formato, solicitado_en)"
                " VALUES (:id, :u, :t, CAST(:p AS jsonb), :f, clock_timestamp())"
            ),
            {"id": reporte_id, "u": usuario_id, "t": tipo, "p": json.dumps(parametros), "f": formato},
        )

    def iniciar(self, tx: Session, reporte_id: uuid.UUID) -> None:
        tx.execute(
            text(
                "UPDATE reporte SET estado = 'EN_PROCESO', iniciado_en = clock_timestamp()"
                " WHERE id = :id AND estado = 'PENDIENTE'"
            ),
            {"id": reporte_id},
        )

    def completar(self, tx: Session, reporte_id: uuid.UUID, archivo_clave: str) -> bool:
        return (
            tx.execute(
                text(
                    "UPDATE reporte SET estado = 'COMPLETADO', archivo_clave = :a, finalizado_en = "
                    "clock_timestamp()"
                    " WHERE id = :id AND estado = 'EN_PROCESO'"
                ),
                {"id": reporte_id, "a": archivo_clave},
            ).rowcount
            == 1
        )

    def fallar(self, tx: Session, reporte_id: uuid.UUID, error_codigo: str) -> None:
        tx.execute(
            text(
                "UPDATE reporte SET estado = 'ERROR', error_codigo = :e, finalizado_en = clock_timestamp()"
                " WHERE id = :id AND estado IN ('PENDIENTE', 'EN_PROCESO')"
            ),
            {"id": reporte_id, "e": error_codigo[:60]},
        )

    def obtener(self, tx: Session, reporte_id: uuid.UUID) -> ReporteDTO | None:
        r = tx.execute(text(_SELECT_REPORTE + " WHERE r.id = :id"), {"id": reporte_id}).one_or_none()
        return ReporteDTO(*r) if r else None

    def listar(self, tx: Session, *, usuario_id: uuid.UUID | None, limite: int = 200) -> list[ReporteDTO]:
        rows = tx.execute(
            text(
                _SELECT_REPORTE + " WHERE (CAST(:u AS uuid) IS NULL OR r.solicitado_por_id = :u)"
                " ORDER BY r.solicitado_en DESC, r.id LIMIT :l"
            ),
            {"u": usuario_id, "l": limite},
        )
        return [ReporteDTO(*r) for r in rows]


# --------------------------------------------------------------------------- predicciones
@dataclass(frozen=True)
class PrediccionDTO:
    envio_id: uuid.UUID
    envio_codigo: str
    estado_envio: str
    compromiso: datetime | None
    fecha_evaluacion: date
    probabilidad: Decimal | None
    riesgo_operativo: str
    riesgo_climatico: str
    metodo: str
    explicacion: str
    datos_entrada: dict[str, Any]
    calculado_en: datetime


class PrediccionRepository:
    def objetivos(self, tx: Session) -> list[uuid.UUID]:
        """Envíos a evaluar cada día: PLANIFICADOS o EN TRÁNSITO (§10.5)."""
        return list(
            tx.execute(
                text(
                    "SELECT id FROM envio WHERE estado IN ('PLANIFICADO', 'EN_TRANSITO')"
                    " ORDER BY creado_en, id"
                )
            ).scalars()
        )

    def guardar(
        self, tx: Session, envio_id: uuid.UUID, fecha: date, metodo: str, version: str, p: Prediccion
    ) -> bool:
        """Idempotente: la clave (envío, fecha, método, versión) es UNIQUE. False = ya estaba calculada
        hoy."""
        return (
            tx.execute(
                text(
                    "INSERT INTO prediccion_riesgo (id, envio_id, fecha_evaluacion, probabilidad_demora, "
                    "riesgo_operativo,"
                    " metodo_codigo, metodo_version, datos_entrada, explicacion, calculado_en)"
                    " VALUES (:id, :e, :f, :p, :r, :m, :v, CAST(:d AS jsonb), :x, clock_timestamp())"
                    " ON CONFLICT (envio_id, fecha_evaluacion, metodo_codigo, metodo_version) DO NOTHING"
                ),
                {
                    "id": uuid.uuid4(),
                    "e": envio_id,
                    "f": fecha,
                    "p": p.probabilidad,
                    "r": p.riesgo,
                    "m": metodo,
                    "v": version,
                    "d": json.dumps(p.datos_entrada),
                    "x": p.explicacion,
                },
            ).rowcount
            == 1
        )

    def vigentes(self, tx: Session, *, alcance: AlcanceConsulta) -> list[PrediccionDTO]:
        """La última predicción de cada envío del alcance (interno: todos; cliente: los suyos; operador: los
        envíos con algún tramo suyo)."""
        rows = tx.execute(
            text(
                """
                SELECT DISTINCT ON (p.envio_id) p.envio_id, e.codigo, e.estado, e.entrega_comprometida_en,
                       p.fecha_evaluacion, p.probabilidad_demora, p.riesgo_operativo, p.riesgo_climatico,
                       p.metodo_codigo || ' ' || p.metodo_version, p.explicacion, p.datos_entrada,
                           p.calculado_en
                FROM prediccion_riesgo p
                JOIN envio e ON e.id = p.envio_id
                WHERE (CAST(:cliente AS uuid) IS NULL AND CAST(:operador AS uuid) IS NULL)
                   OR e.cliente_id = :cliente
                   OR EXISTS (SELECT 1 FROM tramo t WHERE t.envio_id = e.id AND t.operador_id = :operador)
                ORDER BY p.envio_id, p.calculado_en DESC
                """
            ),
            {"cliente": alcance.cliente_id, "operador": alcance.operador_id},
        )
        return [PrediccionDTO(*r) for r in rows]
