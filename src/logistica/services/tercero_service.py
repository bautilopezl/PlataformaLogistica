"""ABM de terceros (clientes, proveedores, operadores) y de sus roles comerciales."""

from __future__ import annotations

import uuid
from typing import Any

from logistica.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from logistica.domain.acceso import (
    PERMISOS_CONSULTA_TERCEROS,
    Permiso,
    TerceroDatos,
    TerceroDTO,
    validar_roles_comerciales,
)
from logistica.repositories.connections import Connections
from logistica.repositories.postgres.terceros import TerceroRepository
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador

GESTIONAR = Permiso.TERCEROS_GESTIONAR


class TerceroService:
    def __init__(self, connections: Connections, autorizador: Autorizador):
        self._conn = connections
        self._aut = autorizador
        self._terceros = TerceroRepository()

    def listar(
        self,
        sesion_id: uuid.UUID,
        *,
        texto: str | None = None,
        rol: str | None = None,
        incluir_inactivos: bool = False,
    ) -> list[TerceroDTO]:
        with self._aut.operacion(sesion_id, *PERMISOS_CONSULTA_TERCEROS) as (tx, p):
            if not p.es_interno:  # alcance: un externo nunca lista empresas ajenas, aunque tenga el permiso
                raise PermissionDeniedError(f"usuario externo {p.usuario_id} no lista terceros")
            return self._terceros.listar(
                tx, texto=(texto or "").strip() or None, rol=rol, incluir_inactivos=incluir_inactivos
            )

    def obtener(self, sesion_id: uuid.UUID, tercero_id: uuid.UUID) -> TerceroDTO:
        """Un interno con permiso de consulta ve cualquier tercero; un externo, solo su propia empresa."""
        with self._aut.operacion(sesion_id) as (tx, p):
            if p.es_interno:
                self._aut.exigir(p, *PERMISOS_CONSULTA_TERCEROS)
            elif not p.puede_ver_tercero(tercero_id):  # alcance: antes de buscar, para no revelar si existe
                raise PermissionDeniedError(f"usuario {p.usuario_id} fuera de alcance de {tercero_id}")
            return self._obtener(tx, tercero_id)

    def crear(self, sesion_id: uuid.UUID, datos: TerceroDatos, roles_comerciales: set[str]) -> TerceroDTO:
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            datos = datos.normalizado()
            validar_roles_comerciales(set(roles_comerciales))
            self._exigir_identificacion_libre(tx, datos)
            tercero_id = uuid.uuid4()
            self._terceros.crear(tx, tercero_id, datos)
            self._terceros.set_roles(tx, tercero_id, set(roles_comerciales))
            tercero = self._obtener(tx, tercero_id)
            auditoria.registrar(
                tx,
                "crear_tercero",
                p,
                entidad="tercero",
                entidad_id=tercero_id,
                roles=sorted(roles_comerciales),
            )
        return tercero

    def actualizar(
        self,
        sesion_id: uuid.UUID,
        tercero_id: uuid.UUID,
        datos: TerceroDatos,
        roles_comerciales: set[str] | None = None,
    ) -> TerceroDTO:
        """Cambia los datos y, si se indican, los roles comerciales, en una sola transacción."""
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            self._obtener(tx, tercero_id)
            datos = datos.normalizado()
            if roles_comerciales is not None:
                validar_roles_comerciales(set(roles_comerciales))
            self._exigir_identificacion_libre(tx, datos, excluir_id=tercero_id)
            self._terceros.actualizar(tx, tercero_id, datos)
            if roles_comerciales is not None:
                self._terceros.set_roles(tx, tercero_id, set(roles_comerciales))
            tercero = self._obtener(tx, tercero_id)
            auditoria.registrar(
                tx,
                "actualizar_tercero",
                p,
                entidad="tercero",
                entidad_id=tercero_id,
                roles=sorted(roles_comerciales) if roles_comerciales is not None else None,
            )
        return tercero

    def set_roles_comerciales(
        self, sesion_id: uuid.UUID, tercero_id: uuid.UUID, roles: set[str]
    ) -> TerceroDTO:
        """Deja activos exactamente `roles`. Los usuarios externos que dependían de un rol quitado pierden el
        acceso en su próxima operación (ver `roles_efectivos`)."""
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            self._obtener(tx, tercero_id)
            validar_roles_comerciales(set(roles))
            self._terceros.set_roles(tx, tercero_id, set(roles))
            tercero = self._obtener(tx, tercero_id)
            auditoria.registrar(
                tx, "roles_tercero", p, entidad="tercero", entidad_id=tercero_id, roles=sorted(roles)
            )
        return tercero

    def set_activo(self, sesion_id: uuid.UUID, tercero_id: uuid.UUID, activo: bool) -> TerceroDTO:
        """Baja lógica: el tercero sigue existiendo (envíos y facturas lo referencian)."""
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            self._obtener(tx, tercero_id)
            self._terceros.set_activo(tx, tercero_id, activo)
            tercero = self._obtener(tx, tercero_id)
            auditoria.registrar(
                tx,
                "activar_tercero" if activo else "desactivar_tercero",
                p,
                entidad="tercero",
                entidad_id=tercero_id,
            )
        return tercero

    def _exigir_identificacion_libre(
        self, tx: Any, datos: TerceroDatos, excluir_id: uuid.UUID | None = None
    ) -> None:
        # Respaldo en la base: uq_tercero_identificacion (pais, tipo, número).
        if self._terceros.existe_identificacion(tx, datos, excluir_id):
            raise ValidationError(
                "identificación duplicada",
                user_message="Ya existe un tercero con esa identificación en ese país.",
            )

    def _obtener(self, tx: Any, tercero_id: uuid.UUID) -> TerceroDTO:
        tercero = self._terceros.obtener(tx, tercero_id)
        if tercero is None:
            raise NotFoundError(f"tercero {tercero_id}", user_message="El tercero no existe.")
        return tercero
