"""Repositorios de catálogos logísticos: ubicaciones, vehículos, contenedores y sensores."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.logistica import ContenedorDatos, SensorDatos, UbicacionDatos, VehiculoDatos
from logistica.domain.logistica_dtos import (
    ContenedorDTO,
    InstalacionDTO,
    SensorDTO,
    UbicacionDTO,
    VehiculoDTO,
)


def _like(texto: str | None) -> str | None:
    if not texto:
        return None
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- ubicaciones
class UbicacionRepository:
    _SELECT = (
        "SELECT id, nombre, tipo, pais_codigo, ciudad, direccion, latitud, longitud, activo FROM ubicacion"
    )

    def obtener(self, tx: Session, ubicacion_id: uuid.UUID) -> UbicacionDTO | None:
        row = tx.execute(text(self._SELECT + " WHERE id = :id"), {"id": ubicacion_id}).one_or_none()
        return UbicacionDTO(*row) if row else None

    def listar(self, tx: Session, *, texto: str | None, incluir_inactivas: bool) -> list[UbicacionDTO]:
        rows = tx.execute(
            text(
                self._SELECT + " WHERE (CAST(:t AS text) IS NULL OR nombre ILIKE '%' || :t || '%' ESCAPE '\\'"
                " OR ciudad ILIKE '%' || :t || '%' ESCAPE '\\') AND (:inactivas OR activo)"
                " ORDER BY pais_codigo, ciudad, nombre LIMIT 500"
            ),
            {"t": _like(texto), "inactivas": incluir_inactivas},
        )
        return [UbicacionDTO(*r) for r in rows]

    def crear(self, tx: Session, ubicacion_id: uuid.UUID, d: UbicacionDatos) -> None:
        tx.execute(
            text(
                "INSERT INTO ubicacion (id, nombre, tipo, pais_codigo, ciudad, direccion, latitud, longitud)"
                " VALUES (:id, :nombre, :tipo, :pais, :ciudad, :direccion, :lat, :lon)"
            ),
            {
                "id": ubicacion_id,
                "nombre": d.nombre,
                "tipo": d.tipo,
                "pais": d.pais_codigo,
                "ciudad": d.ciudad,
                "direccion": d.direccion,
                "lat": d.latitud,
                "lon": d.longitud,
            },
        )

    def actualizar(self, tx: Session, ubicacion_id: uuid.UUID, d: UbicacionDatos) -> bool:
        return (
            tx.execute(
                text(
                    "UPDATE ubicacion SET nombre = :nombre, tipo = :tipo, pais_codigo = :pais, ciudad = "
                    ":ciudad,"
                    " direccion = :direccion, latitud = :lat, longitud = :lon WHERE id = :id"
                ),
                {
                    "id": ubicacion_id,
                    "nombre": d.nombre,
                    "tipo": d.tipo,
                    "pais": d.pais_codigo,
                    "ciudad": d.ciudad,
                    "direccion": d.direccion,
                    "lat": d.latitud,
                    "lon": d.longitud,
                },
            ).rowcount
            == 1
        )

    def set_activa(self, tx: Session, ubicacion_id: uuid.UUID, activa: bool) -> bool:
        return (
            tx.execute(
                text("UPDATE ubicacion SET activo = :a WHERE id = :id"), {"id": ubicacion_id, "a": activa}
            ).rowcount
            == 1
        )


# --------------------------------------------------------------------------- vehículos
class VehiculoRepository:
    def tiene_tramos_abiertos(self, tx: Session, vehiculo_id: uuid.UUID) -> bool:
        return tx.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM tramo WHERE vehiculo_id = :id AND estado IN ('PENDIENTE', "
                "'EN_CURSO'))"
            ),
            {"id": vehiculo_id},
        ).scalar_one()

    _SELECT = (
        "SELECT v.id, v.operador_id, t.razon_social, v.tipo, v.identificacion, v.estado"
        " FROM vehiculo v JOIN tercero t ON t.id = v.operador_id"
    )

    def obtener(self, tx: Session, vehiculo_id: uuid.UUID) -> VehiculoDTO | None:
        row = tx.execute(text(self._SELECT + " WHERE v.id = :id"), {"id": vehiculo_id}).one_or_none()
        return VehiculoDTO(*row) if row else None

    def listar(self, tx: Session, *, operador_id: uuid.UUID | None) -> list[VehiculoDTO]:
        rows = tx.execute(
            text(
                self._SELECT + " WHERE (CAST(:op AS uuid) IS NULL OR v.operador_id = :op)"
                " ORDER BY t.razon_social, v.tipo, v.identificacion LIMIT 500"
            ),
            {"op": operador_id},
        )
        return [VehiculoDTO(*r) for r in rows]

    def existe(self, tx: Session, d: VehiculoDatos) -> bool:
        return tx.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM vehiculo WHERE operador_id = :op AND tipo = :tipo"
                " AND identificacion = :ident)"
            ),
            {"op": d.operador_id, "tipo": d.tipo, "ident": d.identificacion},
        ).scalar_one()

    def crear(self, tx: Session, vehiculo_id: uuid.UUID, d: VehiculoDatos) -> None:
        tx.execute(
            text(
                "INSERT INTO vehiculo (id, operador_id, tipo, identificacion) VALUES (:id, :op, :tipo, "
                ":ident)"
            ),
            {"id": vehiculo_id, "op": d.operador_id, "tipo": d.tipo, "ident": d.identificacion},
        )

    def set_estado(self, tx: Session, vehiculo_id: uuid.UUID, estado: str) -> bool:
        return (
            tx.execute(
                text("UPDATE vehiculo SET estado = :e WHERE id = :id"), {"id": vehiculo_id, "e": estado}
            ).rowcount
            == 1
        )


# --------------------------------------------------------------------------- contenedores
class ContenedorRepository:
    def tiene_asignaciones_activas(self, tx: Session, contenedor_id: uuid.UUID) -> bool:
        return tx.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM envio_contenedor WHERE contenedor_id = :id"
                " AND estado IN ('RESERVADA', 'EN_USO'))"
            ),
            {"id": contenedor_id},
        ).scalar_one()

    _SELECT = "SELECT id, codigo_internacional, tipo, capacidad_kg, capacidad_m3, estado FROM contenedor"

    def obtener(
        self, tx: Session, contenedor_id: uuid.UUID, *, bloquear: bool = False
    ) -> ContenedorDTO | None:
        # FOR UPDATE: dos asignaciones simultáneas del mismo contenedor se validan una después de la otra.
        sql = self._SELECT + " WHERE id = :id" + (" FOR UPDATE" if bloquear else "")
        row = tx.execute(text(sql), {"id": contenedor_id}).one_or_none()
        return ContenedorDTO(*row) if row else None

    def listar(self, tx: Session, *, texto: str | None) -> list[ContenedorDTO]:
        rows = tx.execute(
            text(
                self._SELECT
                + " WHERE (CAST(:t AS text) IS NULL OR codigo_internacional ILIKE '%' || :t || '%'"
                " ESCAPE '\\') ORDER BY codigo_internacional LIMIT 500"
            ),
            {"t": _like(texto)},
        )
        return [ContenedorDTO(*r) for r in rows]

    def existe_codigo(self, tx: Session, codigo: str) -> bool:
        return tx.execute(
            text("SELECT EXISTS (SELECT 1 FROM contenedor WHERE codigo_internacional = :c)"), {"c": codigo}
        ).scalar_one()

    def crear(self, tx: Session, contenedor_id: uuid.UUID, d: ContenedorDatos) -> None:
        tx.execute(
            text(
                "INSERT INTO contenedor (id, codigo_internacional, tipo, capacidad_kg, capacidad_m3)"
                " VALUES (:id, :codigo, :tipo, :kg, :m3)"
            ),
            {
                "id": contenedor_id,
                "codigo": d.codigo_internacional,
                "tipo": d.tipo,
                "kg": d.capacidad_kg,
                "m3": d.capacidad_m3,
            },
        )

    def set_estado(self, tx: Session, contenedor_id: uuid.UUID, estado: str) -> bool:
        return (
            tx.execute(
                text("UPDATE contenedor SET estado = :e WHERE id = :id"), {"id": contenedor_id, "e": estado}
            ).rowcount
            == 1
        )


# --------------------------------------------------------------------------- sensores
class SensorRepository:
    _SELECT = """
        SELECT s.id, s.codigo, s.tipo, s.fabricante, s.modelo, s.estado, s.intervalo_esperado_seg,
               COALESCE((SELECT array_agg(c.variable ORDER BY c.variable) FROM sensor_capacidad c
                         WHERE c.sensor_id = s.id), '{}') AS capacidades,
               (SELECT k.codigo_internacional
                FROM sensor_contenedor sc JOIN contenedor k ON k.id = sc.contenedor_id
                WHERE sc.sensor_id = s.id AND sc.instalado_en <= clock_timestamp()
                  AND (sc.retirado_en IS NULL OR sc.retirado_en > clock_timestamp())) AS contenedor_actual
        FROM sensor s
    """

    @staticmethod
    def _dto(r) -> SensorDTO:
        return SensorDTO(
            r.id,
            r.codigo,
            r.tipo,
            r.fabricante,
            r.modelo,
            r.estado,
            r.intervalo_esperado_seg,
            frozenset(r.capacidades),
            r.contenedor_actual,
        )

    def obtener(self, tx: Session, sensor_id: uuid.UUID, *, bloquear: bool = False) -> SensorDTO | None:
        if bloquear:  # serializa instalaciones simultáneas del mismo sensor
            tx.execute(text("SELECT 1 FROM sensor WHERE id = :id FOR UPDATE"), {"id": sensor_id})
        row = tx.execute(text(self._SELECT + " WHERE s.id = :id"), {"id": sensor_id}).one_or_none()
        return self._dto(row) if row else None

    def listar(self, tx: Session) -> list[SensorDTO]:
        return [self._dto(r) for r in tx.execute(text(self._SELECT + " ORDER BY s.codigo LIMIT 500"))]

    def existe_codigo(self, tx: Session, codigo: str) -> bool:
        return tx.execute(
            text("SELECT EXISTS (SELECT 1 FROM sensor WHERE codigo = :c)"), {"c": codigo}
        ).scalar_one()

    def crear(self, tx: Session, sensor_id: uuid.UUID, d: SensorDatos) -> None:
        tx.execute(
            text(
                "INSERT INTO sensor (id, codigo, tipo, fabricante, modelo, intervalo_esperado_seg)"
                " VALUES (:id, :codigo, :tipo, :fab, :modelo, :intervalo)"
            ),
            {
                "id": sensor_id,
                "codigo": d.codigo,
                "tipo": d.tipo,
                "fab": d.fabricante,
                "modelo": d.modelo,
                "intervalo": d.intervalo_esperado_seg,
            },
        )
        for variable in sorted(d.capacidades):
            tx.execute(
                text("INSERT INTO sensor_capacidad (sensor_id, variable) VALUES (:id, :v)"),
                {"id": sensor_id, "v": variable},
            )

    def set_estado(self, tx: Session, sensor_id: uuid.UUID, estado: str) -> bool:
        return (
            tx.execute(
                text("UPDATE sensor SET estado = :e WHERE id = :id"), {"id": sensor_id, "e": estado}
            ).rowcount
            == 1
        )

    def hay_solapamiento(self, tx: Session, sensor_id: uuid.UUID, desde: datetime) -> bool:
        """¿La instalación [desde, ∞) se superpone con otra del sensor? (mismo criterio que el EXCLUDE)."""
        return tx.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM sensor_contenedor WHERE sensor_id = :id"
                " AND tstzrange(instalado_en, COALESCE(retirado_en, 'infinity'::timestamptz), '[)')"
                " && tstzrange(:desde, 'infinity'::timestamptz, '[)'))"
            ),
            {"id": sensor_id, "desde": desde},
        ).scalar_one()

    def instalar(
        self,
        tx: Session,
        instalacion_id: uuid.UUID,
        sensor_id: uuid.UUID,
        contenedor_id: uuid.UUID,
        instalado_en: datetime,
        usuario_id: uuid.UUID,
        observaciones: str | None,
    ) -> None:
        tx.execute(
            text(
                "INSERT INTO sensor_contenedor (id, sensor_id, contenedor_id, instalado_en, instalado_por_id,"
                " observaciones) VALUES (:id, :s, :c, :en, :u, :obs)"
            ),
            {
                "id": instalacion_id,
                "s": sensor_id,
                "c": contenedor_id,
                "en": instalado_en,
                "u": usuario_id,
                "obs": observaciones,
            },
        )

    def instalacion(self, tx: Session, instalacion_id: uuid.UUID) -> InstalacionDTO | None:
        row = tx.execute(text(self._SELECT_INST + " WHERE sc.id = :id"), {"id": instalacion_id}).one_or_none()
        return InstalacionDTO(*row) if row else None

    def retirar(self, tx: Session, instalacion_id: uuid.UUID, retirado_en: datetime) -> bool:
        """Devuelve False si ya estaba retirada (otra operación se adelantó)."""
        return (
            tx.execute(
                text("UPDATE sensor_contenedor SET retirado_en = :r WHERE id = :id AND retirado_en IS NULL"),
                {"id": instalacion_id, "r": retirado_en},
            ).rowcount
            == 1
        )

    def historial(self, tx: Session, sensor_id: uuid.UUID) -> list[InstalacionDTO]:
        rows = tx.execute(
            text(self._SELECT_INST + " WHERE sc.sensor_id = :id ORDER BY sc.instalado_en DESC"),
            {"id": sensor_id},
        )
        return [InstalacionDTO(*r) for r in rows]

    _SELECT_INST = (
        "SELECT sc.id, sc.sensor_id, sc.contenedor_id, k.codigo_internacional, sc.instalado_en, "
        "sc.retirado_en,"
        " sc.observaciones FROM sensor_contenedor sc JOIN contenedor k ON k.id = sc.contenedor_id"
    )
