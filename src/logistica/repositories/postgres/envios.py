"""Repositorios de envíos, tramos y asignaciones de contenedores (fuente principal: PostgreSQL)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.logistica import AsignacionDatos, EnvioDatos, TramoPlan
from logistica.domain.logistica_dtos import AsignacionDTO, EnvioDTO, TramoDTO


@dataclass(frozen=True)
class AlcanceConsulta:
    """Filtro de alcance para consultas: None = sin restricción (interno)."""

    cliente_id: uuid.UUID | None = None
    operador_id: uuid.UUID | None = None


_SELECT_ENVIO = """
    SELECT e.id, e.codigo, e.cliente_id, c.razon_social, e.origen_id, o.nombre, e.destino_id, d.nombre,
           e.descripcion_carga, e.estado, e.prioridad, e.peso_kg, e.volumen_m3, e.creado_en,
           e.entrega_estimada_en, e.entrega_comprometida_en, e.entrega_real_en, e.version
    FROM envio e
    JOIN tercero c ON c.id = e.cliente_id
    JOIN ubicacion o ON o.id = e.origen_id
    JOIN ubicacion d ON d.id = e.destino_id
"""
# Interno (ambos NULL): todo. Externo: los envíos de los que es cliente O en los que opera algún tramo.
_ALCANCE = """
    ((CAST(:cliente AS uuid) IS NULL AND CAST(:operador AS uuid) IS NULL)
     OR e.cliente_id = :cliente
     OR EXISTS (SELECT 1 FROM tramo x WHERE x.envio_id = e.id AND x.operador_id = :operador))
"""


class EnvioRepository:
    def crear(
        self, tx: Session, envio_id: uuid.UUID, codigo: str, d: EnvioDatos, usuario_id: uuid.UUID
    ) -> None:
        tx.execute(
            text(
                "INSERT INTO envio (id, codigo, cliente_id, origen_id, destino_id, descripcion_carga, "
                "prioridad,"
                " peso_kg, volumen_m3, creado_por_id) VALUES (:id, :codigo, :cliente, :origen, :destino, "
                ":desc,"
                " :prioridad, :peso, :volumen, :usuario)"
            ),
            {
                "id": envio_id,
                "codigo": codigo,
                "cliente": d.cliente_id,
                "origen": d.origen_id,
                "destino": d.destino_id,
                "desc": d.descripcion_carga,
                "prioridad": d.prioridad,
                "peso": d.peso_kg,
                "volumen": d.volumen_m3,
                "usuario": usuario_id,
            },
        )

    def obtener(self, tx: Session, envio_id: uuid.UUID, *, bloquear: bool = False) -> EnvioDTO | None:
        """Con `bloquear`, toma el lock de la fila: las operaciones sobre un mismo envío se serializan."""
        if bloquear:
            tx.execute(text("SELECT 1 FROM envio WHERE id = :id FOR UPDATE"), {"id": envio_id})
        row = tx.execute(text(_SELECT_ENVIO + " WHERE e.id = :id"), {"id": envio_id}).one_or_none()
        return EnvioDTO(*row) if row else None

    def en_alcance(self, tx: Session, envio_id: uuid.UUID, alcance: AlcanceConsulta) -> bool:
        return tx.execute(
            text("SELECT EXISTS (SELECT 1 FROM envio e WHERE e.id = :id AND" + _ALCANCE + ")"),
            {"id": envio_id, "cliente": alcance.cliente_id, "operador": alcance.operador_id},
        ).scalar_one()

    def listar(
        self,
        tx: Session,
        *,
        alcance: AlcanceConsulta,
        estado: str | None,
        texto: str | None,
        limite: int = 500,
    ) -> list[EnvioDTO]:
        rows = tx.execute(
            text(
                _SELECT_ENVIO
                + " WHERE"
                + _ALCANCE
                + " AND (CAST(:estado AS text) IS NULL OR e.estado = :estado)"
                " AND (CAST(:texto AS text) IS NULL OR e.codigo ILIKE '%' || :texto || '%' ESCAPE '\\'"
                "      OR c.razon_social ILIKE '%' || :texto || '%' ESCAPE '\\')"
                " ORDER BY e.creado_en DESC, e.codigo LIMIT :limite"
            ),
            {
                "cliente": alcance.cliente_id,
                "operador": alcance.operador_id,
                "estado": estado,
                "texto": _like(texto),
                "limite": limite,
            },
        )
        return [EnvioDTO(*r) for r in rows]

    def nueva_version(self, tx: Session, envio_id: uuid.UUID, **cambios: object) -> int:
        """Aplica `cambios` (columnas fijas, ver abajo) y suma 1 a la versión del agregado. Devuelve la
        nueva.
        """
        columnas = {"estado", "entrega_estimada_en", "entrega_comprometida_en", "entrega_real_en"}
        desconocidas = set(cambios) - columnas
        if desconocidas:  # los nombres de columna NO vienen del usuario, pero se valida igual
            raise ValueError(f"columnas no permitidas: {desconocidas}")
        sets = "".join(f", {c} = :{c}" for c in sorted(cambios))
        return tx.execute(
            text(
                f"UPDATE envio SET version = version + 1, actualizado_en = clock_timestamp(){sets}"
                " WHERE id = :id RETURNING version"
            ),
            {"id": envio_id, **cambios},
        ).scalar_one()


class TramoRepository:
    _SELECT = """
        SELECT t.id, t.envio_id, t.orden, t.origen_id, o.nombre, t.destino_id, d.nombre, t.medio,
        t.operador_id,
               op.razon_social, t.vehiculo_id, v.identificacion, t.estado, t.salida_estimada_en,
               t.llegada_estimada_en, t.salida_real_en, t.llegada_real_en, t.costo, t.moneda_codigo
        FROM tramo t
        JOIN ubicacion o ON o.id = t.origen_id
        JOIN ubicacion d ON d.id = t.destino_id
        JOIN tercero op ON op.id = t.operador_id
        LEFT JOIN vehiculo v ON v.id = t.vehiculo_id
    """

    def de_envio(self, tx: Session, envio_id: uuid.UUID) -> list[TramoDTO]:
        rows = tx.execute(text(self._SELECT + " WHERE t.envio_id = :id ORDER BY t.orden"), {"id": envio_id})
        return [TramoDTO(*r) for r in rows]

    def obtener(self, tx: Session, tramo_id: uuid.UUID) -> TramoDTO | None:
        row = tx.execute(text(self._SELECT + " WHERE t.id = :id"), {"id": tramo_id}).one_or_none()
        return TramoDTO(*row) if row else None

    def reemplazar(self, tx: Session, envio_id: uuid.UUID, tramos: list[TramoPlan]) -> list[uuid.UUID]:
        """Borra los tramos PENDIENTES del envío y crea los nuevos (el servicio ya validó que todos lo eran).

        Borrar es aceptable acá: un tramo pendiente de un envío no iniciado todavía no tiene historia.
        Devuelve los ids nuevos en orden (viajan en la outbox para que Neo4j reemplace exactamente esos).
        """
        tx.execute(text("DELETE FROM tramo WHERE envio_id = :id AND estado = 'PENDIENTE'"), {"id": envio_id})
        ids = []
        for orden, t in enumerate(tramos, start=1):
            tramo_id = uuid.uuid4()
            ids.append(tramo_id)
            tx.execute(
                text(
                    "INSERT INTO tramo (id, envio_id, orden, origen_id, destino_id, medio, operador_id, vehiculo_id,"
                    " salida_estimada_en, llegada_estimada_en, costo, moneda_codigo) VALUES (:id, :envio, :orden,"
                    " :origen, :destino, :medio, :operador, :vehiculo, :salida, :llegada, :costo, :moneda)"
                ),
                {"id": tramo_id, "envio": envio_id, "orden": orden, "origen": t.origen_id,
                 "destino": t.destino_id, "medio": t.medio, "operador": t.operador_id, "vehiculo": t.vehiculo_id,
                 "salida": t.salida_estimada_en, "llegada": t.llegada_estimada_en, "costo": t.costo,
                 "moneda": t.moneda_codigo},
            )
        return ids

    def asignar_vehiculo(self, tx: Session, tramo_id: uuid.UUID, vehiculo_id: uuid.UUID) -> None:
        tx.execute(
            text("UPDATE tramo SET vehiculo_id = :v, version = version + 1 WHERE id = :id"),
            {"id": tramo_id, "v": vehiculo_id},
        )

    def iniciar(self, tx: Session, tramo_id: uuid.UUID, salida_real: datetime) -> None:
        tx.execute(
            text(
                "UPDATE tramo SET estado = 'EN_CURSO', salida_real_en = :s, version = version + 1"
                " WHERE id = :id"
            ),
            {"id": tramo_id, "s": salida_real},
        )

    def finalizar(self, tx: Session, tramo_id: uuid.UUID, llegada_real: datetime) -> None:
        tx.execute(
            text(
                "UPDATE tramo SET estado = 'FINALIZADO', llegada_real_en = :l, version = version + 1"
                " WHERE id = :id"
            ),
            {"id": tramo_id, "l": llegada_real},
        )

    def cancelar_pendientes(self, tx: Session, envio_id: uuid.UUID) -> None:
        tx.execute(
            text(
                "UPDATE tramo SET estado = 'CANCELADO', version = version + 1"
                " WHERE envio_id = :id AND estado = 'PENDIENTE'"
            ),
            {"id": envio_id},
        )


class AsignacionRepository:
    _SELECT = """
        SELECT a.id, a.envio_id, a.contenedor_id, k.codigo_internacional, a.estado, a.reserva_desde,
               a.reserva_hasta, a.uso_desde, a.uso_hasta, a.peso_asignado_kg, a.volumen_asignado_m3
        FROM envio_contenedor a JOIN contenedor k ON k.id = a.contenedor_id
    """

    def de_envio(self, tx: Session, envio_id: uuid.UUID) -> list[AsignacionDTO]:
        rows = tx.execute(
            text(self._SELECT + " WHERE a.envio_id = :id ORDER BY a.reserva_desde, k.codigo_internacional"),
            {"id": envio_id},
        )
        return [AsignacionDTO(*r) for r in rows]

    def obtener(self, tx: Session, asignacion_id: uuid.UUID) -> AsignacionDTO | None:
        row = tx.execute(text(self._SELECT + " WHERE a.id = :id"), {"id": asignacion_id}).one_or_none()
        return AsignacionDTO(*row) if row else None

    def reserva_superpuesta(
        self, tx: Session, contenedor_id: uuid.UUID, desde: datetime, hasta: datetime
    ) -> bool:
        """Mismo criterio que la restricción ex_envcont_reserva: reservas vigentes [desde, hasta)."""
        return tx.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM envio_contenedor WHERE contenedor_id = :c"
                " AND estado IN ('RESERVADA', 'EN_USO')"
                " AND tstzrange(reserva_desde, reserva_hasta, '[)') && tstzrange(:d, :h, '[)'))"
            ),
            {"c": contenedor_id, "d": desde, "h": hasta},
        ).scalar_one()

    def totales_vigentes(self, tx: Session, envio_id: uuid.UUID) -> tuple[Decimal, Decimal]:
        row = tx.execute(
            text(
                "SELECT COALESCE(sum(peso_asignado_kg), 0), COALESCE(sum(volumen_asignado_m3), 0)"
                " FROM envio_contenedor WHERE envio_id = :id AND estado IN ('RESERVADA', 'EN_USO')"
            ),
            {"id": envio_id},
        ).one()
        return Decimal(row[0]), Decimal(row[1])

    def crear(self, tx: Session, asignacion_id: uuid.UUID, envio_id: uuid.UUID, d: AsignacionDatos) -> None:
        tx.execute(
            text(
                "INSERT INTO envio_contenedor (id, envio_id, contenedor_id, reserva_desde, reserva_hasta,"
                " peso_asignado_kg, volumen_asignado_m3) VALUES (:id, :envio, :cont, :desde, :hasta, :peso, "
                ":vol)"
            ),
            {
                "id": asignacion_id,
                "envio": envio_id,
                "cont": d.contenedor_id,
                "desde": d.reserva_desde,
                "hasta": d.reserva_hasta,
                "peso": d.peso_asignado_kg,
                "vol": d.volumen_asignado_m3,
            },
        )

    def cancelar(self, tx: Session, asignacion_id: uuid.UUID) -> bool:
        """Devuelve False si ya no estaba RESERVADA (por ejemplo, otra operación la canceló antes)."""
        return tx.execute(
            text("UPDATE envio_contenedor SET estado = 'CANCELADA' WHERE id = :id AND estado = 'RESERVADA'"),
            {"id": asignacion_id},
        ).rowcount == 1

    def contenedores_en_uso_por_otro(self, tx: Session, envio_id: uuid.UUID, desde: datetime) -> list[str]:
        """Contenedores reservados por este envío que otro envío está usando en [desde, ∞).

        Bloquea los contenedores (FOR UPDATE) para que nadie empiece a usarlos mientras se valida. Mismo
        criterio que la restricción ex_envcont_uso.
        """
        tx.execute(
            text("SELECT 1 FROM contenedor WHERE id IN (SELECT contenedor_id FROM envio_contenedor"
                 " WHERE envio_id = :e AND estado = 'RESERVADA') ORDER BY id FOR UPDATE"),
            {"e": envio_id},
        )
        return list(tx.execute(
            text(
                "SELECT DISTINCT k.codigo_internacional FROM envio_contenedor mia"
                " JOIN envio_contenedor otra ON otra.contenedor_id = mia.contenedor_id AND otra.envio_id <> mia.envio_id"
                " JOIN contenedor k ON k.id = mia.contenedor_id"
                " WHERE mia.envio_id = :e AND mia.estado = 'RESERVADA'"
                "   AND otra.uso_desde IS NOT NULL AND otra.estado IN ('EN_USO', 'FINALIZADA')"
                "   AND tstzrange(otra.uso_desde, COALESCE(otra.uso_hasta, 'infinity'::timestamptz), '[)')"
                "       && tstzrange(:d, 'infinity'::timestamptz, '[)')"
                " ORDER BY 1"
            ),
            {"e": envio_id, "d": desde},
        ).scalars())

    def cancelar_reservadas(self, tx: Session, envio_id: uuid.UUID) -> None:
        tx.execute(
            text(
                "UPDATE envio_contenedor SET estado = 'CANCELADA'"
                " WHERE envio_id = :id AND estado = 'RESERVADA'"
            ),
            {"id": envio_id},
        )

    def iniciar_uso(self, tx: Session, envio_id: uuid.UUID, desde: datetime) -> None:
        tx.execute(
            text(
                "UPDATE envio_contenedor SET estado = 'EN_USO', uso_desde = :d"
                " WHERE envio_id = :id AND estado = 'RESERVADA'"
            ),
            {"id": envio_id, "d": desde},
        )

    def finalizar_uso(self, tx: Session, envio_id: uuid.UUID, hasta: datetime) -> None:
        tx.execute(
            text(
                "UPDATE envio_contenedor SET estado = 'FINALIZADA', uso_hasta = :h"
                " WHERE envio_id = :id AND estado = 'EN_USO'"
            ),
            {"id": envio_id, "h": hasta},
        )


def _like(texto: str | None) -> str | None:
    if not texto:
        return None
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
