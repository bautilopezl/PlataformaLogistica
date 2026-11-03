"""Repositorios de monitoreo en PostgreSQL: condiciones, contexto al medir, estado de sensores, alertas,
incidentes y la marca de idempotencia `lectura_procesada`.

`lectura_procesada` está preparada para particionarse (AGENTS.md §8.2): no se le agregan FK ni se cambia su PK
(lectura_id, version_reglas), y la aplicación no tiene DELETE sobre ella.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.monitoreo import AlertaActiva, Condicion, Contexto, EstadoSensor
from logistica.domain.monitoreo_dtos import AlertaDTO, CondicionDatos, EstadoSensorDTO, IncidenteDTO
from logistica.repositories.postgres.envios import AlcanceConsulta


def ahora(tx: Session) -> datetime:
    """Reloj de referencia común a todas las instancias: el de PostgreSQL (AGENTS.md §10.4)."""
    return tx.execute(text("SELECT clock_timestamp()")).scalar_one()


# --------------------------------------------------------------------------- condiciones
class CondicionRepository:
    def obtener(self, tx: Session, envio_id: uuid.UUID) -> Condicion | None:
        r = tx.execute(
            text("SELECT temperatura_min_c, temperatura_max_c, humedad_min_pct, humedad_max_pct, vibracion_max_g,"
                 " bateria_min_pct, tiempo_max_sin_lecturas_seg, version_reglas, reglas_evaluacion"
                 " FROM condicion_monitoreo WHERE envio_id = :id"),
            {"id": envio_id},
        ).one_or_none()
        return Condicion(*r[:8], reglas=dict(r[8])) if r else None

    def guardar(self, tx: Session, envio_id: uuid.UUID, d: CondicionDatos, version_reglas: str,
                reglas: dict[str, Any]) -> None:
        tx.execute(
            text(
                """
                INSERT INTO condicion_monitoreo (id, envio_id, temperatura_min_c, temperatura_max_c, humedad_min_pct,
                    humedad_max_pct, vibracion_max_g, bateria_min_pct, tiempo_max_sin_lecturas_seg, version_reglas,
                    reglas_evaluacion)
                VALUES (:id, :envio, :tmin, :tmax, :hmin, :hmax, :vmax, :bmin, :tiempo, :version, CAST(:reglas AS jsonb))
                ON CONFLICT (envio_id) DO UPDATE SET temperatura_min_c = EXCLUDED.temperatura_min_c,
                    temperatura_max_c = EXCLUDED.temperatura_max_c, humedad_min_pct = EXCLUDED.humedad_min_pct,
                    humedad_max_pct = EXCLUDED.humedad_max_pct, vibracion_max_g = EXCLUDED.vibracion_max_g,
                    bateria_min_pct = EXCLUDED.bateria_min_pct,
                    tiempo_max_sin_lecturas_seg = EXCLUDED.tiempo_max_sin_lecturas_seg,
                    version_reglas = EXCLUDED.version_reglas, reglas_evaluacion = EXCLUDED.reglas_evaluacion
                """
            ),
            {"id": uuid.uuid4(), "envio": envio_id, "tmin": d.temperatura_min_c, "tmax": d.temperatura_max_c,
             "hmin": d.humedad_min_pct, "hmax": d.humedad_max_pct, "vmax": d.vibracion_max_g,
             "bmin": d.bateria_min_pct, "tiempo": d.tiempo_max_sin_lecturas_seg, "version": version_reglas,
             "reglas": json.dumps(reglas)},
        )


# --------------------------------------------------------------------------- contexto al medir
class ContextoRepository:
    def al_medir(self, tx: Session, sensor_id: uuid.UUID, fecha: datetime) -> Contexto | None:
        """Envío/contenedor/tramo donde estaba el sensor EN EL INSTANTE de la medición (no al recibirla).

        SUPUESTO M7: el contenedor pertenece al envío mientras está EN USO (de uso_desde a uso_hasta); una
        lectura de un contenedor solo reservado, o de un sensor no instalado, no tiene contexto.
        """
        r = tx.execute(
            text(
                """
                SELECT sc.id AS asignacion, sc.contenedor_id, ec.envio_id,
                       (SELECT t.id FROM tramo t WHERE t.envio_id = ec.envio_id AND t.salida_real_en <= :f
                          AND (t.llegada_real_en IS NULL OR t.llegada_real_en > :f)
                        ORDER BY t.orden DESC LIMIT 1) AS tramo
                FROM sensor_contenedor sc
                JOIN envio_contenedor ec ON ec.contenedor_id = sc.contenedor_id
                     AND ec.estado IN ('EN_USO', 'FINALIZADA') AND ec.uso_desde <= :f
                     AND (ec.uso_hasta IS NULL OR ec.uso_hasta > :f)
                WHERE sc.sensor_id = :s AND sc.instalado_en <= :f AND (sc.retirado_en IS NULL OR sc.retirado_en > :f)
                LIMIT 1
                """
            ),
            {"s": sensor_id, "f": fecha},
        ).one_or_none()
        return Contexto(r.envio_id, r.contenedor_id, r.asignacion, r.tramo) if r else None


# --------------------------------------------------------------------------- estado del sensor
@dataclass(frozen=True)
class SensorEsperado:
    """Sensor que debería estar comunicándose: instalado en un contenedor EN USO de un envío EN TRÁNSITO."""

    sensor_id: uuid.UUID
    contexto: Contexto
    ultimo_contacto: datetime  # última medición evaluada, o el inicio del monitoreo si nunca llegó una


class EstadoSensorRepository:
    def existe(self, tx: Session, sensor_id: uuid.UUID) -> bool:
        return tx.execute(text("SELECT EXISTS (SELECT 1 FROM sensor WHERE id = :id)"), {"id": sensor_id}).scalar_one()

    def listar(self, tx: Session, *, alcance: AlcanceConsulta, envio_id: uuid.UUID | None,
               limite: int = 500) -> list[EstadoSensorDTO]:
        """Estado actual de los sensores para la UI. "Sin comunicación" se CALCULA acá, al consultar."""
        rows = tx.execute(
            text(
                """
                SELECT s.id, s.codigo, s.estado, k.codigo_internacional, e.id, e.codigo, sem.ultima_medicion_en,
                       CASE WHEN e.estado = 'EN_TRANSITO' AND ec.estado = 'EN_USO' THEN
                            extract(epoch FROM clock_timestamp()
                                    - greatest(ec.uso_desde, sc.instalado_en,
                                               coalesce(sem.ultima_medicion_en, '-infinity')))::int END,
                       coalesce(cm.tiempo_max_sin_lecturas_seg, 180),
                       (SELECT count(*) FROM alerta a WHERE a.sensor_id = s.id AND a.estado <> 'CERRADA')
                FROM sensor s
                LEFT JOIN sensor_contenedor sc ON sc.sensor_id = s.id AND sc.retirado_en IS NULL
                LEFT JOIN contenedor k ON k.id = sc.contenedor_id
                LEFT JOIN envio_contenedor ec ON ec.contenedor_id = sc.contenedor_id AND ec.estado = 'EN_USO'
                LEFT JOIN envio e ON e.id = ec.envio_id
                LEFT JOIN condicion_monitoreo cm ON cm.envio_id = e.id
                LEFT JOIN sensor_estado_monitoreo sem ON sem.sensor_id = s.id
                WHERE (CAST(:envio AS uuid) IS NULL OR e.id = :envio)
                  AND ((CAST(:cliente AS uuid) IS NULL AND CAST(:operador AS uuid) IS NULL)
                       OR e.cliente_id = :cliente
                       OR EXISTS (SELECT 1 FROM tramo t WHERE t.envio_id = e.id AND t.operador_id = :operador))
                ORDER BY s.codigo LIMIT :limite
                """
            ),
            {"envio": envio_id, "cliente": alcance.cliente_id, "operador": alcance.operador_id, "limite": limite},
        )
        return [EstadoSensorDTO(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                                r[7] is not None and r[7] > r[8], r[9]) for r in rows]

    def bloquear(self, tx: Session, sensor_id: uuid.UUID) -> bool:
        """Lock del sensor: dos evaluaciones del mismo sensor se aplican una después de la otra."""
        return tx.execute(text("SELECT 1 FROM sensor WHERE id = :id FOR UPDATE"), {"id": sensor_id}).first() is not None

    def obtener(self, tx: Session, sensor_id: uuid.UUID) -> EstadoSensor:
        r = tx.execute(text("SELECT evaluacion_condiciones FROM sensor_estado_monitoreo WHERE sensor_id = :id"),
                       {"id": sensor_id}).one_or_none()
        return EstadoSensor.desde_json(r[0]) if r else EstadoSensor.inicial()

    def guardar(self, tx: Session, sensor_id: uuid.UUID, estado: EstadoSensor, recepcion: datetime) -> None:
        tx.execute(
            text(
                """
                INSERT INTO sensor_estado_monitoreo (sensor_id, ultima_recepcion_en, ultima_medicion_en,
                    ultima_lectura_id, actualizado_en, evaluacion_condiciones)
                VALUES (:s, :rec, :med, :lid, clock_timestamp(), CAST(:j AS jsonb))
                ON CONFLICT (sensor_id) DO UPDATE SET ultima_recepcion_en = EXCLUDED.ultima_recepcion_en,
                    ultima_medicion_en = EXCLUDED.ultima_medicion_en, ultima_lectura_id = EXCLUDED.ultima_lectura_id,
                    actualizado_en = clock_timestamp(), evaluacion_condiciones = EXCLUDED.evaluacion_condiciones
                """
            ),
            {"s": sensor_id, "rec": recepcion, "med": estado.ultima_medicion_en, "lid": estado.ultima_lectura_id,
             "j": json.dumps(estado.a_json())},
        )

    def esperados(self, tx: Session, sensor_ids: list[uuid.UUID] | None = None) -> list[SensorEsperado]:
        """Sensores que deberían comunicarse ahora. Los que nunca enviaron nada cuentan desde que empezó el uso
        del contenedor (o desde la instalación, si fue después): se detectan desde el inicio del monitoreo."""
        rows = tx.execute(
            text(
                """
                SELECT sc.sensor_id, sc.id AS asignacion, sc.contenedor_id, ec.envio_id,
                       (SELECT t.id FROM tramo t WHERE t.envio_id = ec.envio_id AND t.estado = 'EN_CURSO'
                        ORDER BY t.orden DESC LIMIT 1) AS tramo,
                       greatest(ec.uso_desde, sc.instalado_en,
                                coalesce(sem.ultima_medicion_en, '-infinity'::timestamptz)) AS ultimo_contacto
                FROM sensor_contenedor sc
                JOIN envio_contenedor ec ON ec.contenedor_id = sc.contenedor_id AND ec.estado = 'EN_USO'
                JOIN envio e ON e.id = ec.envio_id AND e.estado = 'EN_TRANSITO'
                LEFT JOIN sensor_estado_monitoreo sem ON sem.sensor_id = sc.sensor_id
                WHERE sc.retirado_en IS NULL
                  AND (CAST(:ids AS uuid[]) IS NULL OR sc.sensor_id = ANY(CAST(:ids AS uuid[])))
                ORDER BY sc.sensor_id
                """
            ),
            {"ids": [str(i) for i in sensor_ids] if sensor_ids is not None else None},
        )
        return [SensorEsperado(r.sensor_id, Contexto(r.envio_id, r.contenedor_id, r.asignacion, r.tramo),
                               r.ultimo_contacto) for r in rows]


# --------------------------------------------------------------------------- idempotencia
class LecturaProcesadaRepository:
    def registrar(self, tx: Session, lectura_id: uuid.UUID, version_reglas: str) -> bool:
        """True si la marca se creó ahora; False si esa lectura ya se había evaluado con esa versión."""
        return tx.execute(
            text("INSERT INTO lectura_procesada (lectura_id, version_reglas) VALUES (:l, :v) ON CONFLICT DO NOTHING"),
            {"l": lectura_id, "v": version_reglas},
        ).rowcount == 1

    def existe(self, tx: Session, lectura_id: uuid.UUID, version_reglas: str) -> bool:
        return tx.execute(text("SELECT EXISTS (SELECT 1 FROM lectura_procesada WHERE lectura_id = :l"
                               " AND version_reglas = :v)"), {"l": lectura_id, "v": version_reglas}).scalar_one()


# --------------------------------------------------------------------------- alertas
def clave_alerta(tipo: str, sensor_id: uuid.UUID, envio_id: uuid.UUID) -> str:
    """Una sola alerta activa por (tipo, sensor, envío): las detecciones repetidas la actualizan."""
    return f"{tipo}:{sensor_id}:{envio_id}"


_SELECT_ALERTA = """
    SELECT a.id, a.envio_id, e.codigo, a.contenedor_id, k.codigo_internacional, a.tramo_id, a.sensor_id, s.codigo,
           a.tipo, a.severidad, a.estado, a.primera_deteccion_en, a.ultima_deteccion_en, a.normalizada_en,
           a.cerrada_en, a.cierre_tipo, a.motivo_cierre, a.reconocida_en, i.id AS incidente
    FROM alerta a
    JOIN envio e ON e.id = a.envio_id
    JOIN contenedor k ON k.id = a.contenedor_id
    LEFT JOIN sensor s ON s.id = a.sensor_id
    LEFT JOIN incidente i ON i.alerta_origen_id = a.id
"""
# Alcance: interno (ambos NULL) ve todo; cliente sus envíos; operador las de SUS tramos.
_ALCANCE_ALERTA = """
    ((CAST(:cliente AS uuid) IS NULL AND CAST(:operador AS uuid) IS NULL)
     OR e.cliente_id = :cliente
     OR EXISTS (SELECT 1 FROM tramo t WHERE t.id = a.tramo_id AND t.operador_id = :operador))
"""


class AlertaRepository:
    def activas(self, tx: Session, sensor_id: uuid.UUID, envio_id: uuid.UUID) -> dict[str, tuple[uuid.UUID, AlertaActiva]]:
        rows = tx.execute(
            text("SELECT a.id, a.tipo, a.severidad, EXISTS (SELECT 1 FROM incidente i WHERE i.alerta_origen_id = a.id)"
                 " FROM alerta a WHERE a.sensor_id = :s AND a.envio_id = :e AND a.estado IN ('ABIERTA', 'RECONOCIDA')"),
            {"s": sensor_id, "e": envio_id},
        )
        return {r[1]: (r[0], AlertaActiva(r[1], r[2], r[3])) for r in rows}

    def abrir(self, tx: Session, *, contexto: Contexto, sensor_id: uuid.UUID, tipo: str, severidad: str,
              detectada_en: datetime, detalle: dict[str, Any]) -> uuid.UUID:
        alerta_id = uuid.uuid4()
        tx.execute(
            text("INSERT INTO alerta (id, envio_id, contenedor_id, tramo_id, sensor_id, tipo, severidad,"
                 " primera_deteccion_en, ultima_deteccion_en, clave_deduplicacion, detalle)"
                 " VALUES (:id, :e, :c, :t, :s, :tipo, :sev, :d, :d, :clave, CAST(:detalle AS jsonb))"),
            {"id": alerta_id, "e": contexto.envio_id, "c": contexto.contenedor_id, "t": contexto.tramo_id,
             "s": sensor_id, "tipo": tipo, "sev": severidad, "d": detectada_en,
             "clave": clave_alerta(tipo, sensor_id, contexto.envio_id), "detalle": json.dumps(detalle, default=str)},
        )
        return alerta_id

    def detectar(self, tx: Session, alerta_id: uuid.UUID, detectada_en: datetime, severidad: str | None = None,
                 valor: Decimal | None = None) -> None:
        """Nueva detección del mismo problema (y escalamiento si se indica la severidad)."""
        tx.execute(
            text("UPDATE alerta SET ultima_deteccion_en = greatest(ultima_deteccion_en, :d),"
                 " severidad = coalesce(CAST(:sev AS varchar), severidad),"
                 " detalle = detalle || CAST(:detalle AS jsonb) WHERE id = :id"),
            {"id": alerta_id, "d": detectada_en, "sev": severidad,
             "detalle": json.dumps({"ultimo_valor": str(valor)} if valor is not None else {})},
        )

    def cerrar_automatica(self, tx: Session, alerta_id: uuid.UUID, normalizada_en: datetime) -> None:
        tx.execute(
            text("UPDATE alerta SET estado = 'CERRADA', cierre_tipo = 'AUTOMATICO', normalizada_en = :n,"
                 " cerrada_en = greatest(:n, ultima_deteccion_en) WHERE id = :id AND estado <> 'CERRADA'"),
            {"id": alerta_id, "n": normalizada_en},
        )

    def reconocer(self, tx: Session, alerta_id: uuid.UUID, usuario_id: uuid.UUID) -> bool:
        return tx.execute(
            text("UPDATE alerta SET estado = 'RECONOCIDA', reconocida_por_id = :u, reconocida_en = clock_timestamp()"
                 " WHERE id = :id AND estado = 'ABIERTA'"),
            {"id": alerta_id, "u": usuario_id},
        ).rowcount == 1

    def cerrar_manual(self, tx: Session, alerta_id: uuid.UUID, usuario_id: uuid.UUID, motivo: str) -> bool:
        return tx.execute(
            text("UPDATE alerta SET estado = 'CERRADA', cierre_tipo = 'MANUAL', cerrada_por_id = :u, motivo_cierre = :m,"
                 " cerrada_en = clock_timestamp() WHERE id = :id AND estado IN ('ABIERTA', 'RECONOCIDA')"),
            {"id": alerta_id, "u": usuario_id, "m": motivo},
        ).rowcount == 1

    def obtener(self, tx: Session, alerta_id: uuid.UUID, *, bloquear: bool = False) -> AlertaDTO | None:
        if bloquear:
            tx.execute(text("SELECT 1 FROM alerta WHERE id = :id FOR UPDATE"), {"id": alerta_id})
        r = tx.execute(text(_SELECT_ALERTA + " WHERE a.id = :id"), {"id": alerta_id}).one_or_none()
        return AlertaDTO(*r) if r else None

    def en_alcance(self, tx: Session, alerta_id: uuid.UUID, alcance: AlcanceConsulta) -> bool:
        return tx.execute(
            text("SELECT EXISTS (SELECT 1 FROM alerta a JOIN envio e ON e.id = a.envio_id WHERE a.id = :id AND"
                 + _ALCANCE_ALERTA + ")"),
            {"id": alerta_id, "cliente": alcance.cliente_id, "operador": alcance.operador_id},
        ).scalar_one()

    def listar(self, tx: Session, *, alcance: AlcanceConsulta, solo_activas: bool, envio_id: uuid.UUID | None,
               limite: int = 500) -> list[AlertaDTO]:
        rows = tx.execute(
            text(_SELECT_ALERTA + " WHERE" + _ALCANCE_ALERTA
                 + " AND (NOT :activas OR a.estado IN ('ABIERTA', 'RECONOCIDA'))"
                 " AND (CAST(:envio AS uuid) IS NULL OR a.envio_id = :envio)"
                 " ORDER BY a.ultima_deteccion_en DESC, a.id LIMIT :limite"),
            {"cliente": alcance.cliente_id, "operador": alcance.operador_id, "activas": solo_activas,
             "envio": envio_id, "limite": limite},
        )
        return [AlertaDTO(*r) for r in rows]


# --------------------------------------------------------------------------- incidentes
_SELECT_INCIDENTE = """
    SELECT i.id, i.envio_id, e.codigo, i.tramo_id, i.contenedor_id, i.tipo, i.severidad, i.estado, i.descripcion,
           i.ocurrido_en, i.abierto_en, i.responsable_id, u.email, i.resuelto_en, i.resolucion, i.alerta_origen_id,
           COALESCE((SELECT array_agg(ia.alerta_id ORDER BY ia.alerta_id) FROM incidente_alerta ia
                     WHERE ia.incidente_id = i.id), '{}') AS alertas
    FROM incidente i
    JOIN envio e ON e.id = i.envio_id
    LEFT JOIN usuario u ON u.id = i.responsable_id
"""
_ALCANCE_INCIDENTE = """
    ((CAST(:cliente AS uuid) IS NULL AND CAST(:operador AS uuid) IS NULL)
     OR e.cliente_id = :cliente
     OR EXISTS (SELECT 1 FROM tramo t WHERE t.id = i.tramo_id AND t.operador_id = :operador))
"""


class IncidenteRepository:
    def abrir(self, tx: Session, *, envio_id: uuid.UUID, tramo_id: uuid.UUID | None,
              contenedor_id: uuid.UUID | None, tipo: str, severidad: str, descripcion: str, ocurrido_en: datetime,
              alerta_origen_id: uuid.UUID | None) -> uuid.UUID:
        """Crea el incidente y, si viene de una alerta, su vínculo en incidente_alerta (misma transacción).

        `alerta_origen_id` es UNIQUE en el DDL: una alerta nunca origina dos incidentes.
        """
        incidente_id = uuid.uuid4()
        tx.execute(
            text("INSERT INTO incidente (id, envio_id, tramo_id, contenedor_id, tipo, severidad, descripcion,"
                 " ocurrido_en, alerta_origen_id) VALUES (:id, :e, :t, :c, :tipo, :sev, :desc, :oc, :alerta)"),
            {"id": incidente_id, "e": envio_id, "t": tramo_id, "c": contenedor_id, "tipo": tipo, "sev": severidad,
             "desc": descripcion, "oc": ocurrido_en, "alerta": alerta_origen_id},
        )
        if alerta_origen_id is not None:
            tx.execute(text("INSERT INTO incidente_alerta (incidente_id, alerta_id) VALUES (:i, :a)"),
                       {"i": incidente_id, "a": alerta_origen_id})
        return incidente_id

    def obtener(self, tx: Session, incidente_id: uuid.UUID, *, bloquear: bool = False) -> IncidenteDTO | None:
        if bloquear:
            tx.execute(text("SELECT 1 FROM incidente WHERE id = :id FOR UPDATE"), {"id": incidente_id})
        r = tx.execute(text(_SELECT_INCIDENTE + " WHERE i.id = :id"), {"id": incidente_id}).one_or_none()
        return IncidenteDTO(*r[:16], alertas=list(r[16])) if r else None

    def en_alcance(self, tx: Session, incidente_id: uuid.UUID, alcance: AlcanceConsulta) -> bool:
        return tx.execute(
            text("SELECT EXISTS (SELECT 1 FROM incidente i JOIN envio e ON e.id = i.envio_id WHERE i.id = :id AND"
                 + _ALCANCE_INCIDENTE + ")"),
            {"id": incidente_id, "cliente": alcance.cliente_id, "operador": alcance.operador_id},
        ).scalar_one()

    def listar(self, tx: Session, *, alcance: AlcanceConsulta, solo_abiertos: bool, envio_id: uuid.UUID | None,
               limite: int = 500) -> list[IncidenteDTO]:
        rows = tx.execute(
            text(_SELECT_INCIDENTE + " WHERE" + _ALCANCE_INCIDENTE
                 + " AND (NOT :abiertos OR i.estado <> 'RESUELTO')"
                 " AND (CAST(:envio AS uuid) IS NULL OR i.envio_id = :envio)"
                 " ORDER BY i.abierto_en DESC, i.id LIMIT :limite"),
            {"cliente": alcance.cliente_id, "operador": alcance.operador_id, "abiertos": solo_abiertos,
             "envio": envio_id, "limite": limite},
        )
        return [IncidenteDTO(*r[:16], alertas=list(r[16])) for r in rows]

    def gestionar(self, tx: Session, incidente_id: uuid.UUID, responsable_id: uuid.UUID) -> bool:
        return tx.execute(
            text("UPDATE incidente SET estado = 'EN_GESTION', responsable_id = :r WHERE id = :id AND estado <> 'RESUELTO'"),
            {"id": incidente_id, "r": responsable_id},
        ).rowcount == 1

    def resolver(self, tx: Session, incidente_id: uuid.UUID, resolucion: str) -> bool:
        return tx.execute(
            text("UPDATE incidente SET estado = 'RESUELTO', resolucion = :r,"
                 " resuelto_en = greatest(clock_timestamp(), abierto_en) WHERE id = :id AND estado <> 'RESUELTO'"),
            {"id": incidente_id, "r": resolucion},
        ).rowcount == 1
