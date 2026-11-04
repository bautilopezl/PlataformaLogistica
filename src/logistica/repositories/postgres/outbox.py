"""Repositorio de la outbox (tablas `outbox_evento` y `outbox_entrega`).

El servicio registra el evento EN LA MISMA transacción que el cambio de negocio: si el cambio se confirma,
el evento también, y un trabajador (fase 3) lo entregará a cada destino al menos una vez. El `id` del evento
es el `_id` del documento en Mongo, así una reentrega no duplica.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.logistica_dtos import EventoOutboxDTO

NEO4J = "NEO4J_LOGISTICA"
MONGO = "MONGO_EVENTOS"


class OutboxRepository:
    def registrar(
        self,
        tx: Session,
        *,
        agregado_tipo: str,
        agregado_id: uuid.UUID,
        agregado_version: int,
        evento_tipo: str,
        payload: dict[str, Any],
        destinos: tuple[str, ...],
    ) -> uuid.UUID:
        evento_id = uuid.uuid4()
        tx.execute(
            text(
                "INSERT INTO outbox_evento (id, agregado_tipo, agregado_id, agregado_version, evento_tipo, "
                "payload)"
                " VALUES (:id, :tipo, :agregado, :version, :evento, CAST(:payload AS jsonb))"
            ),
            {
                "id": evento_id,
                "tipo": agregado_tipo,
                "agregado": agregado_id,
                "version": agregado_version,
                "evento": evento_tipo,
                "payload": json.dumps({"event_id": str(evento_id), **payload}, default=_json),
            },
        )
        for destino in destinos:
            tx.execute(
                text("INSERT INTO outbox_entrega (evento_id, destino) VALUES (:id, :destino)"),
                {"id": evento_id, "destino": destino},
            )
        return evento_id

    def de_agregado(self, tx: Session, agregado_id: uuid.UUID) -> list[EventoOutboxDTO]:
        rows = tx.execute(
            text(
                """
                SELECT e.id, e.evento_tipo, e.agregado_version, e.creado_en, e.payload ->> 'tipo' AS tipo,
                       bool_or(x.estado <> 'COMPLETADA') AS pendiente
                FROM outbox_evento e JOIN outbox_entrega x ON x.evento_id = e.id
                WHERE e.agregado_id = :id
                GROUP BY e.id
                ORDER BY e.agregado_version
                """
            ),
            {"id": agregado_id},
        )
        return [
            EventoOutboxDTO(r.id, r.evento_tipo, r.agregado_version, r.creado_en, r.tipo, r.pendiente)
            for r in rows
        ]

    def estados_mongo(self, tx: Session, evento_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Lee únicamente el estado de sincronización de eventos ya consultados en Mongo."""
        if not evento_ids:
            return {}
        rows = tx.execute(
            text(
                "SELECT x.evento_id, x.estado FROM outbox_entrega x"
                " WHERE x.destino = :destino AND x.evento_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"destino": MONGO, "ids": evento_ids},
        )
        return {r.evento_id: r.estado for r in rows}

    def resumen_mongo_envio(
        self, tx: Session, envio_id: uuid.UUID, *, tramo_ids: list[uuid.UUID] | None = None
    ) -> dict[str, int]:
        """Resume la entrega de Mongo sin usar la outbox como fuente del historial."""
        filas = tx.execute(
            text(
                """
                SELECT x.estado, count(*)
                FROM outbox_evento e
                JOIN outbox_entrega x ON x.evento_id = e.id
                WHERE x.destino = :destino
                  AND (e.agregado_id = :envio OR e.payload ->> 'envio_id' = CAST(:envio AS text))
                  AND (
                    CAST(:tramos AS text[]) IS NULL
                    OR e.payload ->> 'tramo_id' IS NULL
                    OR e.payload ->> 'tramo_id' = ANY(CAST(:tramos AS text[]))
                  )
                GROUP BY x.estado
                """
            ),
            {
                "destino": MONGO,
                "envio": envio_id,
                "tramos": [str(tramo_id) for tramo_id in tramo_ids] if tramo_ids is not None else None,
            },
        )
        resultado = dict.fromkeys(ESTADOS_ENTREGA, 0)
        resultado.update({estado: cantidad for estado, cantidad in filas})
        return resultado

    # ------------------------------------------------------------------ consumo (fase 3)
    def reclamar(
        self,
        tx: Session,
        destino: str,
        *,
        propietario_id: uuid.UUID,
        limite: int,
        reserva_s: float,
        agregado_id: uuid.UUID | None = None,
    ) -> list[EntregaReclamada]:
        """Reclama atómicamente hasta `limite` entregas listas para `destino`.

        Lista = PENDIENTE con su próximo intento vencido, o EN_PROCESO con la reserva vencida (el trabajador
        que la tenía se colgó o se cerró). `FOR UPDATE SKIP LOCKED`: dos trabajadores a la vez se reparten
        entregas distintas sin esperarse. Cada reclamo suma 1 a `version_reclamo` e `intentos`.
        `agregado_id` limita el lote a un envío (diagnóstico y pruebas).
        """
        rows = tx.execute(
            text(
                """
                -- Las filas se eligen en un CTE (se evalúa UNA vez). Con
                -- "IN (SELECT ... LIMIT ... FOR UPDATE SKIP LOCKED)" el planificador puede reevaluar la
                -- subconsulta y reclamar más filas que el límite.
                WITH elegidas AS (
                    SELECT y.evento_id, y.destino
                    FROM outbox_entrega y JOIN outbox_evento ev ON ev.id = y.evento_id
                    WHERE y.destino = :destino
                      AND (CAST(:agregado AS uuid) IS NULL OR ev.agregado_id = :agregado)
                      AND ((y.estado = 'PENDIENTE' AND y.proximo_intento_en <= clock_timestamp())
                           OR (y.estado = 'EN_PROCESO' AND y.bloqueada_hasta < clock_timestamp()))
                    ORDER BY ev.agregado_id, ev.agregado_version
                    LIMIT :limite
                    FOR UPDATE OF y SKIP LOCKED
                )
                UPDATE outbox_entrega x
                SET estado = 'EN_PROCESO', propietario_id = :propietario,
                    version_reclamo = x.version_reclamo + 1, intentos = x.intentos + 1,
                    bloqueada_hasta = clock_timestamp() + make_interval(secs => :reserva)
                FROM elegidas el, outbox_evento e
                WHERE x.evento_id = el.evento_id AND x.destino = el.destino AND e.id = x.evento_id
                RETURNING x.evento_id, x.destino, e.agregado_id, e.agregado_version, e.evento_tipo, e.payload,
                          e.creado_en, x.version_reclamo, x.intentos
                """
            ),
            {
                "propietario": propietario_id,
                "reserva": reserva_s,
                "destino": destino,
                "agregado": agregado_id,
                "limite": limite,
            },
        )
        entregas = [EntregaReclamada(*r) for r in rows]
        return sorted(entregas, key=lambda x: (str(x.agregado_id), x.agregado_version))

    def completar(
        self, tx: Session, evento_id: uuid.UUID, destino: str, propietario_id: uuid.UUID, version_reclamo: int
    ) -> bool:
        """Marca COMPLETADA solo si el reclamo sigue siendo de este trabajador (mismo dueño y versión).

        Si la reserva venció y otro la recuperó, devuelve False: el otro es quien la completa.
        """
        return (
            tx.execute(
                text(
                    "UPDATE outbox_entrega SET estado = 'COMPLETADA', procesada_en = clock_timestamp(),"
                    " bloqueada_hasta = NULL, ultimo_error = NULL"
                    " WHERE evento_id = :e AND destino = :d AND estado = 'EN_PROCESO'"
                    " AND propietario_id = :p AND version_reclamo = :v"
                ),
                {"e": evento_id, "d": destino, "p": propietario_id, "v": version_reclamo},
            ).rowcount
            == 1
        )

    def registrar_fallo(
        self,
        tx: Session,
        evento_id: uuid.UUID,
        destino: str,
        propietario_id: uuid.UUID,
        version_reclamo: int,
        error: str,
        max_intentos: int,
    ) -> str | None:
        """Devuelve el estado nuevo: PENDIENTE (reintento con espera creciente) o FALLIDA (se agotaron).

        Espera = 2^intentos segundos, con tope de 5 minutos. Una entrega FALLIDA no se borra: queda en la
        cola con su causa para revisarla (AGENTS.md §9).
        """
        return tx.execute(
            text(
                """
                UPDATE outbox_entrega
                SET estado = CASE WHEN intentos >= :max THEN 'FALLIDA' ELSE 'PENDIENTE' END,
                    proximo_intento_en = clock_timestamp()
                                         + make_interval(secs => least(power(2, intentos), 300)),
                    bloqueada_hasta = NULL, propietario_id = NULL, ultimo_error = :error
                WHERE evento_id = :e AND destino = :d AND estado = 'EN_PROCESO'
                  AND propietario_id = :p AND version_reclamo = :v
                RETURNING estado
                """
            ),
            {
                "e": evento_id,
                "d": destino,
                "p": propietario_id,
                "v": version_reclamo,
                "max": max_intentos,
                "error": error[:2000],
            },
        ).scalar()

    def liberar(
        self,
        tx: Session,
        evento_id: uuid.UUID,
        destino: str,
        propietario_id: uuid.UUID,
        version_reclamo: int,
        error: str,
        espera_s: float,
    ) -> bool:
        """Devuelve la entrega a PENDIENTE SIN gastar el intento: el destino no respondía (no es culpa del
        evento). Así una caída larga de Neo4j o Mongo nunca vuelve FALLIDAS a las entregas (AGENTS.md §9)."""
        return (
            tx.execute(
                text(
                    "UPDATE outbox_entrega SET estado = 'PENDIENTE', intentos = greatest(intentos - 1, 0),"
                    " proximo_intento_en = clock_timestamp() + make_interval(secs => :espera),"
                    " bloqueada_hasta = NULL, propietario_id = NULL, ultimo_error = :error"
                    " WHERE evento_id = :e AND destino = :d AND estado = 'EN_PROCESO'"
                    " AND propietario_id = :p AND version_reclamo = :v"
                ),
                {
                    "e": evento_id,
                    "d": destino,
                    "p": propietario_id,
                    "v": version_reclamo,
                    "espera": espera_s,
                    "error": error[:2000],
                },
            ).rowcount
            == 1
        )

    def reintentar_fallidas(self, tx: Session, destino: str | None = None) -> int:
        """Vuelve a encolar las entregas FALLIDAS (por ejemplo, después de corregir la causa)."""
        return tx.execute(
            text(
                "UPDATE outbox_entrega SET estado = 'PENDIENTE', intentos = 0,"
                " proximo_intento_en = clock_timestamp()"
                " WHERE estado = 'FALLIDA' AND (CAST(:d AS text) IS NULL OR destino = :d)"
            ),
            {"d": destino},
        ).rowcount

    def resumen(self, tx: Session, agregado_id: uuid.UUID | None = None) -> dict[str, dict[str, int]]:
        """Cantidad de entregas por destino y estado."""
        rows = tx.execute(
            text(
                "SELECT x.destino, x.estado, count(*) FROM outbox_entrega x"
                " JOIN outbox_evento e ON e.id = x.evento_id"
                " WHERE CAST(:agregado AS uuid) IS NULL OR e.agregado_id = :agregado GROUP BY 1, 2"
            ),
            {"agregado": agregado_id},
        )
        resultado = {d: dict.fromkeys(ESTADOS_ENTREGA, 0) for d in (NEO4J, MONGO)}
        for destino, estado, n in rows:
            resultado.setdefault(destino, dict.fromkeys(ESTADOS_ENTREGA, 0))[estado] = n
        return resultado


ESTADOS_ENTREGA = ("PENDIENTE", "EN_PROCESO", "COMPLETADA", "FALLIDA")


@dataclass(frozen=True)
class EntregaReclamada:
    evento_id: uuid.UUID
    destino: str
    agregado_id: uuid.UUID
    agregado_version: int
    evento_tipo: str
    payload: dict[str, Any]
    creado_en: datetime
    version_reclamo: int
    intentos: int


def _json(valor: Any) -> str:
    """Fechas en ISO 8601 con zona (las lee `datetime.fromisoformat`); UUID y Decimal como texto."""
    if isinstance(valor, datetime):
        if valor.tzinfo is None:
            raise ValueError("fecha sin zona horaria en un evento de outbox")
        return valor.isoformat()
    return str(valor)
