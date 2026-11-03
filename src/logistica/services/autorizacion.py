"""Punto único de autorización: sesión → permiso (el alcance lo valida cada servicio con el `Principal`).

Todos los servicios abren sus operaciones con `Autorizador.operacion(...)`. El `Principal` se reconstruye
desde PostgreSQL en cada llamada, dentro de la misma transacción de la operación: revocar una sesión,
desactivar un usuario o quitarle un rol corta la operación siguiente. Ocultar un botón en la UI no
reemplaza esta verificación.

Honestidad de seguridad (AGENTS.md §6): la app se conecta con una credencial técnica compartida; estos
controles no equivalen a seguridad por fila en la base.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from logistica.core.errors import PermissionDeniedError, SessionExpiredError
from logistica.domain.acceso import Principal, roles_efectivos
from logistica.repositories.connections import Connections
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.sesiones import SesionRepository


class Autorizador:
    def __init__(self, connections: Connections):
        self._conn = connections
        self._sesiones = SesionRepository()

    @contextmanager
    def operacion(self, sesion_id: uuid.UUID, *permisos: str) -> Iterator[tuple[Any, Principal]]:
        """Abre una transacción, valida la sesión y (si se indican) exige uno de los `permisos`."""
        with unit_of_work(self._conn) as tx:
            principal = self.principal(tx, sesion_id)
            if permisos:
                self.exigir(principal, *permisos)
            yield tx, principal

    def principal(self, tx: Any, sesion_id: uuid.UUID) -> Principal:
        ctx = self._sesiones.contexto_si_vigente(tx, sesion_id)
        if ctx is None:
            raise SessionExpiredError(f"sesión {sesion_id} no vigente")
        if ctx.tercero_id is not None and not ctx.tercero_activo:
            raise SessionExpiredError(
                f"empresa {ctx.tercero_id} inactiva",
                user_message="Su empresa no está habilitada. Contacte al administrador.",
            )
        roles = roles_efectivos(ctx.roles, ctx.tercero_id, ctx.roles_comerciales_activos)
        if not roles:
            raise SessionExpiredError(
                f"usuario {ctx.usuario_id} sin roles efectivos",
                user_message="Su usuario no tiene roles habilitados. Contacte al administrador.",
            )
        return Principal(
            sesion_id,
            ctx.usuario_id,
            ctx.email,
            ctx.nombre_completo,
            ctx.tercero_id,
            roles,
            self._sesiones.permisos_de_roles(tx, roles),
        )

    @staticmethod
    def exigir(principal: Principal, *permisos: str) -> None:
        if not principal.tiene_permiso(*permisos):
            raise PermissionDeniedError(f"usuario {principal.usuario_id} sin {permisos}")
