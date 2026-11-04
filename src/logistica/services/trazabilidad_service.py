"""Consultas de trazabilidad con sesión, permiso, alcance y revalidación entre motores."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from logistica.core.errors import (
    DatabaseUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from logistica.domain.acceso import Permiso, Principal, Rol
from logistica.domain.logistica_dtos import EnvioDTO, TramoDTO
from logistica.domain.trazabilidad_dtos import (
    AsignacionContenedorDTO,
    EventoCursor,
    EventoTrazabilidadDTO,
    PaginaEventosDTO,
    PosicionContenedorDTO,
    RedTrazabilidadDTO,
    RelacionRedDTO,
    RecorridoContenedorDTO,
    RutaTrazabilidadDTO,
    TramoContenedorDTO,
    TramoRutaDTO,
)
from logistica.repositories.connections import Connections
from logistica.repositories.mongo.eventos import EventosRepository
from logistica.repositories.mongo.lecturas import LecturasRepository
from logistica.repositories.neo4j.proyeccion import ProyeccionRepository
from logistica.repositories.postgres.envios import (
    AlcanceConsulta,
    EnvioRepository,
    TramoRepository,
)
from logistica.repositories.postgres.outbox import NEO4J, OutboxRepository
from logistica.repositories.postgres.trazabilidad import TrazabilidadRepository
from logistica.services.autorizacion import Autorizador

log = logging.getLogger(__name__)

MAX_PAGINA_EVENTOS = 100
MAX_TRAMOS_RUTA = 200
MAX_ASIGNACIONES_CONTENEDOR = 100
MAX_POSICIONES_ASIGNACION = 200
MAX_ENVIOS_RED = 500
MAX_RELACIONES_RED = 100


class TrazabilidadService:
    def __init__(self, connections: Connections, autorizador: Autorizador):
        self._conn = connections
        self._aut = autorizador
        self._envios = EnvioRepository()
        self._tramos = TramoRepository()
        self._eventos = EventosRepository(connections)
        self._lecturas = LecturasRepository(connections)
        self._proyeccion = ProyeccionRepository(connections)
        self._outbox = OutboxRepository()
        self._repo = TrazabilidadRepository()

    def eventos(
        self,
        sesion_id: uuid.UUID,
        envio_id: uuid.UUID,
        *,
        tipo: str | None = None,
        desde: datetime | None = None,
        hasta: datetime | None = None,
        texto: str | None = None,
        tramo_id: uuid.UUID | None = None,
        despues: EventoCursor | None = None,
        limite: int = 50,
    ) -> PaginaEventosDTO:
        """Consulta y pagina el histórico Mongo; la outbox solo aporta estado de entrega."""
        self._validar_filtros_eventos(tipo, desde, hasta, texto, despues, limite)
        tipo = (tipo or "").strip().upper() or None
        texto = (texto or "").strip() or None
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, principal):
            self._envio_visible(tx, principal, envio_id)
            tramos = self._tramos_visibles(tx, principal, envio_id)
            tramo_ids = [tramo.id for tramo in tramos] if _es_operador(principal) else None
            if tramo_id is not None and tramo_id not in {tramo.id for tramo in tramos}:
                raise PermissionDeniedError("tramo fuera del alcance del usuario")

        documentos = self._eventos.historial_envio(
            envio_id,
            tipo=tipo,
            desde=desde,
            hasta=hasta,
            texto=texto,
            tramo_id=tramo_id,
            tramo_ids=tramo_ids,
            despues=despues,
            limite=limite + 1,
        )
        visibles = documentos[:limite]
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, principal):
            self._envio_visible(tx, principal, envio_id)
            actuales = self._tramos_visibles(tx, principal, envio_id)
            ids_actuales = [tramo.id for tramo in actuales] if _es_operador(principal) else None
            if ids_actuales != tramo_ids:
                raise PermissionDeniedError("el alcance cambió durante la consulta; vuelva a consultar")
            estados = self._outbox.estados_mongo(tx, [doc["_id"] for doc in visibles])
            resumen = self._outbox.resumen_mongo_envio(tx, envio_id, tramo_ids=tramo_ids)

        eventos = [
            EventoTrazabilidadDTO(
                id=doc["_id"],
                fecha_ocurrencia=_utc(doc["fecha_ocurrencia"]),
                fecha_registro=_utc(doc["fecha_registro"]),
                tipo=doc["tipo"],
                descripcion=doc.get("descripcion"),
                tramo_id=doc.get("tramo_id"),
                ubicacion_id=doc.get("ubicacion_id"),
                contenedor_ids=tuple(doc.get("contenedor_ids", ())),
                detalles=dict(doc.get("detalles") or {}),
                sincronizacion=_estado_evento(estados.get(doc["_id"])),
            )
            for doc in visibles
        ]
        siguiente = (eventos[-1].fecha_ocurrencia, eventos[-1].id) if len(documentos) > limite else None
        return PaginaEventosDTO(
            eventos,
            siguiente,
            resumen["PENDIENTE"] + resumen["EN_PROCESO"],
            resumen["FALLIDA"],
        )

    def ruta(self, sesion_id: uuid.UUID, envio_id: uuid.UUID) -> RutaTrazabilidadDTO:
        """Consulta la ruta real de Neo4j y la contrasta con la versión operativa actual."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, principal):
            self._envio_visible(tx, principal, envio_id)
            tramos = self._tramos_visibles(tx, principal, envio_id)
            ids_tramos = [tramo.id for tramo in tramos]

        disponible = True
        proyeccion = None
        try:
            proyeccion = self._proyeccion.ruta(envio_id, limite=MAX_TRAMOS_RUTA + 1)
        except DatabaseUnavailableError:
            disponible = False
            log.warning("Neo4j no disponible al consultar ruta del envío %s", envio_id)

        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, principal):
            actual = self._envio_visible(tx, principal, envio_id)
            tramos_actuales = self._tramos_visibles(tx, principal, envio_id)
            ids_actuales = [tramo.id for tramo in tramos_actuales]
            if _es_operador(principal) and ids_actuales != ids_tramos:
                raise PermissionDeniedError("el alcance cambió durante la consulta; vuelva a consultar")
            resumen = self._outbox.resumen(tx, envio_id).get(NEO4J, {})
            proyectada = proyeccion.version if proyeccion is not None else None
            estado = _estado_proyeccion(
                actual.version,
                proyectada,
                disponible=disponible,
                pendientes=resumen.get("PENDIENTE", 0) + resumen.get("EN_PROCESO", 0),
                fallidas=resumen.get("FALLIDA", 0),
            )
            mostrados = proyeccion.tramos if proyeccion is not None else []
            if _es_operador(principal):
                permitidos = set(ids_actuales)
                mostrados = [tramo for tramo in mostrados if tramo.id in permitidos]
            mostrados = mostrados[:MAX_TRAMOS_RUTA]
            ubicacion_ids = list(
                {ident for tramo in mostrados for ident in (tramo.origen_id, tramo.destino_id)}
            )
            tercero_ids = list({tramo.operador_id for tramo in mostrados})
            ubicaciones, terceros = self._repo.nombres_catalogo(tx, ubicacion_ids, tercero_ids)
            dto_tramos = [
                TramoRutaDTO(
                    id=tramo.id,
                    orden=tramo.orden,
                    origen_id=tramo.origen_id,
                    origen_nombre=ubicaciones.get(tramo.origen_id, f"Ubicación {tramo.origen_id}"),
                    destino_id=tramo.destino_id,
                    destino_nombre=ubicaciones.get(tramo.destino_id, f"Ubicación {tramo.destino_id}"),
                    operador_id=tramo.operador_id,
                    operador_razon_social=terceros.get(
                        tramo.operador_id, f"Operador {tramo.operador_id}"
                    ),
                    medio=tramo.medio,
                    estado=tramo.estado,
                    vehiculo=tramo.vehiculo,
                    salida_estimada_en=tramo.salida_estimada_en,
                    llegada_estimada_en=tramo.llegada_estimada_en,
                    salida_real_en=tramo.salida_real_en,
                    llegada_real_en=tramo.llegada_real_en,
                )
                for tramo in mostrados
            ]
            return RutaTrazabilidadDTO(
                envio_id=envio_id,
                codigo=actual.codigo,
                estado_proyeccion=estado,
                version_operativa=actual.version,
                version_proyectada=proyectada,
                tramos=dto_tramos,
                nota_catalogo=(
                    "Tramos y versión consultados en Neo4j; nombres vigentes resueltos por ID "
                    "en PostgreSQL (decisión D6)."
                ),
                truncada=proyeccion is not None and len(proyeccion.tramos) > MAX_TRAMOS_RUTA,
            )

    def recorrido_contenedor(
        self, sesion_id: uuid.UUID, codigo: str
    ) -> RecorridoContenedorDTO:
        """Muestra asignaciones y tramos relacionados dentro de sus períodos temporales."""
        codigo = (codigo or "").strip().upper()
        if not codigo:
            raise ValidationError("falta código", user_message="Ingrese el código del contenedor.")
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, principal):
            alcance = _alcance(principal)
            contenedor = self._repo.contenedor_por_codigo(tx, codigo)
            if contenedor is None:
                raise NotFoundError(
                    codigo, user_message="No se encontró el contenedor o no tiene asignaciones visibles."
                )
            filas = self._repo.asignaciones_contenedor(
                tx,
                contenedor.id,
                alcance=alcance,
                limite=MAX_ASIGNACIONES_CONTENEDOR + 1,
            )
            if not filas and not principal.es_interno:
                raise NotFoundError(
                    codigo, user_message="No se encontró el contenedor o no tiene asignaciones visibles."
                )
            truncado = len(filas) > MAX_ASIGNACIONES_CONTENEDOR
            asignaciones = []
            for asignacion in filas[:MAX_ASIGNACIONES_CONTENEDOR]:
                tramos = self._tramos_visibles(tx, principal, asignacion.envio_id)
                recorrido = []
                for tramo in tramos:
                    planificado = (
                        tramo.salida_estimada_en < asignacion.reserva_hasta
                        and tramo.llegada_estimada_en > asignacion.reserva_desde
                    )
                    realizado = _tramo_durante_uso(tramo, asignacion.uso_desde, asignacion.uso_hasta)
                    if planificado or realizado:
                        recorrido.append(
                            TramoContenedorDTO(
                                id=tramo.id,
                                orden=tramo.orden,
                                origen_nombre=tramo.origen_nombre,
                                destino_nombre=tramo.destino_nombre,
                                operador_razon_social=tramo.operador_razon_social,
                                medio=tramo.medio,
                                estado=tramo.estado,
                                salida_estimada_en=tramo.salida_estimada_en,
                                llegada_estimada_en=tramo.llegada_estimada_en,
                                salida_real_en=tramo.salida_real_en,
                                llegada_real_en=tramo.llegada_real_en,
                                planificado_durante_reserva=planificado,
                                realizado_durante_uso=realizado,
                            )
                        )
                asignaciones.append(
                    AsignacionContenedorDTO(
                        id=asignacion.id,
                        envio_id=asignacion.envio_id,
                        envio_codigo=asignacion.envio_codigo,
                        cliente_id=asignacion.cliente_id,
                        cliente_razon_social=asignacion.cliente_razon_social,
                        estado=asignacion.estado,
                        reserva_desde=asignacion.reserva_desde,
                        reserva_hasta=asignacion.reserva_hasta,
                        uso_desde=asignacion.uso_desde,
                        uso_hasta=asignacion.uso_hasta,
                        tramos=recorrido,
                    )
                )
            return RecorridoContenedorDTO(
                contenedor.id, contenedor.codigo_internacional, asignaciones, truncado
            )

    def posiciones_gps(
        self,
        sesion_id: uuid.UUID,
        contenedor_id: uuid.UUID,
        asignacion_id: uuid.UUID,
    ) -> list[PosicionContenedorDTO]:
        """Lee GPS de Mongo dentro del período real de uso de la asignación indicada."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, principal):
            asignacion = self._repo.asignacion_contenedor(
                tx, contenedor_id, asignacion_id, alcance=_alcance(principal)
            )
            if asignacion is None:
                raise PermissionDeniedError("asignación fuera del alcance del usuario")
            tramos = self._tramos_visibles(tx, principal, asignacion.envio_id)
            tramo_ids = [tramo.id for tramo in tramos]
        if asignacion.uso_desde is None:
            return []
        hasta = asignacion.uso_hasta or datetime.now(timezone.utc)
        if asignacion.uso_desde >= hasta:
            return []
        documentos = self._lecturas.posiciones_asignacion(
            envio_id=asignacion.envio_id,
            contenedor_id=contenedor_id,
            desde=asignacion.uso_desde,
            hasta=hasta,
            tramo_ids=tramo_ids if _es_operador(principal) else None,
            limite=MAX_POSICIONES_ASIGNACION,
        )
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, actual_principal):
            actual = self._repo.asignacion_contenedor(
                tx, contenedor_id, asignacion_id, alcance=_alcance(actual_principal)
            )
            actuales = (
                self._tramos_visibles(tx, actual_principal, asignacion.envio_id) if actual is not None else []
            )
            if actual != asignacion or (
                _es_operador(principal) and [tramo.id for tramo in actuales] != tramo_ids
            ):
                raise PermissionDeniedError("el alcance o período de uso cambió; vuelva a consultar")
        return [
            PosicionContenedorDTO(
                _utc(doc["fecha_medicion"]),
                float(doc["posicion"]["coordinates"][1]),
                float(doc["posicion"]["coordinates"][0]),
                doc["sensor_id"],
            )
            for doc in documentos
        ]

    def red(
        self,
        sesion_id: uuid.UUID,
        *,
        cliente_id: uuid.UUID | None = None,
        operador_id: uuid.UUID | None = None,
        envio_id: uuid.UUID | None = None,
        limite: int = 50,
    ) -> RedTrazabilidadDTO:
        """Consulta una red acotada en Neo4j, restringida primero por envíos de PostgreSQL."""
        if not 1 <= limite <= MAX_RELACIONES_RED:
            raise ValidationError("límite inválido", user_message="El límite debe estar entre 1 y 100.")
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, principal):
            if not principal.es_interno:
                if Rol.CLIENTE in principal.roles and cliente_id not in (None, principal.tercero_id):
                    raise PermissionDeniedError("filtro de cliente fuera del alcance")
                if Rol.OPERADOR_LOGISTICO in principal.roles and operador_id not in (
                    None,
                    principal.tercero_id,
                ):
                    raise PermissionDeniedError("filtro de operador fuera del alcance")
                if Rol.OPERADOR_LOGISTICO in principal.roles:
                    operador_id = principal.tercero_id
                if Rol.CLIENTE in principal.roles:
                    cliente_id = principal.tercero_id
            alcance = _alcance(principal)
            ids = self._repo.envios_para_red(
                tx,
                alcance=alcance,
                cliente_filtro=cliente_id,
                operador_filtro=operador_id,
                envio_filtro=envio_id,
                limite=MAX_ENVIOS_RED + 1,
            )
            envios_omitidos = len(ids) > MAX_ENVIOS_RED
            ids = ids[:MAX_ENVIOS_RED]
            if envio_id is not None and envio_id not in ids and not principal.es_interno:
                raise PermissionDeniedError("envío fuera del alcance del usuario")
        if not ids:
            return RedTrazabilidadDTO("SIN_RESULTADOS", [], False, False, 0)

        disponible = True
        filas_grafo: list[dict[str, Any]] = []
        versiones_grafo: dict[uuid.UUID, int] = {}
        try:
            filas_grafo = self._proyeccion.red(
                envio_ids=ids,
                cliente_id=cliente_id,
                operador_id=operador_id,
                limite=limite + 1,
            )
            versiones_grafo = self._proyeccion.versiones(ids)
        except DatabaseUnavailableError:
            disponible = False
            log.warning("Neo4j no disponible al consultar la red de trazabilidad")

        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, principal):
            ids_actuales = self._repo.envios_para_red(
                tx,
                alcance=_alcance(principal),
                cliente_filtro=cliente_id,
                operador_filtro=operador_id,
                envio_filtro=envio_id,
                limite=MAX_ENVIOS_RED + 1,
            )[:MAX_ENVIOS_RED]
            ids_actuales_set = set(ids_actuales)
            ids_resultado = {uuid.UUID(str(fila["envio_id"])) for fila in filas_grafo}
            if not ids_resultado <= ids_actuales_set:
                raise PermissionDeniedError("el alcance cambió durante la consulta; vuelva a consultar")
            versiones_actuales = self._repo.versiones(tx, ids_actuales)
            clientes_actuales = self._repo.clientes_por_envios(tx, ids_actuales)
            operadores_actuales = self._repo.operadores_por_envios(tx, ids_actuales)
            _, nombres_terceros = self._repo.nombres_catalogo(
                tx,
                [],
                list(
                    set(clientes_actuales.values())
                    | {operador for grupo in operadores_actuales.values() for operador in grupo}
                ),
            )
            relaciones = []
            desfasados: set[uuid.UUID] = set()
            for fila in filas_grafo[:limite]:
                eid = uuid.UUID(str(fila["envio_id"]))
                cid = uuid.UUID(str(fila["cliente_id"]))
                oid = uuid.UUID(str(fila["operador_id"]))
                if clientes_actuales.get(eid) != cid or oid not in operadores_actuales.get(eid, set()):
                    continue
                proyectada = fila["version_proyectada"]
                if versiones_actuales.get(eid) != proyectada:
                    desfasados.add(eid)
                relaciones.append(
                    RelacionRedDTO(
                        cliente_id=cid,
                        cliente_razon_social=nombres_terceros.get(cid, f"Empresa {cid}"),
                        envio_id=eid,
                        envio_codigo=fila["envio_codigo"],
                        envio_version_proyectada=proyectada,
                        tramo_id=uuid.UUID(str(fila["tramo_id"])),
                        tramo_orden=fila["tramo_orden"],
                        operador_id=oid,
                        operador_razon_social=nombres_terceros.get(oid, f"Operador {oid}"),
                    )
                )
            faltantes = bool(set(ids_actuales) - set(versiones_grafo)) if disponible else False
            if not disponible:
                estado = "NO_DISPONIBLE"
            elif faltantes:
                estado = "PENDIENTE"
            elif desfasados:
                estado = "ATRASADA"
            elif not relaciones:
                estado = "SIN_RESULTADOS"
            else:
                estado = "ACTUALIZADA"
            return RedTrazabilidadDTO(
                estado,
                relaciones,
                disponible and len(filas_grafo) > limite,
                envios_omitidos,
                len(desfasados),
            )

    def _envio_visible(self, tx: Any, principal: Principal, envio_id: uuid.UUID) -> EnvioDTO:
        if not principal.es_interno and not self._envios.en_alcance(
            tx, envio_id, _alcance(principal)
        ):
            raise PermissionDeniedError(f"envío {envio_id} fuera del alcance")
        envio = self._envios.obtener(tx, envio_id)
        if envio is None:
            raise NotFoundError(f"envío {envio_id}", user_message="El envío no existe.")
        return envio

    def _tramos_visibles(
        self, tx: Any, principal: Principal, envio_id: uuid.UUID
    ) -> list[TramoDTO]:
        tramos = self._tramos.de_envio(tx, envio_id)
        if _es_operador(principal):
            return [tramo for tramo in tramos if tramo.operador_id == principal.tercero_id]
        return tramos

    @staticmethod
    def _validar_filtros_eventos(
        tipo: str | None,
        desde: datetime | None,
        hasta: datetime | None,
        texto: str | None,
        despues: EventoCursor | None,
        limite: int,
    ) -> None:
        if not 1 <= limite <= MAX_PAGINA_EVENTOS:
            raise ValidationError("límite inválido", user_message="El tamaño de página debe ser de 1 a 100.")
        for fecha in (desde, hasta, despues[0] if despues else None):
            if fecha is not None and (fecha.tzinfo is None or fecha.utcoffset() is None):
                raise ValidationError("fecha sin zona", user_message="Las fechas deben incluir zona horaria.")
        if desde is not None and hasta is not None and desde >= hasta:
            raise ValidationError("rango inválido", user_message="Desde debe ser anterior a hasta.")
        if tipo is not None and len(tipo.strip()) > 80:
            raise ValidationError(
                "tipo demasiado largo", user_message="El tipo de evento es demasiado largo."
            )
        if texto is not None and len(texto.strip()) > 120:
            raise ValidationError(
                "texto demasiado largo", user_message="El filtro admite hasta 120 caracteres."
            )
        if despues is not None and not isinstance(despues[1], uuid.UUID):
            raise ValidationError("cursor inválido", user_message="No se pudo continuar la paginación.")


def _alcance(principal: Principal) -> AlcanceConsulta:
    if principal.es_interno:
        return AlcanceConsulta()
    return AlcanceConsulta(
        cliente_id=principal.tercero_id if Rol.CLIENTE in principal.roles else None,
        operador_id=(
            principal.tercero_id if Rol.OPERADOR_LOGISTICO in principal.roles else None
        ),
    )


def _es_operador(principal: Principal) -> bool:
    return Rol.OPERADOR_LOGISTICO in principal.roles


def _tramo_durante_uso(
    tramo: TramoDTO, uso_desde: datetime | None, uso_hasta: datetime | None
) -> bool:
    if uso_desde is None or tramo.salida_real_en is None or tramo.salida_real_en < uso_desde:
        return False
    if uso_hasta is None:
        return tramo.llegada_real_en is None or tramo.llegada_real_en <= datetime.now(timezone.utc)
    return tramo.llegada_real_en is not None and tramo.llegada_real_en <= uso_hasta


def _estado_evento(estado: str | None) -> str:
    if estado is None:
        return "EN_MONGO_SIN_OUTBOX"
    return f"EN_MONGO · OUTBOX_{estado}"


def _estado_proyeccion(
    version_operativa: int,
    version_proyectada: int | None,
    *,
    disponible: bool,
    pendientes: int,
    fallidas: int,
) -> str:
    if not disponible:
        return "NO_DISPONIBLE"
    if version_proyectada is None:
        return "PENDIENTE" if pendientes or fallidas else "SIN_PROYECCION"
    if version_proyectada < version_operativa:
        return "ATRASADA"
    if version_proyectada > version_operativa:
        return "ADELANTADA"
    return "ACTUALIZADA"


def _utc(fecha: datetime) -> datetime:
    return fecha.replace(tzinfo=timezone.utc) if fecha.tzinfo is None else fecha.astimezone(timezone.utc)
