"""Consultas PostgreSQL para validar alcance y aportar datos operativos a trazabilidad."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.logistica_dtos import ContenedorDTO
from logistica.domain.trazabilidad_dtos import AsignacionContenedorBaseDTO
from logistica.repositories.postgres.envios import AlcanceConsulta


class TrazabilidadRepository:
    def contenedor_por_codigo(self, tx: Session, codigo: str) -> ContenedorDTO | None:
        fila = tx.execute(
            text(
                "SELECT id, codigo_internacional, tipo, capacidad_kg, capacidad_m3, estado"
                " FROM contenedor WHERE codigo_internacional = :codigo"
            ),
            {"codigo": codigo},
        ).one_or_none()
        return ContenedorDTO(*fila) if fila else None

    def asignaciones_contenedor(
        self,
        tx: Session,
        contenedor_id: uuid.UUID,
        *,
        alcance: AlcanceConsulta,
        limite: int,
    ) -> list[AsignacionContenedorBaseDTO]:
        filas = tx.execute(
            text(
                """
                SELECT a.id, a.envio_id, e.codigo, e.cliente_id, c.razon_social, a.estado,
                       a.reserva_desde, a.reserva_hasta, a.uso_desde, a.uso_hasta
                FROM envio_contenedor a
                JOIN envio e ON e.id = a.envio_id
                JOIN tercero c ON c.id = e.cliente_id
                WHERE a.contenedor_id = :contenedor
                  AND (CAST(:cliente AS uuid) IS NULL OR e.cliente_id = :cliente)
                  AND (
                    CAST(:operador AS uuid) IS NULL
                    OR EXISTS (
                      SELECT 1 FROM tramo t
                      WHERE t.envio_id = e.id AND t.operador_id = :operador
                    )
                  )
                ORDER BY a.reserva_desde, a.id
                LIMIT :limite
                """
            ),
            {
                "contenedor": contenedor_id,
                "cliente": alcance.cliente_id,
                "operador": alcance.operador_id,
                "limite": limite,
            },
        )
        return [AsignacionContenedorBaseDTO(*fila) for fila in filas]

    def asignacion_contenedor(
        self,
        tx: Session,
        contenedor_id: uuid.UUID,
        asignacion_id: uuid.UUID,
        *,
        alcance: AlcanceConsulta,
    ) -> AsignacionContenedorBaseDTO | None:
        fila = tx.execute(
            text(
                """
                SELECT a.id, a.envio_id, e.codigo, e.cliente_id, c.razon_social, a.estado,
                       a.reserva_desde, a.reserva_hasta, a.uso_desde, a.uso_hasta
                FROM envio_contenedor a
                JOIN envio e ON e.id = a.envio_id
                JOIN tercero c ON c.id = e.cliente_id
                WHERE a.contenedor_id = :contenedor AND a.id = :asignacion
                  AND (CAST(:cliente AS uuid) IS NULL OR e.cliente_id = :cliente)
                  AND (
                    CAST(:operador AS uuid) IS NULL
                    OR EXISTS (
                      SELECT 1 FROM tramo t
                      WHERE t.envio_id = e.id AND t.operador_id = :operador
                    )
                  )
                """
            ),
            {
                "contenedor": contenedor_id,
                "asignacion": asignacion_id,
                "cliente": alcance.cliente_id,
                "operador": alcance.operador_id,
            },
        ).one_or_none()
        return AsignacionContenedorBaseDTO(*fila) if fila else None

    def envios_para_red(
        self,
        tx: Session,
        *,
        alcance: AlcanceConsulta,
        cliente_filtro: uuid.UUID | None,
        operador_filtro: uuid.UUID | None,
        envio_filtro: uuid.UUID | None,
        limite: int,
    ) -> list[uuid.UUID]:
        filas = tx.execute(
            text(
                """
                SELECT e.id
                FROM envio e
                WHERE (CAST(:alcance_cliente AS uuid) IS NULL OR e.cliente_id = :alcance_cliente)
                  AND (
                    CAST(:alcance_operador AS uuid) IS NULL
                    OR EXISTS (
                      SELECT 1 FROM tramo t
                      WHERE t.envio_id = e.id AND t.operador_id = :alcance_operador
                    )
                  )
                  AND (CAST(:cliente AS uuid) IS NULL OR e.cliente_id = :cliente)
                  AND (
                    CAST(:operador AS uuid) IS NULL
                    OR EXISTS (
                      SELECT 1 FROM tramo t
                      WHERE t.envio_id = e.id AND t.operador_id = :operador
                    )
                  )
                  AND (CAST(:envio AS uuid) IS NULL OR e.id = :envio)
                ORDER BY e.creado_en, e.id
                LIMIT :limite
                """
            ),
            {
                "alcance_cliente": alcance.cliente_id,
                "alcance_operador": alcance.operador_id,
                "cliente": cliente_filtro,
                "operador": operador_filtro,
                "envio": envio_filtro,
                "limite": limite,
            },
        )
        return list(filas.scalars())

    def versiones(self, tx: Session, envio_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not envio_ids:
            return {}
        filas = tx.execute(
            text("SELECT id, version FROM envio WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": envio_ids},
        )
        return {fila.id: fila.version for fila in filas}

    def nombres_catalogo(
        self,
        tx: Session,
        ubicacion_ids: list[uuid.UUID],
        tercero_ids: list[uuid.UUID],
    ) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
        """Obtiene nombres vigentes por ID; los nombres guardados en Neo4j pueden estar desactualizados."""
        ubicaciones: dict[uuid.UUID, str] = {}
        terceros: dict[uuid.UUID, str] = {}
        if ubicacion_ids:
            filas = tx.execute(
                text("SELECT id, nombre FROM ubicacion WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": ubicacion_ids},
            )
            ubicaciones = {fila.id: fila.nombre for fila in filas}
        if tercero_ids:
            filas = tx.execute(
                text("SELECT id, razon_social FROM tercero WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": tercero_ids},
            )
            terceros = {fila.id: fila.razon_social for fila in filas}
        return ubicaciones, terceros

    def operadores_por_envios(
        self, tx: Session, envio_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, set[uuid.UUID]]:
        if not envio_ids:
            return {}
        filas = tx.execute(
            text(
                "SELECT envio_id, operador_id FROM tramo"
                " WHERE envio_id = ANY(CAST(:ids AS uuid[])) ORDER BY envio_id, orden"
            ),
            {"ids": envio_ids},
        )
        resultado: dict[uuid.UUID, set[uuid.UUID]] = {}
        for fila in filas:
            resultado.setdefault(fila.envio_id, set()).add(fila.operador_id)
        return resultado

    def clientes_por_envios(
        self, tx: Session, envio_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        if not envio_ids:
            return {}
        filas = tx.execute(
            text("SELECT id, cliente_id FROM envio WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": envio_ids},
        )
        return {fila.id: fila.cliente_id for fila in filas}
