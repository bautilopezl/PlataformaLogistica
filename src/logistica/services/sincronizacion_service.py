"""Consumidores de la outbox: histórico en MongoDB y proyección en Neo4j (AGENTS.md §9).

Garantías:
- Entrega al menos una vez: una entrega solo se marca COMPLETADA después de aplicarse en su destino; si el
  trabajador se cae antes, la reserva vence y otro la retoma.
- Consumidores idempotentes: Mongo inserta por `_id = event_id` ($setOnInsert); Neo4j aplica con MERGE por id.
- Sin retrocesos: el grafo guarda la versión del envío y no aplica estados más viejos. Para Neo4j el
  evento solo avisa QUÉ envío cambió; el subgrafo se recalcula desde PostgreSQL (fuente principal),
  así eventos repetidos o desordenados convergen al mismo resultado.
- Fallas: reintento con espera creciente; agotados los intentos, FALLIDA (no se borra; queda para revisar).

Este servicio no tiene sesión de usuario: corre con la identidad técnica (PG_APP_USER), como un proceso.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from logistica.core.errors import DatabaseUnavailableError, LogisticaError
from logistica.core.logging_setup import redact
from logistica.domain.grafo import EnvioGrafo, armar_envio_grafo
from logistica.repositories.connections import Connections
from logistica.repositories.mongo.eventos import EventosRepository, documento_evento
from logistica.repositories.neo4j.proyeccion import ProyeccionRepository
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.catalogos import UbicacionRepository
from logistica.repositories.postgres.envios import AsignacionRepository, EnvioRepository, TramoRepository
from logistica.repositories.postgres.outbox import (
    FACTURACION,
    MONGO,
    NEO4J,
    EntregaReclamada,
    OutboxRepository,
)

log = logging.getLogger(__name__)

# SUPUESTO (AGENTS.md §16.6): valores iniciales, a medir en el entorno de demostración.
MAX_INTENTOS = 8
RESERVA_S = 120  # debe cubrir el lote completo: LOTE x timeout de un destino lento
LOTE = 20
ESPERA_CAIDA_S = 30  # cuánto esperar para reintentar cuando el destino no responde


@dataclass(frozen=True)
class ResultadoLote:
    procesados: int = 0
    fallidos: int = 0
    omitidos: int = 0  # reclamos perdidos (la reserva venció y otro trabajador los tomó)
    pospuestos: int = 0  # el destino no respondía: se reintentan sin gastar intentos


class SincronizacionService:
    def __init__(
        self,
        connections: Connections,
        *,
        neo4j: Connections | None = None,
        max_intentos: int = MAX_INTENTOS,
        reserva_s: float = RESERVA_S,
        facturacion: Callable[[Any, uuid.UUID, uuid.UUID | None, uuid.UUID], Any] | None = None,
    ):
        """`facturacion(tx, envio_id, usuario_id, evento_id)`: facturación automática (fase 6). Las entregas
        FACTURACION las consume el planificador de procesos (fase 8) con `procesar(FACTURACION)`; sin
        `facturacion` configurada quedan pendientes (no se pierden)."""
        self._pg = connections
        self._facturacion = facturacion
        self._outbox = OutboxRepository()
        self._eventos = EventosRepository(connections)
        self._proyeccion = ProyeccionRepository(neo4j or connections)
        self._envios = EnvioRepository()
        self._tramos = TramoRepository()
        self._asignaciones = AsignacionRepository()
        self._ubicaciones = UbicacionRepository()
        self._max_intentos = max_intentos
        self._reserva_s = reserva_s
        self.propietario_id = uuid.uuid4()  # identifica a este trabajador en los reclamos

    # ------------------------------------------------------------------ consumo
    def procesar_todo(self, *, limite: int = LOTE) -> dict[str, ResultadoLote]:
        """Una vuelta por cada destino de PROYECCIÓN (Mongo y Neo4j). Un error en uno no impide el otro.

        La facturación (destino FACTURACION) no está acá: es un proceso de negocio y la corre el planificador
        de la fase 8, bajo su advisory lock."""
        resultados = {}
        for destino in (MONGO, NEO4J):
            try:
                resultados[destino] = self.procesar(destino, limite=limite)
            except Exception:  # por ejemplo, PostgreSQL caído: se reintenta en la próxima vuelta
                log.exception("No se pudo procesar la outbox de %s", destino)
                resultados[destino] = ResultadoLote()
        return resultados

    def procesar(
        self, destino: str, *, limite: int = LOTE, agregado_id: uuid.UUID | None = None
    ) -> ResultadoLote:
        """Reclama un lote de `destino` y lo aplica. `agregado_id` limita a un envío (diagnóstico y pruebas).

        Tipos de falla:
        - el destino no responde (DatabaseUnavailableError): se liberan esta entrega y el resto del lote SIN
          gastar intentos y se corta la vuelta (no tiene sentido seguir ahora);
        - el evento no se puede aplicar (validación, restricción, o cualquier error inesperado): cuenta un
          intento para ESA entrega y se sigue con las demás. Agotados los intentos queda FALLIDA.
        """
        with unit_of_work(self._pg) as tx:
            entregas = self._outbox.reclamar(
                tx,
                destino,
                propietario_id=self.propietario_id,
                limite=limite,
                reserva_s=self._reserva_s,
                agregado_id=agregado_id,
            )
        procesados = fallidos = omitidos = pospuestos = 0
        aplicados: set[uuid.UUID] = set()  # Neo4j: un envío se proyecta una vez por lote
        for i, entrega in enumerate(entregas):
            try:
                completada = self._aplicar(entrega, aplicados)
            except DatabaseUnavailableError as exc:
                for pendiente in entregas[i:]:
                    self._liberar(pendiente, exc)
                pospuestos = len(entregas) - i
                log.warning(
                    "Destino %s no disponible; %d entregas se reintentarán en %d s",
                    destino,
                    len(entregas) - i,
                    ESPERA_CAIDA_S,
                )
                break
            except Exception as exc:  # error del EVENTO: cuenta un intento y se sigue con el resto
                if not isinstance(exc, (LogisticaError, ValueError, KeyError)):
                    log.exception("Error inesperado aplicando %s/%s", entrega.evento_id, entrega.destino)
                self._fallar(entrega, exc)
                fallidos += 1
            else:
                if completada:
                    procesados += 1
                else:
                    omitidos += 1
        if entregas:
            log.info(
                "Outbox %s: %d procesados, %d fallidos, %d omitidos", destino, procesados, fallidos, omitidos
            )
        return ResultadoLote(procesados, fallidos, omitidos, pospuestos)

    def _aplicar(self, entrega: EntregaReclamada, aplicados: set[uuid.UUID]) -> bool:
        if entrega.destino == MONGO:
            # fecha_registro = cuándo se registró el evento (estable: una reentrega da el mismo documento).
            self._eventos.guardar(documento_evento(entrega.payload, fecha_registro=entrega.creado_en))
            with unit_of_work(self._pg) as tx:
                return self._completar(tx, entrega)
        if entrega.destino == NEO4J:
            if entrega.agregado_id not in aplicados:
                # 1) Foto consistente del envío (FOR SHARE) en una transacción corta: el lock se suelta ANTES
                #    de escribir en Neo4j, así un Neo4j lento no frena las operaciones de los usuarios.
                with unit_of_work(self._pg) as tx:
                    grafo = self._grafo_de(tx, entrega.agregado_id)
                # 2) Proyección. Si mientras tanto el envío cambió, su propio evento la actualiza después
                #    (la guarda de versión impide que esta foto pise una más nueva).
                if grafo is not None:
                    self._proyeccion.aplicar(grafo)  # False = ya estaba al día: igual se completa
                aplicados.add(entrega.agregado_id)
            # 3) Recién aplicado el efecto se marca la entrega (al menos una vez).
            with unit_of_work(self._pg) as tx:
                return self._completar(tx, entrega)
        if entrega.destino == FACTURACION and self._facturacion is not None:
            # Factura y entrega completada en UNA transacción: una reentrega o un segundo trabajador nunca
            # facturan dos veces (además, los cargos se bloquean y un cargo no entra en dos facturas).
            with unit_of_work(self._pg) as tx:
                if not self._completar(tx, entrega):
                    return False  # el reclamo ya no es nuestro: no se factura
                envio_id = uuid.UUID(str(entrega.payload["envio_id"]))
                usuario = entrega.payload.get("usuario_id")
                self._facturacion(
                    tx, envio_id, uuid.UUID(str(usuario)) if usuario else None, entrega.evento_id
                )
                return True
        raise ValueError(f"destino desconocido: {entrega.destino}")

    def _completar(self, tx: Any, entrega: EntregaReclamada) -> bool:
        ok = self._outbox.completar(
            tx, entrega.evento_id, entrega.destino, self.propietario_id, entrega.version_reclamo
        )
        if not ok:
            log.info(
                "Reclamo perdido de %s/%s (lo completa otro trabajador)", entrega.evento_id, entrega.destino
            )
        return ok

    def _fallar(self, entrega: EntregaReclamada, exc: Exception) -> None:
        error = redact(f"{type(exc).__name__}: {exc}")
        with unit_of_work(self._pg) as tx:
            estado = self._outbox.registrar_fallo(
                tx,
                entrega.evento_id,
                entrega.destino,
                self.propietario_id,
                entrega.version_reclamo,
                error,
                self._max_intentos,
            )
        nivel = logging.ERROR if estado == "FALLIDA" else logging.WARNING
        log.log(
            nivel,
            "Entrega %s/%s (%s, intento %d) -> %s: %s",
            entrega.evento_id,
            entrega.destino,
            entrega.evento_tipo,
            entrega.intentos,
            estado,
            error,
        )

    def _liberar(self, entrega: EntregaReclamada, exc: Exception) -> None:
        error = redact(f"{type(exc).__name__}: {exc}")
        with unit_of_work(self._pg) as tx:
            self._outbox.liberar(
                tx,
                entrega.evento_id,
                entrega.destino,
                self.propietario_id,
                entrega.version_reclamo,
                error,
                ESPERA_CAIDA_S,
            )

    def reintentar_fallidas(self, destino: str | None = None) -> int:
        """Vuelve a encolar las entregas FALLIDAS (de un destino o de todos). Devuelve cuántas."""
        with unit_of_work(self._pg) as tx:
            n = self._outbox.reintentar_fallidas(tx, destino)
        log.info("Reencoladas %d entregas fallidas (%s)", n, destino or "todos los destinos")
        return n

    def _grafo_de(self, tx: Any, envio_id: uuid.UUID) -> EnvioGrafo | None:
        envio = self._envios.obtener(tx, envio_id, compartir=True)  # D1: todo de un mismo estado
        if envio is None:
            return None
        tramos = self._tramos.de_envio(tx, envio_id)
        ids = (
            {envio.origen_id, envio.destino_id}
            | {t.origen_id for t in tramos}
            | {t.destino_id for t in tramos}
        )
        # Orden fijo: dos trabajadores toman los locks de Ubicacion en el mismo orden (sin deadlocks).
        ubicaciones = {i: self._ubicaciones.obtener(tx, i) for i in sorted(ids, key=str)}
        return armar_envio_grafo(envio, tramos, self._asignaciones.de_envio(tx, envio_id), ubicaciones)

    def reconstruir_grafo(self, *, envio_ids: list[uuid.UUID] | None = None) -> int:
        """Vuelve a proyectar los envíos desde PostgreSQL (todos, o los indicados). Devuelve cuántos.

        Cada envío se aplica permitiendo la MISMA versión (el grafo pudo perderse o dañarse), nunca una más
        vieja: si mientras tanto un trabajador proyectó una versión nueva, no se pisa. En la reconstrucción
        completa se borran solo los envíos del grafo que no existen en PostgreSQL, consultado en el momento
        de borrar (los envíos nunca se borran físicamente en PostgreSQL: lo que falta ahí es basura).
        """
        with unit_of_work(self._pg) as tx:
            ids = envio_ids if envio_ids is not None else self._envios.ids(tx)
            grafos = [g for g in (self._grafo_de(tx, i) for i in ids) if g is not None]
        for grafo in grafos:
            self._proyeccion.aplicar(grafo, forzar=True)
        if envio_ids is None:
            candidatos = self._proyeccion.envios_proyectados()
            with unit_of_work(self._pg) as tx:
                existentes = self._envios.existentes(tx, candidatos)
            borrados = self._proyeccion.borrar_envios([c for c in candidatos if c not in existentes])
            if borrados:
                log.warning(
                    "Reconstrucción: %d envíos del grafo no existen en PostgreSQL y se borraron", borrados
                )
        log.info("Grafo reconstruido: %d envíos", len(grafos))
        return len(grafos)

    def resumen(self, *, agregado_id: uuid.UUID | None = None) -> dict[str, dict[str, int]]:
        """Entregas por destino y estado (para mostrar el estado de la sincronización)."""
        with unit_of_work(self._pg) as tx:
            return self._outbox.resumen(tx, agregado_id)
