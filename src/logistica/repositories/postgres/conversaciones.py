"""Conversaciones y períodos de pertenencia; serialización por conversación."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.comunicacion import ConversacionDTO, ParticipanteDTO


class ConversacionesRepository:
    def reloj(self, tx: Session) -> datetime:
        return tx.execute(text("SELECT clock_timestamp()")).scalar_one()

    def obtener(
        self, tx: Session, ident: UUID, *, bloquear: bool = True,
    ) -> ConversacionDTO | None:
        if bloquear:
            tx.execute(text("SELECT set_config('lock_timeout', '5000', true)"))
        consulta = "SELECT * FROM conversacion WHERE id=:id"
        if bloquear:
            consulta += " FOR UPDATE"
        row = tx.execute(
            text(consulta), {"id": ident}
        ).one_or_none()
        return self._dto(row) if row else None

    @staticmethod
    def _dto(row) -> ConversacionDTO:
        return ConversacionDTO(
            row.id, row.tipo, row.nombre or "Conversación privada", row.creada_por_id, row.archivada_en
        )

    def listar(self, tx: Session, usuario: UUID, *, publicas: bool = False) -> list[ConversacionDTO]:
        rows = tx.execute(
            text("""
                SELECT c.* FROM conversacion c WHERE c.archivada_en IS NULL AND
                ((:publicas AND c.tipo='PUBLICA') OR
                 (NOT :publicas AND EXISTS (
                    SELECT 1 FROM conversacion_miembro m WHERE m.conversacion_id=c.id
                    AND m.usuario_id=:usuario AND m.ingreso_en<=clock_timestamp()
                    AND (m.salida_en IS NULL OR m.salida_en>clock_timestamp()))))
                ORDER BY c.creado_en DESC, c.id DESC
            """),
            {"usuario": usuario, "publicas": publicas},
        )
        return [self._dto(row) for row in rows]

    def miembros(self, tx: Session, ident: UUID) -> tuple[ParticipanteDTO, ...]:
        rows = tx.execute(
            text("""
                SELECT m.*, u.nombre || ' ' || u.apellido AS nombre,
                       m.ingreso_en<=clock_timestamp() AND
                       (m.salida_en IS NULL OR m.salida_en>clock_timestamp()) AS vigente
                FROM conversacion_miembro m JOIN usuario u ON u.id=m.usuario_id
                WHERE m.conversacion_id=:id ORDER BY m.ingreso_en, m.id
            """),
            {"id": ident},
        )
        return tuple(
            ParticipanteDTO(r.usuario_id, r.nombre, r.rol, r.ingreso_en, r.salida_en, r.vigente)
            for r in rows
        )

    def crear(self, tx: Session, ident: UUID, tipo: str, nombre: str, usuario: UUID) -> None:
        tx.execute(
            text("""
                INSERT INTO conversacion(id,tipo,nombre,creada_por_id)
                VALUES (:id,:tipo,:nombre,:usuario)
            """),
            {"id": ident, "tipo": tipo, "nombre": nombre or None, "usuario": usuario},
        )

    def incorporar(self, tx: Session, ident: UUID, usuario: UUID, rol: str) -> None:
        tx.execute(
            text("""
                INSERT INTO conversacion_miembro(id,conversacion_id,usuario_id,rol,ingreso_en)
                VALUES (:id,:conv,:usuario,:rol,clock_timestamp())
            """),
            {"id": uuid4(), "conv": ident, "usuario": usuario, "rol": rol},
        )

    def retirar(self, tx: Session, ident: UUID, usuario: UUID) -> None:
        tx.execute(
            text("""
                UPDATE conversacion_miembro SET salida_en=greatest(clock_timestamp(),
                    ingreso_en + interval '1 microsecond')
                WHERE conversacion_id=:id AND usuario_id=:usuario
                    AND ingreso_en<=clock_timestamp()
                    AND (salida_en IS NULL OR salida_en>clock_timestamp())
            """),
            {"id": ident, "usuario": usuario},
        )

    def archivar(self, tx: Session, ident: UUID) -> None:
        tx.execute(
            text("UPDATE conversacion SET archivada_en=clock_timestamp() WHERE id=:id"), {"id": ident}
        )
