"""Repositorio de usuarios y de sus roles (tablas `usuario`, `usuario_rol`, `rol`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.core.errors import IntegrityError
from logistica.domain.acceso import UsuarioDatos, UsuarioDTO


@dataclass(frozen=True)
class Credenciales:
    """Lo mínimo para el login. El hash nunca sale de la capa de servicios (ni en el repr, por los logs)."""

    usuario_id: uuid.UUID
    password_hash: str = field(repr=False)
    activo: bool = True


_SELECT_USUARIO = """
    SELECT u.id, u.nombre, u.apellido, u.email, u.tercero_id, t.razon_social, u.activo,
           COALESCE(array_agg(r.codigo ORDER BY r.codigo) FILTER (WHERE r.codigo IS NOT NULL), '{}') AS roles
    FROM usuario u
    LEFT JOIN tercero t ON t.id = u.tercero_id
    LEFT JOIN usuario_rol ur ON ur.usuario_id = u.id
    LEFT JOIN rol r ON r.id = ur.rol_id
"""


def _dto(row) -> UsuarioDTO:
    return UsuarioDTO(
        row.id,
        row.nombre,
        row.apellido,
        row.email,
        row.tercero_id,
        row.razon_social,
        row.activo,
        frozenset(row.roles),
    )


class UsuarioRepository:
    def buscar_credenciales(self, tx: Session, email: str) -> Credenciales | None:
        row = tx.execute(
            text("SELECT id, password_hash, activo FROM usuario WHERE lower(email) = lower(:email)"),
            {"email": email.strip()},
        ).one_or_none()
        return Credenciales(row.id, row.password_hash, row.activo) if row else None

    def password_hash(self, tx: Session, usuario_id: uuid.UUID) -> str | None:
        return tx.execute(
            text("SELECT password_hash FROM usuario WHERE id = :id"), {"id": usuario_id}
        ).scalar()

    def existe_email(self, tx: Session, email: str, excluir_id: uuid.UUID | None = None) -> bool:
        return tx.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM usuario WHERE lower(email) = lower(:email) "
                "AND (CAST(:excluir AS uuid) IS NULL OR id <> :excluir))"
            ),
            {"email": email, "excluir": excluir_id},
        ).scalar_one()

    def obtener(self, tx: Session, usuario_id: uuid.UUID) -> UsuarioDTO | None:
        row = tx.execute(
            text(_SELECT_USUARIO + " WHERE u.id = :id GROUP BY u.id, t.razon_social"), {"id": usuario_id}
        ).one_or_none()
        return _dto(row) if row else None

    def listar(self, tx: Session) -> list[UsuarioDTO]:
        rows = tx.execute(
            text(_SELECT_USUARIO + " GROUP BY u.id, t.razon_social ORDER BY u.apellido, u.nombre")
        )
        return [_dto(r) for r in rows]

    def crear(self, tx: Session, usuario_id: uuid.UUID, datos: UsuarioDatos, password_hash: str) -> None:
        tx.execute(
            text(
                "INSERT INTO usuario (id, tercero_id, nombre, apellido, email, password_hash) "
                "VALUES (:id, :tercero, :nombre, :apellido, :email, :hash)"
            ),
            {
                "id": usuario_id,
                "tercero": datos.tercero_id,
                "nombre": datos.nombre,
                "apellido": datos.apellido,
                "email": datos.email,
                "hash": password_hash,
            },
        )

    def actualizar_datos(self, tx: Session, usuario_id: uuid.UUID, datos: UsuarioDatos) -> bool:
        result = tx.execute(
            text(
                "UPDATE usuario SET nombre = :nombre, apellido = :apellido, email = :email, "
                "actualizado_en = clock_timestamp() WHERE id = :id"
            ),
            {"id": usuario_id, "nombre": datos.nombre, "apellido": datos.apellido, "email": datos.email},
        )
        return result.rowcount == 1

    def set_activo(self, tx: Session, usuario_id: uuid.UUID, activo: bool) -> bool:
        result = tx.execute(
            text("UPDATE usuario SET activo = :activo, actualizado_en = clock_timestamp() WHERE id = :id"),
            {"id": usuario_id, "activo": activo},
        )
        return result.rowcount == 1

    def set_password_hash(self, tx: Session, usuario_id: uuid.UUID, password_hash: str) -> None:
        tx.execute(
            text(
                "UPDATE usuario SET password_hash = :hash, actualizado_en = clock_timestamp() WHERE id = :id"
            ),
            {"id": usuario_id, "hash": password_hash},
        )

    def reemplazar_roles(self, tx: Session, usuario_id: uuid.UUID, roles: set[str] | frozenset[str]) -> None:
        tx.execute(text("DELETE FROM usuario_rol WHERE usuario_id = :id"), {"id": usuario_id})
        insertados = tx.execute(
            text(
                "INSERT INTO usuario_rol (usuario_id, rol_id) "
                "SELECT :id, r.id FROM rol r WHERE r.codigo = ANY(:roles)"
            ),
            {"id": usuario_id, "roles": sorted(roles)},
        ).rowcount
        if insertados != len(roles):  # p. ej. la semilla de roles no se ejecutó
            raise IntegrityError(
                f"roles no encontrados en la tabla rol: {sorted(roles)}",
                user_message="Algún rol no existe en la base. Ejecute setup_databases.py.",
            )

    def bloquear_cambios_de_administradores(self, tx: Session) -> None:
        """Lock de transacción para todo cambio que pueda reducir (o crear el primer) administrador.

        Por qué no alcanza con `SELECT ... FOR UPDATE`: quitar un rol borra filas de `usuario_rol` sin tocar
        `usuario`, y PostgreSQL solo vuelve a evaluar una fila bloqueada si esa fila cambió. Con este lock la
        segunda transacción espera a que la primera termine, y su conteo (una sentencia nueva, con foto nueva
        en READ COMMITTED) ya ve el cambio confirmado. Se libera solo al terminar la transacción.
        """
        tx.execute(text("SELECT pg_advisory_xact_lock(hashtext('logistica.administradores'))"))

    def contar_administradores_activos(self, tx: Session) -> int:
        return tx.execute(
            text(
                "SELECT count(DISTINCT u.id) FROM usuario u JOIN usuario_rol ur ON ur.usuario_id = u.id "
                "JOIN rol r ON r.id = ur.rol_id WHERE r.codigo = 'ADMINISTRADOR' AND u.activo"
            )
        ).scalar_one()
