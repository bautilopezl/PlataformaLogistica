"""Login, logout, sesiones y cambio de la propia contraseña."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from logistica.core.config import SecurityConfig
from logistica.core.errors import AuthenticationError, NotFoundError, SessionExpiredError
from logistica.domain.acceso import Permiso, Principal, SesionDTO, validar_password
from logistica.domain.auditoria import RECHAZO
from logistica.domain.passwords import Hasher
from logistica.repositories.connections import Connections
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.sesiones import SesionRepository
from logistica.repositories.postgres.usuarios import UsuarioRepository
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador

log = logging.getLogger(__name__)

# Mismo mensaje para email inexistente, clave incorrecta o usuario inactivo: no revela qué cuentas existen.
MENSAJE_LOGIN_FALLIDO = "Email o contraseña incorrectos."


class AccesoService:
    def __init__(
        self,
        connections: Connections,
        hasher: Hasher,
        security: SecurityConfig,
        instancia_id: uuid.UUID,
        autorizador: Autorizador,
    ):
        self._conn = connections
        self._hasher = hasher
        self._security = security
        self._instancia_id = instancia_id  # identifica esta ejecución de la app (no es credencial)
        self._aut = autorizador
        self._usuarios = UsuarioRepository()
        self._sesiones = SesionRepository()

    def login(self, email: str, password: str, cliente_descripcion: str | None = None) -> Principal:
        try:
            with unit_of_work(self._conn) as tx:
                principal = self._abrir_sesion(tx, email, password, cliente_descripcion)
                auditoria.registrar(
                    tx,
                    "login",
                    principal,
                    entidad="sesion",
                    entidad_id=principal.sesion_id,
                    instancia_id=self._instancia_id,
                )
            return principal
        except _LoginRechazado as rechazo:
            # La transacción del login se revirtió (no queda sesión); el intento se registra en una propia.
            with unit_of_work(self._conn) as tx:
                auditoria.registrar(
                    tx,
                    rechazo.accion,
                    None,
                    resultado=RECHAZO,
                    usuario_id=rechazo.usuario_id,
                    instancia_id=self._instancia_id,
                )
            raise rechazo.error from None

    def _abrir_sesion(self, tx: Any, email: str, password: str, cliente: str | None) -> Principal:
        cred = self._usuarios.buscar_credenciales(tx, email)
        # Se verifica siempre (con un hash de referencia si el email no existe): así el tiempo de
        # respuesta no revela qué emails existen.
        clave_ok = self._hasher.verificar(cred.password_hash if cred else None, password)
        if cred is None or not clave_ok or not cred.activo:
            # Con email inexistente no se guarda nada personal (ni el email tipeado).
            raise _LoginRechazado(
                "login_fallido",
                cred.usuario_id if cred else None,
                AuthenticationError("login fallido", user_message=MENSAJE_LOGIN_FALLIDO),
            )
        if self._hasher.necesita_rehash(cred.password_hash):
            self._usuarios.set_password_hash(tx, cred.usuario_id, self._hasher.hash(password))
        sesion_id = uuid.uuid4()
        self._sesiones.crear(
            tx, sesion_id, cred.usuario_id, self._security.session_hours, self._instancia_id, cliente
        )
        try:
            return self._aut.principal(tx, sesion_id)
        except SessionExpiredError as exc:
            # Usuario externo cuya empresa está inactiva o sin el rol comercial: no entra.
            raise _LoginRechazado(
                "login_rechazado",
                cred.usuario_id,
                AuthenticationError(str(exc), user_message=exc.user_message),
            ) from exc

    def principal(self, sesion_id: uuid.UUID) -> Principal:
        with self._aut.operacion(sesion_id) as (_tx, principal):
            return principal

    def logout(self, sesion_id: uuid.UUID) -> None:
        with unit_of_work(self._conn) as tx:
            usuario_id = self._sesiones.usuario_de(tx, sesion_id)
            cerrada = self._sesiones.finalizar(tx, sesion_id, "CERRADA")
            if usuario_id is not None:  # una sesión inexistente no se audita (no hay a quién atribuirla)
                auditoria.registrar(
                    tx,
                    "logout",
                    None,
                    usuario_id=usuario_id,
                    sesion_id=sesion_id,
                    entidad="sesion",
                    entidad_id=sesion_id,
                    efectivo=cerrada,
                )

    def listar_sesiones(
        self, sesion_id: uuid.UUID, *, todas: bool = False, solo_abiertas: bool = True
    ) -> list[SesionDTO]:
        """Sesiones propias; con `todas`, las de todos los usuarios (requiere sesiones.administrar)."""
        with self._aut.operacion(sesion_id) as (tx, p):
            if todas:
                self._aut.exigir(p, Permiso.SESIONES_ADMINISTRAR)
            sesiones = self._sesiones.listar(
                tx, usuario_id=None if todas else p.usuario_id, solo_abiertas=solo_abiertas
            )
        return [_marcar_actual(s, sesion_id) for s in sesiones]

    def revocar_sesion(self, sesion_id: uuid.UUID, objetivo_id: uuid.UUID) -> None:
        """Cualquiera revoca sus propias sesiones; las ajenas requieren sesiones.administrar."""
        with self._aut.operacion(sesion_id) as (tx, p):
            duenio = self._sesiones.usuario_de(tx, objetivo_id)
            if duenio is None:
                raise NotFoundError(f"sesión {objetivo_id}", user_message="La sesión no existe.")
            if duenio != p.usuario_id:
                self._aut.exigir(p, Permiso.SESIONES_ADMINISTRAR)
            self._sesiones.finalizar(tx, objetivo_id, "REVOCADA")
            auditoria.registrar(
                tx, "revocar_sesion", p, entidad="sesion", entidad_id=objetivo_id, usuario_afectado=duenio
            )

    def cambiar_password(self, sesion_id: uuid.UUID, actual: str, nueva: str) -> None:
        with self._aut.operacion(sesion_id) as (tx, p):
            if not self._hasher.verificar(self._usuarios.password_hash(tx, p.usuario_id), actual):
                raise AuthenticationError(
                    "clave actual incorrecta", user_message="La contraseña actual no es correcta."
                )
            validar_password(nueva)
            self._usuarios.set_password_hash(tx, p.usuario_id, self._hasher.hash(nueva))
            # Las otras sesiones del usuario (otros equipos) se revocan; la actual sigue.
            otras = self._sesiones.revocar_de_usuario(tx, p.usuario_id, excepto=sesion_id)
            auditoria.registrar(
                tx,
                "cambiar_password",
                p,
                entidad="usuario",
                entidad_id=p.usuario_id,
                sesiones_revocadas=otras,
            )


class _LoginRechazado(Exception):
    """Uso interno: sale del bloque de la transacción (revirtiéndola) llevando qué auditar."""

    def __init__(self, accion: str, usuario_id: uuid.UUID | None, error: AuthenticationError):
        super().__init__(accion)
        self.accion, self.usuario_id, self.error = accion, usuario_id, error


def _marcar_actual(s: SesionDTO, actual: uuid.UUID) -> SesionDTO:
    if s.id != actual:
        return s
    return SesionDTO(
        s.id,
        s.usuario_id,
        s.usuario_email,
        s.inicio_en,
        s.vence_en,
        s.fin_en,
        s.estado,
        s.cliente_descripcion,
        es_actual=True,
    )
