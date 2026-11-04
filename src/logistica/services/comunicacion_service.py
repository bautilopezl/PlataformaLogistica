"""Chat directo PostgreSQL/MongoDB sin promesas de atomicidad distribuida."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from logistica.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from logistica.domain.acceso import Principal, UsuarioDTO, roles_efectivos
from logistica.domain.comunicacion import (
    ConversacionDTO,
    CursorMensaje,
    DestinatarioDTO,
    MensajeDTO,
    PaginaMensajesDTO,
    ParticipanteDTO,
    VistaChatDTO,
    techo_milisegundo,
    texto_validado,
)
from logistica.repositories.connections import Connections
from logistica.repositories.mongo.mensajes import MensajesRepository
from logistica.repositories.postgres.conversaciones import ConversacionesRepository
from logistica.repositories.postgres.usuarios import UsuarioRepository
from logistica.repositories.postgres.terceros import TerceroRepository
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador

log = logging.getLogger(__name__)
CREAR = "conversaciones.crear"
ContextoChat = tuple[Principal, ConversacionDTO, tuple[ParticipanteDTO, ...]]


class ComunicacionService:
    def __init__(self, connections: Connections, autorizador: Autorizador):
        self._aut = autorizador
        self._repo = ConversacionesRepository()
        self._usuarios = UsuarioRepository()
        self._mensajes = MensajesRepository(connections)

    def _usuario_habilitado(self, tx: Any, usuario: UUID) -> UsuarioDTO:
        dto = self._usuarios.obtener(tx, usuario)
        if dto is None or not dto.activo:
            raise PermissionDeniedError("participante inexistente o desactivado")
        # La consulta del contexto exige también empresa y rol comercial activos.
        comerciales = frozenset()
        if dto.tercero_id:
            tercero = TerceroRepository().obtener(tx, dto.tercero_id)
            if tercero is None or not tercero.activo:
                raise PermissionDeniedError("empresa del participante inactiva")
            comerciales = tercero.roles_comerciales
        if not roles_efectivos(dto.roles, dto.tercero_id, comerciales):
            raise PermissionDeniedError("participante sin roles efectivos")
        return dto

    def _acceso(
        self, tx: Any, sesion: UUID, ident: UUID, *, gestionar: bool = False, bloquear: bool = True,
    ) -> ContextoChat:
        conv = self._repo.obtener(tx, ident, bloquear=bloquear)
        principal = self._aut.principal(tx, sesion)  # también después de esperar el lock
        if conv is None or conv.archivada_en:
            raise PermissionDeniedError("conversación inexistente o archivada")
        miembros = self._repo.miembros(tx, ident)
        propios = [m for m in miembros if m.usuario_id == principal.usuario_id and m.vigente]
        if not propios:
            raise PermissionDeniedError(
                "membresía no vigente", user_message="Ya no tiene acceso a esta conversación."
            )
        if conv.tipo in ("GRUPAL", "PUBLICA") and not principal.es_interno:
            raise PermissionDeniedError("grupos y canales son sólo internos")
        if conv.tipo == "PRIVADA":
            usuarios = {m.usuario_id for m in miembros}
            if len(usuarios) != 2:
                raise PermissionDeniedError("privada sin exactamente dos participantes")
            for usuario in usuarios:
                dto = self._usuarios.obtener(tx, usuario)
                if dto is None or dto.tercero_id != principal.tercero_id:
                    raise PermissionDeniedError("privado fuera de la empresa")
        if gestionar:
            self._aut.exigir(principal, CREAR)
            if propios[0].rol != "ADMINISTRADOR" or conv.tipo == "PRIVADA":
                raise PermissionDeniedError("requiere administrador miembro del grupo o canal")
        return principal, conv, miembros

    def _validar_resultado(self, sesion: UUID, ident: UUID, contexto: ContextoChat) -> None:
        """Descarta una respuesta entre motores si el acceso cambió antes de devolverla."""
        with self._aut.operacion(sesion) as (tx, _):
            if self._acceso(tx, sesion, ident) != contexto:
                raise PermissionDeniedError(
                    "el acceso cambió durante la operación",
                    user_message="Cambió el acceso. Se descartó la respuesta; vuelva a consultar.",
                )

    def destinatarios(self, sesion: UUID, *, grupo: bool = False) -> tuple[DestinatarioDTO, ...]:
        """Directorio acotado: misma empresa para privados; grupos sólo internos."""
        with self._aut.operacion(sesion) as (tx, p):
            if grupo:
                self._aut.exigir(p, CREAR)
                if not p.es_interno:
                    raise PermissionDeniedError("grupo externo no permitido")
            resultado = []
            for u in self._usuarios.listar(tx):
                if u.activo and u.tercero_id == p.tercero_id and u.id != p.usuario_id:
                    try:
                        self._usuario_habilitado(tx, u.id)
                    except PermissionDeniedError:
                        log.info("Directorio chat omite usuario no habilitado %s", u.id)
                        continue
                    resultado.append(DestinatarioDTO(u.id, f"{u.nombre} {u.apellido}"))
            return tuple(resultado)

    def crear(
        self, sesion: UUID, tipo: str, nombre: str, participantes: tuple[UUID, ...],
    ) -> UUID:
        """Crea conversación y miembros con auditoría en una sola transacción PostgreSQL."""
        if tipo not in ("PRIVADA", "GRUPAL", "PUBLICA"):
            raise ValidationError("tipo de conversación inválido")
        nombre = nombre.strip()
        if len(nombre) > 150 or (tipo != "PRIVADA" and not nombre):
            raise ValidationError("nombre requerido, hasta 150 caracteres")
        with self._aut.operacion(sesion) as (tx, p):
            if tipo != "PRIVADA":
                self._aut.exigir(p, CREAR)
                if not p.es_interno:
                    raise PermissionDeniedError("grupos y canales son internos")
            ids = set(participantes) | {p.usuario_id}
            if tipo == "PRIVADA" and len(ids) != 2:
                raise ValidationError("un privado requiere dos usuarios distintos")
            for usuario in ids:
                dto = self._usuario_habilitado(tx, usuario)
                if dto.tercero_id != p.tercero_id:
                    raise PermissionDeniedError("participante fuera del alcance de la conversación")
            ident = uuid4()
            self._repo.crear(tx, ident, tipo, nombre, p.usuario_id)
            for usuario in sorted(ids):
                rol = "ADMINISTRADOR" if usuario == p.usuario_id and tipo != "PRIVADA" else "MIEMBRO"
                self._repo.incorporar(tx, ident, usuario, rol)
            auditoria.registrar(tx, "conversacion.crear", p, entidad="conversacion", entidad_id=ident)
            return ident

    def incorporar(
        self, sesion: UUID, ident: UUID, usuario: UUID, rol: str = "MIEMBRO",
    ) -> None:
        """Sólo un administrador miembro con permiso incorpora participantes internos."""
        if rol not in ("MIEMBRO", "ADMINISTRADOR"):
            raise ValidationError("rol de miembro inválido")
        with self._aut.operacion(sesion) as (tx, _):
            p, _, miembros = self._acceso(tx, sesion, ident, gestionar=True)
            dto = self._usuario_habilitado(tx, usuario)
            if dto.tercero_id is not None:
                raise PermissionDeniedError("grupos y canales no admiten externos")
            if any(m.usuario_id == usuario and m.vigente for m in miembros):
                raise ValidationError("el usuario ya es miembro vigente")
            self._repo.incorporar(tx, ident, usuario, rol)
            auditoria.registrar(
                tx, "conversacion.incorporar", p, entidad="conversacion", entidad_id=ident,
                participante_id=str(usuario), rol=rol,
            )

    def unirse(self, sesion: UUID, ident: UUID) -> None:
        """Un canal público sólo se descubre internamente; leer exige incorporación explícita."""
        with self._aut.operacion(sesion) as (tx, p):
            conv = self._repo.obtener(tx, ident)
            p = self._aut.principal(tx, sesion)
            if not p.es_interno or conv is None or conv.tipo != "PUBLICA" or conv.archivada_en:
                raise PermissionDeniedError("canal fuera del alcance interno")
            propios = [m for m in self._repo.miembros(tx, ident) if m.usuario_id == p.usuario_id]
            if any(m.vigente for m in propios):
                raise ValidationError("ya pertenece a este canal")
            if propios:
                raise PermissionDeniedError(
                    "reingreso al canal requiere administrador miembro",
                    user_message=(
                        "Su membresía fue cerrada. Solicite la reincorporación al administrador del canal."
                    ),
                )
            self._repo.incorporar(tx, ident, p.usuario_id, "MIEMBRO")
            auditoria.registrar(tx, "conversacion.unirse", p, entidad="conversacion", entidad_id=ident)

    def retirar(self, sesion: UUID, ident: UUID, usuario: UUID) -> None:
        """Cierra la vigencia sin borrar historia; privados conservan sus dos identidades."""
        with self._aut.operacion(sesion) as (tx, _):
            p, _, miembros = self._acceso(tx, sesion, ident, gestionar=True)
            if not any(m.usuario_id == usuario and m.vigente for m in miembros):
                raise NotFoundError("miembro no vigente")
            if usuario == p.usuario_id and not any(
                m.vigente and m.rol == "ADMINISTRADOR" and m.usuario_id != usuario for m in miembros
            ):
                raise ValidationError("no puede retirar al último administrador; archive el grupo")
            self._repo.retirar(tx, ident, usuario)
            auditoria.registrar(
                tx, "conversacion.retirar", p, entidad="conversacion", entidad_id=ident,
                participante_id=str(usuario),
            )

    def archivar(self, sesion: UUID, ident: UUID) -> None:
        """Cierra el grupo/canal para lectura y envío sin borrar documentos."""
        with self._aut.operacion(sesion) as (tx, _):
            p, _, _ = self._acceso(tx, sesion, ident, gestionar=True)
            self._repo.archivar(tx, ident)
            auditoria.registrar(tx, "conversacion.archivar", p, entidad="conversacion", entidad_id=ident)

    def enviar(self, sesion: UUID, ident: UUID, mensaje_id: UUID, contenido: str) -> MensajeDTO:
        """Mongo confirma primero. Una falla posterior no borra el mensaje ni confirma éxito en UI."""
        texto_validado(contenido)
        if not isinstance(mensaje_id, UUID):
            raise ValidationError("el mensaje requiere UUID estable")
        with self._aut.operacion(sesion) as (tx, _):
            contexto = self._acceso(tx, sesion, ident)
            p, _, miembros = contexto
            ingreso = max(m.ingreso_en for m in miembros if m.usuario_id == p.usuario_id and m.vigente)
            fecha = techo_milisegundo(max(self._repo.reloj(tx), ingreso))
            mensaje = self._mensajes.guardar(MensajeDTO(
                mensaje_id, ident, p.usuario_id, fecha, contenido
            ))
            if self._acceso(tx, sesion, ident) != contexto:
                raise PermissionDeniedError("acceso revocado durante envío; persistencia a reconciliar")
            auditoria.registrar(
                tx, "mensaje.confirmado_mongo", p, entidad="mensaje", entidad_id=mensaje.id,
                conversacion_id=str(ident),
            )
        self._validar_resultado(sesion, ident, contexto)
        return mensaje

    def historial(
        self, sesion: UUID, ident: UUID, *, antes: CursorMensaje | None = None,
        hasta: datetime | None = None, limite: int = 50,
    ) -> PaginaMensajesDTO:
        """Sólo períodos de pertenencia, corte temporal fijo y orden fecha/UUID descendente."""
        if not 1 <= limite <= 100:
            raise ValidationError("el tamaño de página debe estar entre 1 y 100")
        if (hasta is not None and hasta.tzinfo is None) or (
            antes is not None and (antes[0].tzinfo is None or not isinstance(antes[1], UUID))
        ):
            raise ValidationError("cursor inválido o fecha sin zona")
        with self._aut.operacion(sesion) as (tx, _):
            contexto = self._acceso(tx, sesion, ident)
            p, _, miembros = contexto
            ahora = techo_milisegundo(self._repo.reloj(tx))
            corte = min(hasta, ahora) if hasta else ahora
            propios = tuple(m for m in miembros if m.usuario_id == p.usuario_id)
        mensajes = self._mensajes.historial(ident, propios, corte, antes, limite + 1)
        self._validar_resultado(sesion, ident, contexto)
        pagina = mensajes[:limite]
        siguiente = (pagina[-1].fecha_envio, pagina[-1].id) if len(mensajes) > limite else None
        return PaginaMensajesDTO(pagina, siguiente, corte, miembros)

    def vista(
        self, sesion: UUID, ident: UUID | None = None, *, antes: CursorMensaje | None = None,
        hasta: datetime | None = None,
    ) -> VistaChatDTO:
        """Una vuelta de polling; una sesión por operación, nunca conexiones compartidas entre hilos."""
        with self._aut.operacion(sesion) as (_, inicial):
            pass
        pagina = self.historial(sesion, ident, antes=antes, hasta=hasta) if ident else None
        with self._aut.operacion(sesion) as (tx, p):
            if p != inicial:
                raise PermissionDeniedError("la autorización cambió durante el polling")
            if ident is not None and pagina is not None:
                _, _, miembros = self._acceso(tx, sesion, ident, bloquear=False)
                if miembros != pagina.membresias:
                    raise PermissionDeniedError("la membresía cambió durante el polling")
            conversaciones = self._repo.listar(tx, p.usuario_id)
            publicas = self._repo.listar(tx, p.usuario_id, publicas=True) if p.es_interno else []
            visibles = []
            for conv in conversaciones:
                try:
                    self._acceso(tx, sesion, conv.id, bloquear=False)
                except PermissionDeniedError:
                    log.info("Conversación fuera del alcance actual %s", conv.id)
                    continue
                visibles.append(conv)
        return VistaChatDTO(tuple(visibles), tuple(publicas), pagina)
