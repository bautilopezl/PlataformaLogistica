"""Repositorio de sesiones y del contexto de autorización (tablas `sesion`, `usuario`, roles y permisos).

La validez de una sesión se decide con el reloj de PostgreSQL (`clock_timestamp()`), no con el del equipo:
todas las instancias comparten la misma referencia de tiempo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.acceso import SesionDTO


@dataclass(frozen=True)
class ContextoSesion:
    """Datos crudos para reconstruir el `Principal`. Las reglas se aplican en el servicio."""

    usuario_id: uuid.UUID
    email: str
    nombre_completo: str
    tercero_id: uuid.UUID | None
    tercero_activo: bool | None
    roles: frozenset[str]
    roles_comerciales_activos: frozenset[str]


class SesionRepository:
    def crear(
        self,
        tx: Session,
        sesion_id: uuid.UUID,
        usuario_id: uuid.UUID,
        horas: float,
        instancia_id: uuid.UUID,
        cliente_descripcion: str | None,
    ) -> None:
        tx.execute(
            text(
                "INSERT INTO sesion (id, usuario_id, inicio_en, vence_en, instancia_id, cliente_descripcion) "
                "VALUES (:id, :usuario, clock_timestamp(), "
                "clock_timestamp() + make_interval(secs => :segundos), "
                ":instancia, :cliente)"
            ),
            {
                "id": sesion_id,
                "usuario": usuario_id,
                "segundos": horas * 3600,
                "instancia": instancia_id,
                "cliente": (cliente_descripcion or "")[:200] or None,
            },
        )

    def contexto_si_vigente(self, tx: Session, sesion_id: uuid.UUID) -> ContextoSesion | None:
        """Devuelve el contexto solo si la sesión está ABIERTA, no venció y el usuario está activo."""
        row = tx.execute(
            text("""
                SELECT u.id AS usuario_id, u.email, u.nombre || ' ' || u.apellido AS nombre_completo,
                       u.tercero_id, t.activo AS tercero_activo,
                       COALESCE((SELECT array_agg(r.codigo) FROM usuario_rol ur JOIN rol r ON r.id = ur.rol_id
                                 WHERE ur.usuario_id = u.id), '{}') AS roles,
                       COALESCE((SELECT array_agg(tr.rol_comercial) FROM tercero_rol tr
                                 WHERE tr.tercero_id = u.tercero_id AND tr.activo), '{}') AS comerciales
                FROM sesion s
                JOIN usuario u ON u.id = s.usuario_id
                LEFT JOIN tercero t ON t.id = u.tercero_id
                WHERE s.id = :id AND s.estado = 'ABIERTA' AND s.vence_en > clock_timestamp() AND u.activo
            """),
            {"id": sesion_id},
        ).one_or_none()
        if row is None:
            return None
        return ContextoSesion(
            row.usuario_id,
            row.email,
            row.nombre_completo,
            row.tercero_id,
            row.tercero_activo,
            frozenset(row.roles),
            frozenset(row.comerciales),
        )

    def permisos_de_roles(self, tx: Session, roles: frozenset[str]) -> frozenset[str]:
        rows = tx.execute(
            text(
                "SELECT DISTINCT p.codigo FROM rol r JOIN rol_permiso rp ON rp.rol_id = r.id "
                "JOIN permiso p ON p.id = rp.permiso_id WHERE r.codigo = ANY(:roles)"
            ),
            {"roles": sorted(roles)},
        ).scalars()
        return frozenset(rows)

    def usuario_de(self, tx: Session, sesion_id: uuid.UUID) -> uuid.UUID | None:
        return tx.execute(text("SELECT usuario_id FROM sesion WHERE id = :id"), {"id": sesion_id}).scalar()

    def finalizar(self, tx: Session, sesion_id: uuid.UUID, estado: str) -> bool:
        """Pasa una sesión ABIERTA a CERRADA (logout) o REVOCADA. Devuelve False si ya estaba finalizada."""
        result = tx.execute(
            text(
                "UPDATE sesion SET estado = :estado, fin_en = greatest(clock_timestamp(), inicio_en) "
                "WHERE id = :id AND estado = 'ABIERTA'"
            ),
            {"id": sesion_id, "estado": estado},
        )
        return result.rowcount == 1

    def revocar_de_usuario(
        self, tx: Session, usuario_id: uuid.UUID, *, excepto: uuid.UUID | None = None
    ) -> int:
        result = tx.execute(
            text(
                "UPDATE sesion SET estado = 'REVOCADA', fin_en = greatest(clock_timestamp(), inicio_en) "
                "WHERE usuario_id = :id AND estado = 'ABIERTA' "
                "AND (CAST(:excepto AS uuid) IS NULL OR id <> :excepto)"
            ),
            {"id": usuario_id, "excepto": excepto},
        )
        return result.rowcount

    def listar(
        self, tx: Session, *, usuario_id: uuid.UUID | None, solo_abiertas: bool, limite: int = 500
    ) -> list[SesionDTO]:
        rows = tx.execute(
            text("""
                SELECT s.id, s.usuario_id, u.email, s.inicio_en, s.vence_en, s.fin_en, s.estado,
                       s.cliente_descripcion
                FROM sesion s JOIN usuario u ON u.id = s.usuario_id
                WHERE (CAST(:usuario AS uuid) IS NULL OR s.usuario_id = :usuario)
                  AND (NOT :solo_abiertas OR (s.estado = 'ABIERTA' AND s.vence_en > clock_timestamp()))
                ORDER BY s.inicio_en DESC
                LIMIT :limite
            """),
            {"usuario": usuario_id, "solo_abiertas": solo_abiertas, "limite": limite},
        )
        return [
            SesionDTO(
                r.id,
                r.usuario_id,
                r.email,
                r.inicio_en,
                r.vence_en,
                r.fin_en,
                r.estado,
                r.cliente_descripcion,
            )
            for r in rows
        ]
