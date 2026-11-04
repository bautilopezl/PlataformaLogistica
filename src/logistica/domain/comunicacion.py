"""Contratos de comunicación; fechas UTC y cursores sin desplazamientos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from logistica.core.errors import ValidationError

CursorMensaje = tuple[datetime, UUID]


def texto_validado(contenido: str) -> str:
    if not contenido.strip() or len(contenido) > 4000:
        raise ValidationError(
            "texto de chat inválido", user_message="Escriba entre 1 y 4000 caracteres no vacíos."
        )
    return contenido


def techo_milisegundo(fecha: datetime) -> datetime:
    """BSON tiene precisión de milisegundos; no adelantar el ingreso ni acortar la salida."""
    fecha = fecha.astimezone(timezone.utc)
    resto = fecha.microsecond % 1000
    return fecha + timedelta(microseconds=1000 - resto) if resto else fecha


@dataclass(frozen=True)
class ConversacionDTO:
    id: UUID
    tipo: str
    nombre: str
    creada_por_id: UUID
    archivada_en: datetime | None


@dataclass(frozen=True)
class ParticipanteDTO:
    usuario_id: UUID
    nombre: str
    rol: str
    ingreso_en: datetime
    salida_en: datetime | None
    vigente: bool


@dataclass(frozen=True)
class DestinatarioDTO:
    id: UUID
    nombre: str


@dataclass(frozen=True)
class MensajeDTO:
    id: UUID
    conversacion_id: UUID
    remitente_id: UUID
    fecha_envio: datetime
    contenido: str


@dataclass(frozen=True)
class PaginaMensajesDTO:
    mensajes: tuple[MensajeDTO, ...]
    siguiente: CursorMensaje | None
    hasta: datetime
    membresias: tuple[ParticipanteDTO, ...]


@dataclass(frozen=True)
class VistaChatDTO:
    conversaciones: tuple[ConversacionDTO, ...]
    publicas: tuple[ConversacionDTO, ...]
    pagina: PaginaMensajesDTO | None
