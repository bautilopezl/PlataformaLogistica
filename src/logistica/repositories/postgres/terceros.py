"""Repositorio de terceros y de sus roles comerciales (tablas `tercero`, `tercero_rol`).

Las bajas son lógicas: un tercero se desactiva (`activo = false`) y un rol comercial quitado queda con
`activo = false`; nunca se borran filas, porque otras tablas (envíos, facturas) las referencian.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.acceso import TerceroDatos, TerceroDTO

_SELECT = """
    SELECT t.id, t.razon_social, t.pais_codigo, t.tipo_identificacion, t.numero_identificacion, t.email,
           t.telefono, t.direccion, t.activo,
           COALESCE(array_agg(tr.rol_comercial ORDER BY tr.rol_comercial)
                    FILTER (WHERE tr.activo), '{}') AS roles
    FROM tercero t
    LEFT JOIN tercero_rol tr ON tr.tercero_id = t.id
"""


def _dto(row) -> TerceroDTO:
    return TerceroDTO(
        row.id,
        row.razon_social,
        row.pais_codigo,
        row.tipo_identificacion,
        row.numero_identificacion,
        row.email,
        row.telefono,
        row.direccion,
        row.activo,
        frozenset(row.roles),
    )


def _escapar_like(texto: str) -> str:
    """Busca el texto literal: '%' y '_' escritos por el usuario no actúan como comodines."""
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _params(datos: TerceroDatos) -> dict:
    return {
        "razon": datos.razon_social,
        "pais": datos.pais_codigo,
        "tipo": datos.tipo_identificacion,
        "numero": datos.numero_identificacion,
        "email": datos.email,
        "telefono": datos.telefono,
        "direccion": datos.direccion,
    }


class TerceroRepository:
    def obtener(self, tx: Session, tercero_id: uuid.UUID) -> TerceroDTO | None:
        row = tx.execute(text(_SELECT + " WHERE t.id = :id GROUP BY t.id"), {"id": tercero_id}).one_or_none()
        return _dto(row) if row else None

    def listar(
        self, tx: Session, *, texto: str | None, rol: str | None, incluir_inactivos: bool, limite: int = 500
    ) -> list[TerceroDTO]:
        rows = tx.execute(
            text(
                _SELECT
                + """
                WHERE (CAST(:texto AS text) IS NULL
                       OR t.razon_social ILIKE '%' || :texto || '%' ESCAPE '\\'
                       OR t.numero_identificacion ILIKE '%' || :texto || '%' ESCAPE '\\')
                  AND (:incluir_inactivos OR t.activo)
                  AND (CAST(:rol AS text) IS NULL OR EXISTS (
                        SELECT 1 FROM tercero_rol x
                        WHERE x.tercero_id = t.id AND x.rol_comercial = :rol AND x.activo))
                GROUP BY t.id
                ORDER BY t.razon_social
                LIMIT :limite
            """
            ),
            {
                "texto": _escapar_like(texto) if texto else None,
                "rol": rol,
                "incluir_inactivos": incluir_inactivos,
                "limite": limite,
            },
        )
        return [_dto(r) for r in rows]

    def existe_identificacion(
        self, tx: Session, datos: TerceroDatos, excluir_id: uuid.UUID | None = None
    ) -> bool:
        return tx.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM tercero WHERE pais_codigo = :pais "
                "AND tipo_identificacion = :tipo "
                "AND numero_identificacion = :numero AND (CAST(:excluir AS uuid) IS NULL OR id <> :excluir))"
            ),
            {
                "pais": datos.pais_codigo,
                "tipo": datos.tipo_identificacion,
                "numero": datos.numero_identificacion,
                "excluir": excluir_id,
            },
        ).scalar_one()

    def crear(self, tx: Session, tercero_id: uuid.UUID, datos: TerceroDatos) -> None:
        tx.execute(
            text(
                "INSERT INTO tercero (id, razon_social, pais_codigo, tipo_identificacion, "
                "numero_identificacion, "
                "email, telefono, direccion) VALUES (:id, :razon, :pais, :tipo, :numero, :email, :telefono, "
                ":direccion)"
            ),
            {"id": tercero_id, **_params(datos)},
        )

    def actualizar(self, tx: Session, tercero_id: uuid.UUID, datos: TerceroDatos) -> bool:
        result = tx.execute(
            text(
                "UPDATE tercero SET razon_social = :razon, pais_codigo = :pais, tipo_identificacion = :tipo, "
                "numero_identificacion = :numero, email = :email, telefono = :telefono, "
                "direccion = :direccion, "
                "actualizado_en = clock_timestamp() WHERE id = :id"
            ),
            {"id": tercero_id, **_params(datos)},
        )
        return result.rowcount == 1

    def set_activo(self, tx: Session, tercero_id: uuid.UUID, activo: bool) -> bool:
        result = tx.execute(
            text("UPDATE tercero SET activo = :activo, actualizado_en = clock_timestamp() WHERE id = :id"),
            {"id": tercero_id, "activo": activo},
        )
        return result.rowcount == 1

    def set_roles(self, tx: Session, tercero_id: uuid.UUID, roles: set[str] | frozenset[str]) -> None:
        """Deja activos exactamente `roles`; los demás quedan desactivados (baja lógica)."""
        tx.execute(
            text("UPDATE tercero_rol SET activo = (rol_comercial = ANY(:roles)) WHERE tercero_id = :id"),
            {"id": tercero_id, "roles": sorted(roles)},
        )
        tx.execute(
            text(
                "INSERT INTO tercero_rol (tercero_id, rol_comercial) "
                "SELECT :id, r FROM unnest(CAST(:roles AS text[])) r "
                "ON CONFLICT (tercero_id, rol_comercial) DO UPDATE SET activo = TRUE"
            ),
            {"id": tercero_id, "roles": sorted(roles)},
        )
