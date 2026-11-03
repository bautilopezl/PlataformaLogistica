"""Jerarquía de errores de la aplicación.

La interfaz solo debería mostrar `user_message`; el detalle técnico va al log.
"""

from __future__ import annotations


class LogisticaError(Exception):
    """Base de todos los errores controlados."""

    user_message = "Ocurrió un error en la operación."

    def __init__(self, detail: str | None = None, *, user_message: str | None = None):
        super().__init__(detail or self.user_message)
        if user_message:
            self.user_message = user_message


class ValidationError(LogisticaError):
    user_message = "Los datos ingresados no son válidos."


class AuthenticationError(LogisticaError):
    user_message = "Credenciales inválidas."


class PermissionDeniedError(LogisticaError):
    user_message = "No tiene permiso para realizar esta acción."


class SessionExpiredError(LogisticaError):
    user_message = "La sesión venció o fue revocada. Inicie sesión nuevamente."


class NotFoundError(LogisticaError):
    user_message = "No se encontró el registro solicitado."


class BusinessRuleError(LogisticaError):
    user_message = "La operación viola una regla de negocio."


class IntegrityError(LogisticaError):
    user_message = "La operación viola una restricción de integridad."


class DatabaseUnavailableError(LogisticaError):
    """Un motor no responde. `engine` indica cuál (postgres, mongo, neo4j)."""

    user_message = "No se pudo acceder a la base de datos."

    def __init__(self, engine: str, detail: str | None = None):
        super().__init__(detail or f"{engine} no disponible")
        self.engine = engine
        self.user_message = f"No se pudo acceder a {engine}. Reintente en unos instantes."
