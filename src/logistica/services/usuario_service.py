"""Administración de usuarios y de sus roles (requiere usuarios.administrar)."""

from __future__ import annotations

import uuid
from typing import Any

from logistica.core.errors import BusinessRuleError, NotFoundError, ValidationError
from logistica.domain.acceso import (
    Permiso,
    Rol,
    UsuarioDatos,
    UsuarioDTO,
    validar_password,
    validar_roles_de_usuario,
)
from logistica.domain.passwords import Hasher
from logistica.repositories.connections import Connections
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.sesiones import SesionRepository
from logistica.repositories.postgres.terceros import TerceroRepository
from logistica.repositories.postgres.usuarios import UsuarioRepository
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador

ADMIN = Permiso.USUARIOS_ADMINISTRAR


class UsuarioService:
    def __init__(self, connections: Connections, hasher: Hasher, autorizador: Autorizador):
        self._conn = connections
        self._hasher = hasher
        self._aut = autorizador
        self._usuarios = UsuarioRepository()
        self._terceros = TerceroRepository()
        self._sesiones = SesionRepository()

    # --- consultas ----------------------------------------------------------
    def listar(self, sesion_id: uuid.UUID) -> list[UsuarioDTO]:
        with self._aut.operacion(sesion_id, ADMIN) as (tx, _p):
            return self._usuarios.listar(tx)

    def contar_administradores_activos(self, sesion_id: uuid.UUID) -> int:
        with self._aut.operacion(sesion_id, ADMIN) as (tx, _p):
            return self._usuarios.contar_administradores_activos(tx)

    # --- altas y cambios ----------------------------------------------------
    def crear(self, sesion_id: uuid.UUID, datos: UsuarioDatos, roles: set[str], password: str) -> UsuarioDTO:
        with self._aut.operacion(sesion_id, ADMIN) as (tx, p):
            usuario = self._crear(tx, datos, roles, password)
            auditoria.registrar(
                tx, "crear_usuario", p, entidad="usuario", entidad_id=usuario.id, roles=sorted(roles)
            )
        return usuario

    def crear_administrador_inicial(self, datos: UsuarioDatos, password: str) -> UsuarioDTO:
        """Primer administrador del sistema (scripts/crear_admin.py). Solo funciona si no hay ninguno activo.

        No pide sesión porque todavía no existe nadie que pueda iniciarla; por eso se niega en cuanto hay un
        administrador activo (no es una puerta trasera).
        """
        with unit_of_work(self._conn) as tx:
            self._usuarios.bloquear_cambios_de_administradores(tx)  # dos ejecuciones a la vez: una espera
            if self._usuarios.contar_administradores_activos(tx) > 0:
                raise BusinessRuleError(
                    "ya hay administradores", user_message="Ya existe un administrador activo."
                )
            usuario = self._crear(
                tx,
                UsuarioDatos(datos.nombre, datos.apellido, datos.email, None),
                {Rol.ADMINISTRADOR},
                password,
            )
            auditoria.registrar(
                tx, "crear_administrador_inicial", None, entidad="usuario", entidad_id=usuario.id
            )
        return usuario

    def actualizar(
        self, sesion_id: uuid.UUID, usuario_id: uuid.UUID, datos: UsuarioDatos, roles: set[str] | None = None
    ) -> UsuarioDTO:
        """Cambia nombre, apellido, email y (si se indican) roles, en una sola transacción.

        La empresa de un usuario no se cambia (AGENTS.md §11): se ignora `datos.tercero_id`.
        """
        with self._aut.operacion(sesion_id, ADMIN) as (tx, p):
            actual = self._obtener(tx, usuario_id)
            datos = UsuarioDatos(datos.nombre, datos.apellido, datos.email, actual.tercero_id).normalizado()
            self._exigir_email_libre(tx, datos.email, excluir_id=usuario_id)
            self._usuarios.actualizar_datos(tx, usuario_id, datos)
            if roles is not None:
                self._aplicar_roles(tx, actual, set(roles))
            usuario = self._obtener(tx, usuario_id)
            auditoria.registrar(
                tx,
                "actualizar_usuario",
                p,
                entidad="usuario",
                entidad_id=usuario_id,
                roles=sorted(roles) if roles is not None else None,
            )
        return usuario

    def asignar_roles(self, sesion_id: uuid.UUID, usuario_id: uuid.UUID, roles: set[str]) -> UsuarioDTO:
        with self._aut.operacion(sesion_id, ADMIN) as (tx, p):
            self._aplicar_roles(tx, self._obtener(tx, usuario_id), set(roles))
            usuario = self._obtener(tx, usuario_id)
            auditoria.registrar(
                tx, "asignar_roles", p, entidad="usuario", entidad_id=usuario_id, roles=sorted(roles)
            )
        return usuario

    def _aplicar_roles(self, tx: Any, actual: UsuarioDTO, roles: set[str]) -> None:
        if roles == set(actual.roles):
            return  # sin cambios: no se exige que la empresa siga activa para editar nombre o email
        validar_roles_de_usuario(roles, actual.tercero_id, self._comerciales(tx, actual.tercero_id))
        if Rol.ADMINISTRADOR in actual.roles and Rol.ADMINISTRADOR not in roles and actual.activo:
            self._exigir_otro_administrador(tx)
        self._usuarios.reemplazar_roles(tx, actual.id, roles)

    def set_activo(self, sesion_id: uuid.UUID, usuario_id: uuid.UUID, activo: bool) -> UsuarioDTO:
        """Activa o desactiva (baja lógica). Desactivar revoca todas las sesiones abiertas del usuario."""
        with self._aut.operacion(sesion_id, ADMIN) as (tx, p):
            actual = self._obtener(tx, usuario_id)
            revocadas = 0
            if not activo:
                if usuario_id == p.usuario_id:
                    raise BusinessRuleError(
                        "auto-desactivación", user_message="No puede desactivar su propio usuario."
                    )
                if Rol.ADMINISTRADOR in actual.roles and actual.activo:
                    self._exigir_otro_administrador(tx)
            self._usuarios.set_activo(tx, usuario_id, activo)
            if not activo:
                revocadas = self._sesiones.revocar_de_usuario(tx, usuario_id)
            usuario = self._obtener(tx, usuario_id)
            auditoria.registrar(
                tx,
                "activar_usuario" if activo else "desactivar_usuario",
                p,
                entidad="usuario",
                entidad_id=usuario_id,
                sesiones_revocadas=revocadas,
            )
        return usuario

    def resetear_password(self, sesion_id: uuid.UUID, usuario_id: uuid.UUID, nueva: str) -> None:
        """El administrador fija una contraseña nueva; las sesiones abiertas del usuario se revocan."""
        with self._aut.operacion(sesion_id, ADMIN) as (tx, p):
            self._obtener(tx, usuario_id)
            validar_password(nueva)
            self._usuarios.set_password_hash(tx, usuario_id, self._hasher.hash(nueva))
            revocadas = self._sesiones.revocar_de_usuario(tx, usuario_id)
            auditoria.registrar(
                tx,
                "resetear_password",
                p,
                entidad="usuario",
                entidad_id=usuario_id,
                sesiones_revocadas=revocadas,
            )

    # --- auxiliares ---------------------------------------------------------
    def _crear(self, tx: Any, datos: UsuarioDatos, roles: set[str], password: str) -> UsuarioDTO:
        datos = datos.normalizado()
        validar_password(password)
        validar_roles_de_usuario(set(roles), datos.tercero_id, self._comerciales(tx, datos.tercero_id))
        self._exigir_email_libre(tx, datos.email)
        usuario_id = uuid.uuid4()
        self._usuarios.crear(tx, usuario_id, datos, self._hasher.hash(password))
        self._usuarios.reemplazar_roles(tx, usuario_id, set(roles))
        return self._obtener(tx, usuario_id)

    def _comerciales(self, tx: Any, tercero_id: uuid.UUID | None) -> frozenset[str]:
        if tercero_id is None:
            return frozenset()
        tercero = self._terceros.obtener(tx, tercero_id)
        if tercero is None:
            raise NotFoundError(f"tercero {tercero_id}", user_message="La empresa indicada no existe.")
        if not tercero.activo:
            raise BusinessRuleError("tercero inactivo", user_message="La empresa indicada está desactivada.")
        return tercero.roles_comerciales

    def _exigir_email_libre(self, tx: Any, email: str, excluir_id: uuid.UUID | None = None) -> None:
        # La base tiene además uq_usuario_email (lower(email)) como respaldo ante dos altas simultáneas.
        if self._usuarios.existe_email(tx, email, excluir_id):
            raise ValidationError("email duplicado", user_message="Ya existe un usuario con ese email.")

    def _exigir_otro_administrador(self, tx: Any) -> None:
        self._usuarios.bloquear_cambios_de_administradores(tx)  # ver el docstring del repositorio
        if self._usuarios.contar_administradores_activos(tx) <= 1:
            raise BusinessRuleError(
                "último administrador", user_message="Debe quedar al menos un administrador activo."
            )

    def _obtener(self, tx: Any, usuario_id: uuid.UUID) -> UsuarioDTO:
        usuario = self._usuarios.obtener(tx, usuario_id)
        if usuario is None:
            raise NotFoundError(f"usuario {usuario_id}", user_message="El usuario no existe.")
        return usuario
