"""Monitoreo: ingesta de lecturas, evaluación de reglas, alertas, incidentes y consultas (AGENTS.md §9, §10.4).

Flujo "Lectura IoT" (§9):
1. `registrar_lectura` (simulador/importador): resuelve el contexto AL MEDIR desde PostgreSQL y guarda la
   lectura DIRECTO en MongoDB con `_id` estable y evaluación PENDIENTE.
2. `evaluar_pendientes`: reserva atómica en Mongo → UNA transacción de PostgreSQL que bloquea el sensor,
   inserta `lectura_procesada` (PK lectura_id + version_reglas = idempotencia) y aplica alertas/estado/incidente
   → marca la lectura en Mongo. Si el proceso se cae entre el commit y la marca, el reintento encuentra
   `lectura_procesada` y solo marca: nunca duplica efectos.
3. `evaluar_comunicacion`: la falta de comunicación se CALCULA (último contacto + umbral). La fase 8 la
   programará; hoy la llaman la UI al refrescar y `scripts/simulador_iot.py`.

Operaciones de usuario: sesión → permiso → alcance → regla → transacción → auditoría.
SUPUESTOS M1..M7: ver AGENTS.md §16 ("Supuestos de la fase 4").
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from logistica.core.errors import (
    BusinessRuleError,
    DatabaseUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from logistica.core.logging_setup import redact
from logistica.domain import monitoreo as dm
from logistica.domain.acceso import Permiso, Principal
from logistica.domain.logistica import EstadoEnvio, exigir_con_zona, exigir_editable
from logistica.domain.monitoreo_dtos import (
    AlertaDTO,
    CondicionDatos,
    EstadoSensorDTO,
    IncidenteDTO,
    LecturaDTO,
    PaginaLecturas,
    PosicionDTO,
)
from logistica.repositories.connections import Connections
from logistica.repositories.mongo.lecturas import LecturasRepository
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.envios import AlcanceConsulta, EnvioRepository, TramoRepository
from logistica.repositories.postgres.monitoreo import (
    AlertaRepository,
    CondicionRepository,
    ContextoRepository,
    EstadoSensorRepository,
    IncidenteRepository,
    LecturaProcesadaRepository,
)
from logistica.repositories.postgres.monitoreo import ahora as ahora_pg
from logistica.repositories.postgres.outbox import MONGO, OutboxRepository
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador
from logistica.services.envio_service import _alcance

log = logging.getLogger(__name__)

VERSION_REGLAS = "1.0"  # SUPUESTO M6: versión inicial de las reglas (parámetros por defecto de §10.4)
RESERVA_S = 60
MAX_INTENTOS = 5
ESPERA_REINTENTO_S = 30
TIPOS_INCIDENTE_MANUAL = ("TEMPERATURA", "HUMEDAD", "VIBRACION", "BATERIA", "SIN_COMUNICACION", "DANIO", "OTRO")
SEVERIDADES = ("BAJA", "MEDIA", "ALTA")


@dataclass(frozen=True)
class LecturaEntrada:
    """Lo que manda un dispositivo (o el simulador). `id` estable: reenviar la misma lectura no la duplica."""

    id: uuid.UUID
    sensor_id: uuid.UUID
    fecha_medicion: datetime
    mediciones: dict[str, Any]
    latitud: float | None = None
    longitud: float | None = None
    origen: str = "DISPOSITIVO"


@dataclass(frozen=True)
class ResultadoEvaluacion:
    evaluadas: int = 0  # actualizaron el estado actual
    historicas: int = 0  # tardías o anteriores: quedan como histórico válido
    revision: int = 0  # sin contexto o reloj adelantado: quedan para revisión
    repetidas: int = 0  # ya estaban procesadas (por ejemplo, se cayó antes de marcar): solo se marcaron
    reintentos: int = 0  # fallaron y se reintentarán


class MonitoreoService:
    def __init__(self, connections: Connections, autorizador: Autorizador, *,
                 reloj: Callable[[], datetime] | None = None, reserva_s: float = RESERVA_S,
                 max_intentos: int = MAX_INTENTOS):
        self._conn = connections
        self._aut = autorizador
        self._reloj = reloj  # None = reloj de PostgreSQL (referencia común para todas las instancias)
        self._reserva_s = reserva_s
        self._max_intentos = max_intentos
        self._lecturas = LecturasRepository(connections)
        self._condiciones = CondicionRepository()
        self._contextos = ContextoRepository()
        self._estados = EstadoSensorRepository()
        self._procesadas = LecturaProcesadaRepository()
        self._alertas = AlertaRepository()
        self._incidentes = IncidenteRepository()
        self._envios = EnvioRepository()
        self._tramos = TramoRepository()
        self._outbox = OutboxRepository()
        self.propietario_id = uuid.uuid4()  # identifica a este evaluador en las reservas de Mongo

    # ======================================================================= condiciones
    def definir_condiciones(self, sesion_id: uuid.UUID, envio_id: uuid.UUID, datos: CondicionDatos) -> CondicionDatos:
        """SUPUESTO M6: se definen mientras el envío no salió (BORRADOR o PLANIFICADO)."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (tx, p):
            envio = self._envios.obtener(tx, envio_id, bloquear=True)
            if envio is None:
                raise NotFoundError(f"envío {envio_id}", user_message="El envío no existe.")
            exigir_editable(envio.estado)
            normalizada = CondicionDatos(**dm.validar_condicion(datos))
            self._condiciones.guardar(tx, envio_id, normalizada, VERSION_REGLAS, {})
            auditoria.registrar(tx, "definir_condiciones", p, entidad="envio", entidad_id=envio_id)
            return normalizada

    def condiciones(self, sesion_id: uuid.UUID, envio_id: uuid.UUID) -> CondicionDatos | None:
        with self._aut.operacion(sesion_id, Permiso.MONITOREO_CONSULTAR, Permiso.ENVIOS_PLANIFICAR) as (tx, p):
            self._exigir_envio_visible(tx, p, envio_id)
            c = self._condiciones.obtener(tx, envio_id)
            if c is None:
                return None
            return CondicionDatos(c.temperatura_min_c, c.temperatura_max_c, c.humedad_min_pct, c.humedad_max_pct,
                                  c.vibracion_max_g, c.bateria_min_pct, c.tiempo_max_sin_lecturas_seg)

    # ======================================================================= ingesta
    def registrar_lectura(self, entrada: LecturaEntrada) -> bool:
        """Guarda la lectura en Mongo (idempotente por `id`). Devuelve True si es nueva.

        Proceso técnico (dispositivo/simulador): no tiene sesión de usuario. El contexto se resuelve AL MEDIR;
        si no hay (sensor no instalado o contenedor sin envío en uso) la lectura se guarda igual, sin contexto,
        y la evaluación la deja para revisión (SUPUESTO M2): nunca se inventa un contexto.
        """
        exigir_con_zona(entrada.fecha_medicion)
        mediciones = dm.validar_mediciones(entrada.mediciones)
        posicion = dm.validar_posicion(entrada.latitud, entrada.longitud)
        if entrada.origen not in dm.ORIGENES:
            raise ValidationError(f"origen {entrada.origen}", user_message="Origen de lectura inválido.")
        with unit_of_work(self._conn) as tx:
            if not self._estados.existe(tx, entrada.sensor_id):  # sin lock: la ingesta no frena al evaluador
                raise NotFoundError(f"sensor {entrada.sensor_id}", user_message="El sensor no existe.")
            contexto = self._contextos.al_medir(tx, entrada.sensor_id, entrada.fecha_medicion)
            recepcion = self._ahora(tx)
        doc: dict[str, Any] = {
            "_id": entrada.id, "sensor_id": entrada.sensor_id,
            "fecha_medicion": entrada.fecha_medicion.astimezone(timezone.utc), "fecha_recepcion": recepcion,
            "mediciones": {k: float(v) for k, v in mediciones.items()}, "origen": entrada.origen,
            "evaluacion": {"estado": "PENDIENTE", "intentos": 0, "version_reclamo": 0, "proximo_intento_en": recepcion},
        }
        if contexto is not None:
            doc.update({"asignacion_sensor_id": contexto.asignacion_sensor_id, "contenedor_id": contexto.contenedor_id,
                        "envio_id": contexto.envio_id})
            if contexto.tramo_id is not None:
                doc["tramo_id"] = contexto.tramo_id
        if posicion is not None:
            doc["posicion"] = {"type": "Point", "coordinates": [posicion[1], posicion[0]]}  # GeoJSON: [lon, lat]
        return self._lecturas.insertar(doc)

    # ======================================================================= evaluación
    def evaluar_pendientes(self, *, limite: int = 200,
                           sensor_ids: list[uuid.UUID] | None = None) -> ResultadoEvaluacion:
        """Evalúa hasta `limite` lecturas pendientes (las más viejas primero). Para tras el primer fallo de
        PostgreSQL: no tiene sentido seguir si la base no responde."""
        cuenta = dict.fromkeys(("evaluadas", "historicas", "revision", "repetidas", "reintentos"), 0)
        for _ in range(limite):
            ahora = self._ahora_simple()
            doc = self._lecturas.reservar(propietario_id=self.propietario_id, ahora=ahora,
                                          reserva_s=self._reserva_s, sensor_ids=sensor_ids)
            if doc is None:
                break
            try:
                cuenta[self._evaluar(doc)] += 1
            except DatabaseUnavailableError as exc:
                self._lecturas.reintentar(doc, error=redact(str(exc)), ahora=ahora, espera_s=ESPERA_REINTENTO_S,
                                          max_intentos=self._max_intentos, gasta_intento=False)
                cuenta["reintentos"] += 1
                log.warning("PostgreSQL no disponible: se pospone la evaluación de lecturas")
                break
            except Exception as exc:  # error de ESA lectura: cuenta un intento y se sigue con las demás
                log.exception("Error evaluando la lectura %s", doc["_id"])
                self._lecturas.reintentar(doc, error=redact(f"{type(exc).__name__}: {exc}"), ahora=ahora,
                                          espera_s=ESPERA_REINTENTO_S, max_intentos=self._max_intentos)
                cuenta["reintentos"] += 1
        return ResultadoEvaluacion(**cuenta)

    def _evaluar(self, doc: dict[str, Any]) -> str:
        """Evalúa una lectura reservada. Devuelve el contador que corresponde."""
        lectura = _lectura_de(doc)
        with unit_of_work(self._conn) as tx:
            self._estados.bloquear(tx, lectura.sensor_id)  # una evaluación por sensor a la vez
            condicion = (self._condiciones.obtener(tx, lectura.contexto.envio_id) if lectura.contexto else None)
            condicion = condicion or dm.condicion_vacia(VERSION_REGLAS)
            if not self._procesadas.registrar(tx, lectura.id, condicion.version_reglas):
                repetida = True  # ya se evaluó (se cayó antes de marcar en Mongo): no se repiten efectos
                resultado = None
            else:
                repetida = False
                estado = self._estados.obtener(tx, lectura.sensor_id)
                activas = (self._alertas.activas(tx, lectura.sensor_id, lectura.contexto.envio_id)
                           if lectura.contexto else {})
                resultado = dm.evaluar_lectura(lectura, estado, condicion, {t: a for t, (_, a) in activas.items()})
                if resultado.actualiza_estado:
                    ids = {t: i for t, (i, _) in activas.items()}
                    self._aplicar(tx, lectura.sensor_id, lectura.contexto, resultado.acciones, ids, condicion)
                    self._estados.guardar(tx, lectura.sensor_id, resultado.estado, lectura.fecha_recepcion)
        ahora = self._ahora_simple()
        if repetida:
            self._lecturas.marcar_procesada(doc, version_reglas=condicion.version_reglas, ahora=ahora,
                                            resultado="REPETIDA")
            return "repetidas"
        clasificacion = resultado.clasificacion
        if clasificacion in (dm.Clasificacion.SIN_CONTEXTO, dm.Clasificacion.RELOJ_ADELANTADO):
            self._lecturas.marcar_revision(doc, motivo=clasificacion.value, ahora=ahora)
            log.warning("Lectura %s para revisión: %s", lectura.id, clasificacion.value)
            return "revision"
        self._lecturas.marcar_procesada(doc, version_reglas=condicion.version_reglas, ahora=ahora,
                                        resultado=clasificacion.value)
        return "evaluadas" if resultado.actualiza_estado else "historicas"

    def _aplicar(self, tx: Any, sensor_id: uuid.UUID, contexto: dm.Contexto, acciones: list[Any],
                 ids: dict[str, uuid.UUID], condicion: dm.Condicion) -> None:
        """Ejecuta las acciones del dominio en la transacción de la evaluación."""
        for a in acciones:
            if isinstance(a, dm.AbrirAlerta):
                ids[a.tipo] = self._alertas.abrir(
                    tx, contexto=contexto, sensor_id=sensor_id, tipo=a.tipo, severidad=a.severidad,
                    detectada_en=a.detectada_en,
                    detalle={"valor": str(a.valor) if a.valor is not None else None,
                             "version_reglas": condicion.version_reglas})
            elif isinstance(a, dm.ActualizarAlerta):
                self._alertas.detectar(tx, ids[a.tipo], a.detectada_en, valor=a.valor)
            elif isinstance(a, dm.EscalarAlerta):
                self._alertas.detectar(tx, ids[a.tipo], a.detectada_en, severidad=a.severidad)
            elif isinstance(a, dm.CerrarAlerta):
                self._alertas.cerrar_automatica(tx, ids.pop(a.tipo), a.normalizada_en)
            elif isinstance(a, dm.AbrirIncidente):
                incidente_id = self._incidentes.abrir(
                    tx, envio_id=contexto.envio_id, tramo_id=contexto.tramo_id, contenedor_id=contexto.contenedor_id,
                    tipo=a.tipo, severidad=a.severidad, descripcion=a.descripcion, ocurrido_en=a.ocurrido_en,
                    alerta_origen_id=ids[a.tipo_alerta])
                self._evento_incidente(tx, incidente_id, "INCIDENTE_ABIERTO", a.ocurrido_en, None,
                                       {"tipo_incidente": a.tipo, "severidad": a.severidad, "automatico": True})
                log.info("Incidente automático %s (%s) en el envío %s", incidente_id, a.tipo, contexto.envio_id)

    def evaluar_comunicacion(self, *, sensor_ids: list[uuid.UUID] | None = None) -> int:
        """Abre/escala alertas SIN_COMUNICACION de los sensores que deberían estar enviando y no lo hacen.

        Devuelve cuántas alertas abrió o escaló. El cierre lo hace la llegada de una lectura nueva y reciente.
        """
        cambios = 0
        with unit_of_work(self._conn) as tx:
            ahora = self._ahora(tx)
            for esperado in self._estados.esperados(tx, sensor_ids):
                self._estados.bloquear(tx, esperado.sensor_id)
                condicion = (self._condiciones.obtener(tx, esperado.contexto.envio_id)
                             or dm.condicion_vacia(VERSION_REGLAS))
                activas = self._alertas.activas(tx, esperado.sensor_id, esperado.contexto.envio_id)
                activa = activas.get(dm.SIN_COMUNICACION)
                accion = dm.evaluar_comunicacion(ultimo_contacto=esperado.ultimo_contacto, ahora=ahora,
                                                 condicion=condicion, activa=activa[1] if activa else None)
                if accion is None:
                    continue
                ids = {t: i for t, (i, _) in activas.items()}
                self._aplicar(tx, esperado.sensor_id, esperado.contexto, [accion], ids, condicion)
                cambios += not isinstance(accion, dm.ActualizarAlerta)
        return cambios

    # ======================================================================= consultas (con sesión)
    def estado_sensores(self, sesion_id: uuid.UUID, *, envio_id: uuid.UUID | None = None) -> list[EstadoSensorDTO]:
        with self._aut.operacion(sesion_id, Permiso.MONITOREO_CONSULTAR) as (tx, p):
            if envio_id is not None:
                self._exigir_envio_visible(tx, p, envio_id)
            return self._estados.listar(tx, alcance=_alcance(p), envio_id=envio_id)

    def alertas(self, sesion_id: uuid.UUID, *, solo_activas: bool = True,
                envio_id: uuid.UUID | None = None) -> list[AlertaDTO]:
        with self._aut.operacion(sesion_id, Permiso.MONITOREO_CONSULTAR) as (tx, p):
            return self._alertas.listar(tx, alcance=_alcance(p), solo_activas=solo_activas, envio_id=envio_id)

    def incidentes(self, sesion_id: uuid.UUID, *, solo_abiertos: bool = True,
                   envio_id: uuid.UUID | None = None) -> list[IncidenteDTO]:
        with self._aut.operacion(sesion_id, Permiso.MONITOREO_CONSULTAR) as (tx, p):
            return self._incidentes.listar(tx, alcance=_alcance(p), solo_abiertos=solo_abiertos, envio_id=envio_id)

    def lecturas(self, sesion_id: uuid.UUID, *, campo: str, valor: uuid.UUID, desde: datetime | None = None,
                 hasta: datetime | None = None, limite: int = 100,
                 despues: tuple[datetime, uuid.UUID] | None = None) -> PaginaLecturas:
        """Histórico paginado. Un usuario externo solo consulta por envío (de su alcance)."""
        exigir_con_zona(desde, hasta)
        if not 1 <= limite <= 1000:
            raise ValidationError("límite", user_message="Tamaño de página inválido.")
        with self._aut.operacion(sesion_id, Permiso.MONITOREO_CONSULTAR) as (tx, p):
            tramos = self._alcance_lecturas(tx, p, campo, valor)
        docs = self._lecturas.historial(campo=campo, valor=valor, desde=desde, hasta=hasta, limite=limite + 1,
                                        despues=despues, tramo_ids=tramos)
        pagina = [_lectura_dto(d) for d in docs[:limite]]
        siguiente = (pagina[-1].fecha_medicion, pagina[-1].id) if len(docs) > limite else None
        return PaginaLecturas(pagina, siguiente)

    def posiciones(self, sesion_id: uuid.UUID, envio_id: uuid.UUID) -> list[PosicionDTO]:
        with self._aut.operacion(sesion_id, Permiso.MONITOREO_CONSULTAR) as (tx, p):
            tramos = self._alcance_lecturas(tx, p, "envio_id", envio_id)
        return [PosicionDTO(d["fecha_medicion"], d["posicion"]["coordinates"][1], d["posicion"]["coordinates"][0],
                            d["sensor_id"]) for d in self._lecturas.posiciones(envio_id=envio_id, tramo_ids=tramos)]

    # ======================================================================= acciones de usuario
    def reconocer_alerta(self, sesion_id: uuid.UUID, alerta_id: uuid.UUID) -> AlertaDTO:
        """Reconocer = "la vi y me ocupo"; NO cierra la alerta."""
        with self._aut.operacion(sesion_id, Permiso.ALERTAS_RECONOCER) as (tx, p):
            self._exigir_alerta_visible(tx, p, alerta_id)
            alerta = self._alertas.obtener(tx, alerta_id, bloquear=True)
            if not self._alertas.reconocer(tx, alerta_id, p.usuario_id):
                raise BusinessRuleError(f"alerta {alerta.estado}", user_message="Solo se reconoce una alerta abierta.")
            auditoria.registrar(tx, "reconocer_alerta", p, entidad="alerta", entidad_id=alerta_id)
            return self._alertas.obtener(tx, alerta_id)

    def cerrar_alerta(self, sesion_id: uuid.UUID, alerta_id: uuid.UUID, motivo: str) -> AlertaDTO:
        """Cierre manual: exige motivo; guarda usuario y fecha (ck_alerta_cierre_manual)."""
        with self._aut.operacion(sesion_id, Permiso.ALERTAS_CERRAR) as (tx, p):
            motivo = (motivo or "").strip()
            if not motivo:
                raise ValidationError("sin motivo", user_message="Indique el motivo del cierre.")
            self._exigir_alerta_visible(tx, p, alerta_id)
            self._alertas.obtener(tx, alerta_id, bloquear=True)
            if not self._alertas.cerrar_manual(tx, alerta_id, p.usuario_id, motivo):
                raise BusinessRuleError("alerta ya cerrada", user_message="La alerta ya está cerrada.")
            auditoria.registrar(tx, "cerrar_alerta", p, entidad="alerta", entidad_id=alerta_id)
            return self._alertas.obtener(tx, alerta_id)

    def crear_incidente(self, sesion_id: uuid.UUID, envio_id: uuid.UUID, *, tipo: str, severidad: str,
                        descripcion: str, alerta_id: uuid.UUID | None = None) -> IncidenteDTO:
        """Incidente manual (SUPUESTO M5): desde una alerta del envío o directo sobre un envío del alcance."""
        with self._aut.operacion(sesion_id, Permiso.INCIDENTES_CREAR) as (tx, p):
            self._exigir_envio_visible(tx, p, envio_id)
            tipo, severidad, descripcion = (tipo or "").strip().upper(), (severidad or "").strip().upper(), \
                (descripcion or "").strip()
            if tipo not in TIPOS_INCIDENTE_MANUAL or severidad not in SEVERIDADES or not descripcion:
                raise ValidationError("incidente inválido",
                                      user_message="Indique tipo, severidad y una descripción del incidente.")
            envio = self._envios.obtener(tx, envio_id)
            if envio.estado not in (EstadoEnvio.PLANIFICADO, EstadoEnvio.EN_TRANSITO, EstadoEnvio.ENTREGADO):
                raise BusinessRuleError("envío sin viaje", user_message="El envío no tiene un viaje que monitorear.")
            tramo_id = contenedor_id = None
            ocurrido = self._ahora(tx)
            if alerta_id is not None:
                self._exigir_alerta_visible(tx, p, alerta_id)
                alerta = self._alertas.obtener(tx, alerta_id, bloquear=True)
                if alerta.envio_id != envio_id:
                    raise ValidationError("alerta de otro envío", user_message="La alerta no pertenece al envío.")
                if alerta.incidente_id is not None:
                    raise BusinessRuleError("alerta con incidente", user_message="La alerta ya tiene un incidente.")
                tramo_id, contenedor_id, ocurrido = alerta.tramo_id, alerta.contenedor_id, alerta.primera_deteccion_en
            incidente_id = self._incidentes.abrir(tx, envio_id=envio_id, tramo_id=tramo_id, contenedor_id=contenedor_id,
                                                  tipo=tipo, severidad=severidad, descripcion=descripcion,
                                                  ocurrido_en=ocurrido, alerta_origen_id=alerta_id)
            self._evento_incidente(tx, incidente_id, "INCIDENTE_ABIERTO", ocurrido, p.usuario_id,
                                   {"tipo_incidente": tipo, "severidad": severidad, "automatico": False})
            auditoria.registrar(tx, "crear_incidente", p, entidad="incidente", entidad_id=incidente_id, tipo=tipo)
            return self._incidentes.obtener(tx, incidente_id)

    def tomar_incidente(self, sesion_id: uuid.UUID, incidente_id: uuid.UUID) -> IncidenteDTO:
        """El usuario se hace responsable: EN_GESTION."""
        with self._aut.operacion(sesion_id, Permiso.INCIDENTES_GESTIONAR) as (tx, p):
            self._exigir_incidente_visible(tx, p, incidente_id)
            self._incidentes.obtener(tx, incidente_id, bloquear=True)
            if not self._incidentes.gestionar(tx, incidente_id, p.usuario_id):
                raise BusinessRuleError("incidente resuelto", user_message="El incidente ya está resuelto.")
            auditoria.registrar(tx, "tomar_incidente", p, entidad="incidente", entidad_id=incidente_id)
            return self._incidentes.obtener(tx, incidente_id)

    def resolver_incidente(self, sesion_id: uuid.UUID, incidente_id: uuid.UUID, resolucion: str) -> IncidenteDTO:
        """Resolver es humano: el evaluador nunca resuelve (normalizar una alerta no resuelve el incidente)."""
        with self._aut.operacion(sesion_id, Permiso.INCIDENTES_GESTIONAR) as (tx, p):
            resolucion = (resolucion or "").strip()
            if not resolucion:
                raise ValidationError("sin resolución", user_message="Describa la resolución del incidente.")
            self._exigir_incidente_visible(tx, p, incidente_id)
            self._incidentes.obtener(tx, incidente_id, bloquear=True)
            if not self._incidentes.resolver(tx, incidente_id, resolucion):
                raise BusinessRuleError("incidente resuelto", user_message="El incidente ya está resuelto.")
            incidente = self._incidentes.obtener(tx, incidente_id)
            self._evento_incidente(tx, incidente_id, "INCIDENTE_RESUELTO", incidente.resuelto_en, p.usuario_id,
                                   {"tipo_incidente": incidente.tipo, "severidad": incidente.severidad})
            auditoria.registrar(tx, "resolver_incidente", p, entidad="incidente", entidad_id=incidente_id)
            return incidente

    # ======================================================================= auxiliares
    def _evento_incidente(self, tx: Any, incidente_id: uuid.UUID, tipo: str, fecha: datetime,
                          usuario_id: uuid.UUID | None, detalles: dict[str, Any]) -> None:
        """SUPUESTO M1: los incidentes (abierto/resuelto) van al historial de eventos en Mongo vía outbox.

        Las alertas no: el validador de eventos_logisticos no tiene tipos para ellas, y su fuente es PostgreSQL.
        Agregado INCIDENTE: versión 1 = abierto, 2 = resuelto. No se toca Neo4j en esta fase.
        """
        incidente = self._incidentes.obtener(tx, incidente_id)
        doc: dict[str, Any] = {"tipo": tipo, "envio_id": incidente.envio_id, "fecha_ocurrencia": fecha,
                               "contenedor_ids": [str(incidente.contenedor_id)] if incidente.contenedor_id else [],
                               "detalles": {"incidente_id": str(incidente_id), **detalles}}
        if incidente.tramo_id is not None:
            doc["tramo_id"] = incidente.tramo_id
        if usuario_id is not None:
            doc["usuario_id"] = usuario_id
        self._outbox.registrar(tx, agregado_tipo="INCIDENTE", agregado_id=incidente_id,
                               agregado_version=1 if tipo == "INCIDENTE_ABIERTO" else 2, evento_tipo=tipo,
                               payload=doc, destinos=(MONGO,))

    def _exigir_envio_visible(self, tx: Any, p: Principal, envio_id: uuid.UUID) -> None:
        if not p.es_interno and not self._envios.en_alcance(tx, envio_id, _alcance(p)):
            raise PermissionDeniedError(f"usuario {p.usuario_id} fuera de alcance de {envio_id}")
        if p.es_interno and self._envios.obtener(tx, envio_id) is None:
            raise NotFoundError(f"envío {envio_id}", user_message="El envío no existe.")

    def _exigir_alerta_visible(self, tx: Any, p: Principal, alerta_id: uuid.UUID) -> None:
        if not p.es_interno and not self._alertas.en_alcance(tx, alerta_id, _alcance(p)):
            raise PermissionDeniedError(f"usuario {p.usuario_id} fuera de alcance de la alerta {alerta_id}")
        if self._alertas.obtener(tx, alerta_id) is None:
            raise NotFoundError(f"alerta {alerta_id}", user_message="La alerta no existe.")

    def _exigir_incidente_visible(self, tx: Any, p: Principal, incidente_id: uuid.UUID) -> None:
        if not p.es_interno and not self._incidentes.en_alcance(tx, incidente_id, _alcance(p)):
            raise PermissionDeniedError(f"usuario {p.usuario_id} fuera de alcance del incidente {incidente_id}")
        if self._incidentes.obtener(tx, incidente_id) is None:
            raise NotFoundError(f"incidente {incidente_id}", user_message="El incidente no existe.")

    def _alcance_lecturas(self, tx: Any, p: Principal, campo: str, valor: uuid.UUID) -> list[uuid.UUID] | None:
        """Internos: todo. Externos: solo por envío de su alcance; el operador, solo durante SUS tramos."""
        if p.es_interno:
            return None
        if campo != "envio_id":
            raise PermissionDeniedError(f"usuario externo consultando lecturas por {campo}")
        self._exigir_envio_visible(tx, p, valor)
        envio = self._envios.obtener(tx, valor)
        if envio is not None and envio.cliente_id == p.tercero_id:
            return None  # el cliente ve todas las lecturas de SU envío
        return [t.id for t in self._tramos.de_envio(tx, valor) if t.operador_id == p.tercero_id]

    def _ahora(self, tx: Any) -> datetime:
        return self._reloj() if self._reloj is not None else ahora_pg(tx)

    def _ahora_simple(self) -> datetime:
        if self._reloj is not None:
            return self._reloj()
        with unit_of_work(self._conn) as tx:
            return self._ahora(tx)


def _lectura_de(doc: dict[str, Any]) -> dm.Lectura:
    contexto = None
    if doc.get("envio_id") and doc.get("contenedor_id") and doc.get("asignacion_sensor_id"):
        contexto = dm.Contexto(doc["envio_id"], doc["contenedor_id"], doc["asignacion_sensor_id"], doc.get("tramo_id"))
    return dm.Lectura(id=doc["_id"], sensor_id=doc["sensor_id"], fecha_medicion=_utc(doc["fecha_medicion"]),
                      fecha_recepcion=_utc(doc["fecha_recepcion"]), contexto=contexto,
                      mediciones={k: Decimal(str(v)) for k, v in doc.get("mediciones", {}).items()})


def _lectura_dto(d: dict[str, Any]) -> LecturaDTO:
    med = d.get("mediciones", {})
    pos = (d.get("posicion") or {}).get("coordinates")

    def valor(k):
        return Decimal(str(med[k])) if k in med else None

    return LecturaDTO(d["_id"], d["sensor_id"], _utc(d["fecha_medicion"]), _utc(d["fecha_recepcion"]),
                      valor("temperatura_c"), valor("humedad_pct"), valor("vibracion_g"), valor("bateria_pct"),
                      pos[1] if pos else None, pos[0] if pos else None, d.get("origen", ""),
                      (d.get("evaluacion") or {}).get("estado"))


def _utc(fecha: datetime) -> datetime:
    """Mongo devuelve fechas en UTC; si el cliente no las marcó con zona, se marcan."""
    return fecha if fecha.tzinfo is not None else fecha.replace(tzinfo=timezone.utc)
