"""Envíos: registro, planificación (tramos y contenedores), ejecución, entrega, cancelación y eventos.

Cada operación que modifica un envío sigue el mismo orden, en UNA transacción de PostgreSQL:
  sesión → permiso → lock del envío (FOR UPDATE) → alcance → reglas → cambio → versión + outbox → auditoría.
El lock serializa las operaciones concurrentes sobre un mismo envío; la versión del agregado (envio.version)
sube en cada cambio y viaja en la outbox, así un consumidor puede descartar eventos viejos (AGENTS.md §9).

SUPUESTOS de esta fase: S1..S10 (ver domain/logistica.py y AGENTS.md §16).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

from logistica.core.errors import BusinessRuleError, NotFoundError, PermissionDeniedError, ValidationError
from logistica.domain.acceso import Permiso, Principal, Rol, RolComercial
from logistica.domain.logistica import (
    EVENTOS_MANUALES,
    AsignacionDatos,
    EnvioDatos,
    EstadoEnvio,
    EstadoRecurso,
    TipoEvento,
    TramoPlan,
    TramoRef,
    exigir_con_zona,
    exigir_editable,
    exigir_transicion,
    generar_codigo_envio,
    validar_capacidad,
    validar_confirmacion_entrega,
    validar_continuidad,
    validar_detalles_evento,
    validar_fin_tramo,
    validar_inicio_tramo,
    validar_total_asignado,
    validar_vehiculo_para_tramo,
)
from logistica.domain.logistica_dtos import (
    AsignacionDTO,
    EnvioDetalleDTO,
    EnvioDTO,
    EventoOutboxDTO,
    TramoDTO,
)
from logistica.repositories.connections import Connections
from logistica.repositories.postgres.catalogos import (
    ContenedorRepository,
    UbicacionRepository,
    VehiculoRepository,
)
from logistica.repositories.postgres.envios import (
    AlcanceConsulta,
    AsignacionRepository,
    EnvioRepository,
    TramoRepository,
)
from logistica.repositories.postgres.outbox import MONGO, NEO4J, OutboxRepository
from logistica.repositories.postgres.terceros import TerceroRepository
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador

AGREGADO = "ENVIO"


class EnvioService:
    def __init__(self, connections: Connections, autorizador: Autorizador):
        self._conn = connections
        self._aut = autorizador
        self._envios = EnvioRepository()
        self._tramos = TramoRepository()
        self._asignaciones = AsignacionRepository()
        self._outbox = OutboxRepository()
        self._terceros = TerceroRepository()
        self._ubicaciones = UbicacionRepository()
        self._vehiculos = VehiculoRepository()
        self._contenedores = ContenedorRepository()

    # ======================================================================= consultas
    def listar(
        self, sesion_id: uuid.UUID, *, estado: str | None = None, texto: str | None = None
    ) -> list[EnvioDTO]:
        """Internos: todos. Cliente: sus envíos. Operador: envíos con al menos un tramo de su empresa."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, p):
            return self._envios.listar(
                tx, alcance=_alcance(p), estado=estado or None, texto=(texto or "").strip() or None
            )

    def obtener(self, sesion_id: uuid.UUID, envio_id: uuid.UUID) -> EnvioDetalleDTO:
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, p):
            envio = self._visible(tx, p, envio_id)
            return self._detalle(tx, p, envio)

    def eventos(self, sesion_id: uuid.UUID, envio_id: uuid.UUID) -> list[EventoOutboxDTO]:
        """Eventos registrados del envío, con su estado de entrega a los otros motores (fase 3)."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CONSULTAR) as (tx, p):
            self._visible(tx, p, envio_id)
            return self._outbox.de_agregado(tx, envio_id)

    # ======================================================================= registro
    def crear(self, sesion_id: uuid.UUID, datos: EnvioDatos) -> EnvioDTO:
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_CREAR) as (tx, p):
            d = datos.normalizado()
            self._exigir_tercero_con_rol(tx, d.cliente_id, RolComercial.CLIENTE, "El cliente")
            for ubicacion_id in (d.origen_id, d.destino_id):
                self._exigir_ubicacion_activa(tx, ubicacion_id)
            envio_id = uuid.uuid4()
            self._envios.crear(
                tx, envio_id, generar_codigo_envio(datetime.now().astimezone()), d, p.usuario_id
            )
            envio = self._envios.obtener(tx, envio_id)
            self._evento(
                tx,
                envio,
                envio.version,
                "ENVIO_CREADO",
                (NEO4J,),
                {
                    "envio_id": envio_id,
                    "codigo": envio.codigo,
                    "cliente_id": d.cliente_id,
                    "origen_id": d.origen_id,
                    "destino_id": d.destino_id,
                    "estado": envio.estado,
                },
            )
            auditoria.registrar(
                tx, "crear_envio", p, entidad="envio", entidad_id=envio_id, codigo=envio.codigo
            )
            return envio

    # ======================================================================= planificación
    def definir_tramos(
        self, sesion_id: uuid.UUID, envio_id: uuid.UUID, tramos: list[TramoPlan]
    ) -> EnvioDetalleDTO:
        """Reemplaza el recorrido completo. Si el envío estaba PLANIFICADO, vuelve a BORRADOR."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (tx, p):
            envio = self._bloquear(tx, envio_id)
            exigir_editable(envio.estado)
            planes = [t.normalizado() for t in tramos]
            validar_continuidad(envio.origen_id, envio.destino_id, [
                TramoRef(i, t.origen_id, t.destino_id, t.salida_estimada_en, t.llegada_estimada_en)
                for i, t in enumerate(planes, start=1)
            ])
            for t in planes:
                self._exigir_tercero_con_rol(tx, t.operador_id, RolComercial.OPERADOR, "El operador del tramo")
                for ubicacion_id in (t.origen_id, t.destino_id):
                    self._exigir_ubicacion_activa(tx, ubicacion_id)
                if t.vehiculo_id is not None:
                    self._validar_vehiculo(tx, t.vehiculo_id, t.medio, t.operador_id)
            ids = self._tramos.reemplazar(tx, envio_id, planes)
            if envio.estado == EstadoEnvio.PLANIFICADO:
                exigir_transicion(envio.estado, EstadoEnvio.BORRADOR)
            version = self._envios.nueva_version(
                tx, envio_id, estado=EstadoEnvio.BORRADOR, entrega_estimada_en=None
            )
            # Con los ids, Neo4j puede reemplazar los nodos Tramo del envío por exactamente estos.
            self._evento(tx, envio, version, "TRAMOS_DEFINIDOS", (NEO4J,), {
                "envio_id": envio_id,
                "tramos": [
                    {"tramo_id": tid, "orden": i, "origen_id": t.origen_id, "destino_id": t.destino_id,
                     "medio": t.medio, "operador_id": t.operador_id}
                    for i, (tid, t) in enumerate(zip(ids, planes), start=1)
                ],
            })
            auditoria.registrar(tx, "definir_tramos", p, entidad="envio", entidad_id=envio_id, tramos=len(planes))
            return self._detalle(tx, p, self._envios.obtener(tx, envio_id))

    def asignar_vehiculo(self, sesion_id: uuid.UUID, tramo_id: uuid.UUID, vehiculo_id: uuid.UUID) -> TramoDTO:
        """El vehículo puede faltar al planificar, pero debe estar antes de iniciar el tramo."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (tx, p):
            tramo = self._tramo(tx, tramo_id)
            envio = self._bloquear(tx, tramo.envio_id)
            if envio.estado not in (EstadoEnvio.BORRADOR, EstadoEnvio.PLANIFICADO, EstadoEnvio.EN_TRANSITO):
                raise BusinessRuleError("envío cerrado", user_message="El envío ya no admite cambios.")
            tramo = self._tramo(tx, tramo_id)  # releído con el envío bloqueado
            if tramo.estado != "PENDIENTE":
                raise BusinessRuleError(
                    "tramo no pendiente", user_message="Solo se cambia el vehículo de un tramo pendiente."
                )
            self._validar_vehiculo(tx, vehiculo_id, tramo.medio, tramo.operador_id)
            self._tramos.asignar_vehiculo(tx, tramo_id, vehiculo_id)
            auditoria.registrar(
                tx, "asignar_vehiculo", p, entidad="tramo", entidad_id=tramo_id, vehiculo_id=vehiculo_id
            )
            return self._tramo(tx, tramo_id)

    def asignar_contenedor(
        self, sesion_id: uuid.UUID, envio_id: uuid.UUID, datos: AsignacionDatos
    ) -> AsignacionDTO:
        """Reserva un contenedor para el envío respetando capacidad y sin reservas superpuestas.

        Locks: el envío (para sumar sus asignaciones sin carreras) y el contenedor (para que dos envíos no lo
        reserven a la vez). Respaldo en la base: la restricción ex_envcont_reserva (EXCLUDE gist).
        """
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (tx, p):
            envio = self._bloquear(tx, envio_id)
            exigir_editable(envio.estado)
            d = datos.normalizado()
            contenedor = self._contenedores.obtener(tx, d.contenedor_id, bloquear=True)
            if contenedor is None:
                raise NotFoundError(f"contenedor {d.contenedor_id}", user_message="El contenedor no existe.")
            if contenedor.estado != EstadoRecurso.HABILITADO:
                raise BusinessRuleError(
                    "contenedor no habilitado", user_message="El contenedor no está habilitado."
                )
            validar_capacidad(
                d.peso_asignado_kg, d.volumen_asignado_m3, contenedor.capacidad_kg, contenedor.capacidad_m3
            )
            peso, volumen = self._asignaciones.totales_vigentes(tx, envio_id)
            validar_total_asignado(
                envio.peso_kg, envio.volumen_m3, peso + d.peso_asignado_kg, volumen + d.volumen_asignado_m3
            )
            if self._asignaciones.reserva_superpuesta(tx, d.contenedor_id, d.reserva_desde, d.reserva_hasta):
                raise BusinessRuleError(
                    "contenedor reservado", user_message="El contenedor ya está reservado en ese período."
                )
            asignacion_id = uuid.uuid4()
            self._asignaciones.crear(tx, asignacion_id, envio_id, d)
            version = self._envios.nueva_version(tx, envio_id)
            self._evento(
                tx,
                envio,
                version,
                "CONTENEDOR_ASIGNADO",
                (NEO4J,),
                {
                    "envio_id": envio_id,
                    "asignacion_id": asignacion_id,
                    "contenedor_id": d.contenedor_id,
                    "desde": d.reserva_desde,
                    "hasta": d.reserva_hasta,
                },
            )
            auditoria.registrar(
                tx,
                "asignar_contenedor",
                p,
                entidad="envio",
                entidad_id=envio_id,
                contenedor=contenedor.codigo_internacional,
            )
            return self._asignaciones.obtener(tx, asignacion_id)

    def cancelar_asignacion(self, sesion_id: uuid.UUID, asignacion_id: uuid.UUID) -> AsignacionDTO:
        """Cancela una reserva. Si un envío PLANIFICADO se queda sin contenedores vuelve a BORRADOR (S4)."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (tx, p):
            asignacion = self._asignaciones.obtener(tx, asignacion_id)
            if asignacion is None:
                raise NotFoundError(f"asignación {asignacion_id}", user_message="La asignación no existe.")
            envio = self._bloquear(tx, asignacion.envio_id)
            exigir_editable(envio.estado)
            asignacion = self._asignaciones.obtener(tx, asignacion_id)  # releída con el envío bloqueado
            if asignacion.estado != "RESERVADA" or not self._asignaciones.cancelar(tx, asignacion_id):
                raise BusinessRuleError("asignación no reservada", user_message="Solo se cancela una reserva.")
            peso, _ = self._asignaciones.totales_vigentes(tx, envio.id)
            if envio.estado == EstadoEnvio.PLANIFICADO and peso == 0:
                version = self._envios.nueva_version(
                    tx, envio.id, estado=EstadoEnvio.BORRADOR, entrega_estimada_en=None
                )
            else:
                version = self._envios.nueva_version(tx, envio.id)
            self._evento(tx, envio, version, "ASIGNACION_CANCELADA", (NEO4J,),
                         {"envio_id": envio.id, "asignacion_id": asignacion_id})
            auditoria.registrar(tx, "cancelar_asignacion", p, entidad="envio", entidad_id=envio.id,
                                contenedor=asignacion.contenedor_codigo)
            return self._asignaciones.obtener(tx, asignacion_id)

    def planificar(self, sesion_id: uuid.UUID, envio_id: uuid.UUID) -> EnvioDTO:
        """BORRADOR → PLANIFICADO. SUPUESTO S4: recorrido continuo y al menos un contenedor asignado."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (tx, p):
            envio = self._bloquear(tx, envio_id)
            exigir_transicion(envio.estado, EstadoEnvio.PLANIFICADO)
            tramos = self._tramos.de_envio(tx, envio_id)
            validar_continuidad(envio.origen_id, envio.destino_id, [_ref(t) for t in tramos])
            peso, _ = self._asignaciones.totales_vigentes(tx, envio_id)
            if peso == 0:
                raise BusinessRuleError("sin contenedor asignado",
                                        user_message="Asigne al menos un contenedor antes de planificar.")
            estimada = tramos[-1].llegada_estimada_en
            if estimada < envio.creado_en:  # mismo criterio que ck_envio_fechas_posteriores
                raise BusinessRuleError(
                    "estimada anterior a la creación",
                    user_message="La llegada estimada del último tramo no puede ser anterior a la creación "
                    "del envío.",
                )
            version = self._envios.nueva_version(
                tx, envio_id, estado=EstadoEnvio.PLANIFICADO, entrega_estimada_en=estimada
            )
            self._evento(tx, envio, version, "ENVIO_PLANIFICADO", (NEO4J,),
                         {"envio_id": envio_id, "entrega_estimada_en": estimada})
            auditoria.registrar(tx, "planificar_envio", p, entidad="envio", entidad_id=envio_id)
            return self._envios.obtener(tx, envio_id)

    def iniciar_tramo(self, sesion_id: uuid.UUID, tramo_id: uuid.UUID, salida_real: datetime) -> TramoDTO:
        """Inicia un tramo. El primero pone el envío EN_TRANSITO y fija el compromiso (SUPUESTO S3)."""
        with self._aut.operacion(sesion_id, Permiso.TRAMOS_ACTUALIZAR) as (tx, p):
            exigir_con_zona(salida_real)
            tramo = self._tramo_en_alcance(tx, p, tramo_id)
            envio = self._bloquear(tx, tramo.envio_id)
            if envio.estado not in (EstadoEnvio.PLANIFICADO, EstadoEnvio.EN_TRANSITO):
                raise BusinessRuleError("envío no planificado",
                                        user_message="El envío debe estar planificado para iniciar un tramo.")
            tramos = self._tramos.de_envio(tx, envio.id)
            tramo = next(t for t in tramos if t.id == tramo_id)  # releído con el envío bloqueado
            anterior = next((t for t in tramos if t.orden == tramo.orden - 1), None)
            validar_inicio_tramo(tramo.orden, tramo.estado, tramo.vehiculo_id, anterior.estado if anterior else None)
            if anterior is not None and salida_real < anterior.llegada_real_en:
                raise BusinessRuleError("sale antes de la llegada del anterior",
                                        user_message=f"El tramo {tramo.orden} no puede salir antes de la llegada "
                                        f"real del tramo {anterior.orden}.")
            self._validar_vehiculo(tx, tramo.vehiculo_id, tramo.medio, tramo.operador_id)
            if envio.estado == EstadoEnvio.PLANIFICADO:
                self._validar_contenedores_para_salir(tx, envio.id, salida_real)
            self._tramos.iniciar(tx, tramo_id, salida_real)
            if envio.estado == EstadoEnvio.PLANIFICADO:
                exigir_transicion(envio.estado, EstadoEnvio.EN_TRANSITO)
                # El compromiso se fija una sola vez (el trigger trg_envio_compromiso_inmutable lo garantiza).
                self._asignaciones.iniciar_uso(tx, envio.id, salida_real)
                version = self._envios.nueva_version(
                    tx, envio.id, estado=EstadoEnvio.EN_TRANSITO,
                    entrega_comprometida_en=envio.entrega_estimada_en,
                )
            else:
                version = self._envios.nueva_version(tx, envio.id)
            self._evento(tx, envio, version, "TRAMO_INICIADO", (MONGO, NEO4J), self._doc_evento(
                tx, envio, TipoEvento.TRAMO_INICIADO, salida_real, p, tramo_id=tramo_id,
                ubicacion_id=tramo.origen_id, detalles={"orden": tramo.orden, "vehiculo_id": str(tramo.vehiculo_id)},
            ))
            auditoria.registrar(tx, "iniciar_tramo", p, entidad="tramo", entidad_id=tramo_id, envio=envio.codigo)
            return self._tramo(tx, tramo_id)

    def _validar_contenedores_para_salir(self, tx: Any, envio_id: uuid.UUID, salida_real: datetime) -> None:
        """Al salir el envío, sus contenedores pasan a EN_USO desde `salida_real` sin fecha de fin.

        Se bloquean y se verifica que ningún otro envío los esté usando en ese momento (por ejemplo, uno que
        se atrasó más allá de su reserva). Respaldo en la base: ex_envcont_uso.
        """
        peso, _ = self._asignaciones.totales_vigentes(tx, envio_id)
        if peso == 0:
            raise BusinessRuleError("sin contenedores", user_message="El envío no tiene contenedores asignados.")
        ocupados = self._asignaciones.contenedores_en_uso_por_otro(tx, envio_id, salida_real)
        if ocupados:
            raise BusinessRuleError(
                f"contenedores en uso: {ocupados}",
                user_message="Contenedor todavía en uso por otro envío: " + ", ".join(ocupados) + ".",
            )

    def finalizar_tramo(self, sesion_id: uuid.UUID, tramo_id: uuid.UUID, llegada_real: datetime) -> TramoDTO:
        with self._aut.operacion(sesion_id, Permiso.TRAMOS_ACTUALIZAR) as (tx, p):
            exigir_con_zona(llegada_real)
            tramo = self._tramo_en_alcance(tx, p, tramo_id)
            envio = self._bloquear(tx, tramo.envio_id)
            tramo = self._tramo(tx, tramo_id)
            validar_fin_tramo(tramo.estado, tramo.salida_real_en, llegada_real)
            self._tramos.finalizar(tx, tramo_id, llegada_real)
            version = self._envios.nueva_version(tx, envio.id)
            self._evento(tx, envio, version, "TRAMO_FINALIZADO", (MONGO, NEO4J), self._doc_evento(
                tx, envio, TipoEvento.TRAMO_FINALIZADO, llegada_real, p, tramo_id=tramo_id,
                ubicacion_id=tramo.destino_id, detalles={"orden": tramo.orden},
            ))
            auditoria.registrar(tx, "finalizar_tramo", p, entidad="tramo", entidad_id=tramo_id, envio=envio.codigo)
            return self._tramo(tx, tramo_id)

    def confirmar_entrega(self, sesion_id: uuid.UUID, envio_id: uuid.UUID, entrega_real: datetime) -> EnvioDTO:
        """La confirma Operaciones (envios.planificar); llegar al último destino no la sustituye."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (tx, p):
            exigir_con_zona(entrega_real)
            envio = self._bloquear(tx, envio_id)
            exigir_transicion(envio.estado, EstadoEnvio.ENTREGADO)
            tramos = self._tramos.de_envio(tx, envio_id)
            llegadas = [t.llegada_real_en for t in tramos if t.llegada_real_en is not None]
            validar_confirmacion_entrega([t.estado for t in tramos], max(llegadas, default=None), entrega_real)
            # Mismos criterios que la base: ck_envio_fechas_posteriores y ck_envcont_uso_orden (uso > 0).
            usos = [a.uso_desde for a in self._asignaciones.de_envio(tx, envio_id) if a.estado == "EN_USO"]
            if entrega_real < envio.creado_en or any(entrega_real <= u for u in usos):
                raise BusinessRuleError(
                    "entrega no posterior al inicio",
                    user_message="La entrega debe ser posterior al inicio del viaje (uso de los contenedores).",
                )
            self._asignaciones.finalizar_uso(tx, envio_id, entrega_real)
            version = self._envios.nueva_version(
                tx, envio_id, estado=EstadoEnvio.ENTREGADO, entrega_real_en=entrega_real
            )
            self._evento(tx, envio, version, "ENVIO_ENTREGADO", (MONGO, NEO4J), self._doc_evento(
                tx, envio, TipoEvento.ENTREGADO, entrega_real, p, ubicacion_id=envio.destino_id,
            ))
            auditoria.registrar(tx, "confirmar_entrega", p, entidad="envio", entidad_id=envio_id)
            return self._envios.obtener(tx, envio_id)

    def cancelar(self, sesion_id: uuid.UUID, envio_id: uuid.UUID, motivo: str) -> EnvioDTO:
        """Solo antes de salir (SUPUESTO S8). Cancela tramos pendientes y libera las reservas de contenedores."""
        with self._aut.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (tx, p):
            motivo = (motivo or "").strip()
            if not motivo:
                raise ValidationError("sin motivo", user_message="Indique el motivo de la cancelación.")
            envio = self._bloquear(tx, envio_id)
            exigir_transicion(envio.estado, EstadoEnvio.CANCELADO)
            # El documento se arma ANTES de cancelar: así lleva los contenedores que tenía el envío.
            doc = self._doc_evento(tx, envio, TipoEvento.CANCELADO, datetime.now().astimezone(), p,
                                   detalles={"motivo": motivo})
            self._tramos.cancelar_pendientes(tx, envio_id)
            self._asignaciones.cancelar_reservadas(tx, envio_id)
            version = self._envios.nueva_version(tx, envio_id, estado=EstadoEnvio.CANCELADO)
            self._evento(tx, envio, version, "ENVIO_CANCELADO", (MONGO, NEO4J), doc)
            auditoria.registrar(tx, "cancelar_envio", p, entidad="envio", entidad_id=envio_id)
            return self._envios.obtener(tx, envio_id)

    def registrar_evento(
        self,
        sesion_id: uuid.UUID,
        envio_id: uuid.UUID,
        tipo: str,
        fecha_ocurrencia: datetime,
        detalles: dict[str, Any],
        *,
        tramo_id: uuid.UUID | None = None,
        ubicacion_id: uuid.UUID | None = None,
        descripcion: str | None = None,
    ) -> None:
        """Registra un evento manual (carga, descarga, retraso, aduana). Va a Mongo vía outbox (SUPUESTO S1)."""
        with self._aut.operacion(sesion_id, Permiso.TRAMOS_ACTUALIZAR) as (tx, p):
            # Alcance ANTES de bloquear o buscar: a un externo no se le revela si un envío ajeno existe.
            if not p.es_interno and not self._envios.en_alcance(
                tx, envio_id, AlcanceConsulta(operador_id=p.tercero_id)
            ):
                raise PermissionDeniedError(f"usuario {p.usuario_id} sin tramos en {envio_id}")
            tipo = (tipo or "").strip().upper()
            if tipo not in EVENTOS_MANUALES:
                raise ValidationError(f"evento {tipo} no manual",
                                      user_message="Ese tipo de evento lo registra el sistema, no se carga a mano.")
            detalles = validar_detalles_evento(tipo, detalles)
            exigir_con_zona(fecha_ocurrencia)
            envio = self._bloquear(tx, envio_id)
            if envio.estado not in (EstadoEnvio.PLANIFICADO, EstadoEnvio.EN_TRANSITO):
                raise BusinessRuleError("envío cerrado",
                                        user_message="Solo se registran eventos de envíos planificados o en tránsito.")
            if tramo_id is not None:
                tramo = self._tramo(tx, tramo_id)
                if tramo.envio_id != envio_id:
                    raise ValidationError("tramo de otro envío", user_message="El tramo no pertenece al envío.")
                if not p.es_interno and tramo.operador_id != p.tercero_id:
                    raise PermissionDeniedError(f"tramo {tramo_id} de otro operador")
            if ubicacion_id is not None and self._ubicaciones.obtener(tx, ubicacion_id) is None:
                raise NotFoundError(f"ubicación {ubicacion_id}", user_message="La ubicación no existe.")
            version = self._envios.nueva_version(tx, envio_id)
            self._evento(tx, envio, version, "EVENTO_REGISTRADO", (MONGO,), self._doc_evento(
                tx, envio, TipoEvento(tipo), fecha_ocurrencia, p, tramo_id=tramo_id, ubicacion_id=ubicacion_id,
                descripcion=(descripcion or "").strip() or None, detalles=detalles,
            ))
            auditoria.registrar(tx, "registrar_evento", p, entidad="envio", entidad_id=envio_id, tipo=tipo)

    def _visible(self, tx: Any, p: Principal, envio_id: uuid.UUID) -> EnvioDTO:
        """Alcance: a un externo no se le revela si un envío ajeno existe (siempre 'sin permiso')."""
        if not p.es_interno and not self._envios.en_alcance(tx, envio_id, _alcance(p)):
            raise PermissionDeniedError(f"usuario {p.usuario_id} fuera de alcance de {envio_id}")
        envio = self._envios.obtener(tx, envio_id)
        if envio is None:
            raise NotFoundError(f"envío {envio_id}", user_message="El envío no existe.")
        return envio

    def _detalle(self, tx: Any, p: Principal, envio: EnvioDTO) -> EnvioDetalleDTO:
        """Arma el detalle aplicando lo que cada perfil puede ver (AGENTS.md §11)."""
        tramos = self._tramos.de_envio(tx, envio.id)
        es_su_cliente = p.tercero_id is not None and p.tercero_id == envio.cliente_id
        if not p.es_interno and not es_su_cliente:
            tramos = [t for t in tramos if t.operador_id == p.tercero_id]  # el operador ve solo sus tramos
        if not p.es_interno:
            # Costos: el cliente no ve costos internos; el operador solo el de sus propios tramos.
            tramos = [t if t.operador_id == p.tercero_id else _sin_costo(t) for t in tramos]
        return EnvioDetalleDTO(envio, tramos, self._asignaciones.de_envio(tx, envio.id))

    def _bloquear(self, tx: Any, envio_id: uuid.UUID) -> EnvioDTO:
        envio = self._envios.obtener(tx, envio_id, bloquear=True)
        if envio is None:
            raise NotFoundError(f"envío {envio_id}", user_message="El envío no existe.")
        return envio

    def _tramo(self, tx: Any, tramo_id: uuid.UUID) -> TramoDTO:
        tramo = self._tramos.obtener(tx, tramo_id)
        if tramo is None:
            raise NotFoundError(f"tramo {tramo_id}", user_message="El tramo no existe.")
        return tramo

    def _tramo_en_alcance(self, tx: Any, p: Principal, tramo_id: uuid.UUID) -> TramoDTO:
        """Un operador solo actúa sobre los tramos de su empresa."""
        tramo = self._tramo(tx, tramo_id)
        if not p.es_interno and tramo.operador_id != p.tercero_id:
            raise PermissionDeniedError(f"usuario {p.usuario_id} sobre tramo ajeno {tramo_id}")
        return tramo

    def _exigir_tercero_con_rol(self, tx: Any, tercero_id: uuid.UUID, rol: RolComercial, quien: str) -> None:
        tercero = self._terceros.obtener(tx, tercero_id)
        if tercero is None or not tercero.activo or rol not in tercero.roles_comerciales:
            raise BusinessRuleError(
                f"tercero {tercero_id} sin rol {rol}",
                user_message=f"{quien} debe ser un tercero activo con rol {rol.value.lower()}.",
            )

    def _exigir_ubicacion_activa(self, tx: Any, ubicacion_id: uuid.UUID) -> None:
        ubicacion = self._ubicaciones.obtener(tx, ubicacion_id)
        if ubicacion is None or not ubicacion.activo:
            raise BusinessRuleError(
                f"ubicación {ubicacion_id} inactiva",
                user_message="Las ubicaciones del envío deben existir y estar activas.",
            )

    def _validar_vehiculo(
        self, tx: Any, vehiculo_id: uuid.UUID | None, medio: str, operador_id: uuid.UUID
    ) -> None:
        vehiculo = self._vehiculos.obtener(tx, vehiculo_id) if vehiculo_id else None
        if vehiculo is None:
            raise BusinessRuleError("vehículo inexistente", user_message="El vehículo indicado no existe.")
        validar_vehiculo_para_tramo(medio, operador_id, vehiculo.tipo, vehiculo.operador_id, vehiculo.estado)

    def _evento(
        self,
        tx: Any,
        envio: EnvioDTO,
        version: int,
        evento_tipo: str,
        destinos: tuple[str, ...],
        payload: dict[str, Any],
    ) -> None:
        self._outbox.registrar(
            tx,
            agregado_tipo=AGREGADO,
            agregado_id=envio.id,
            agregado_version=version,
            evento_tipo=evento_tipo,
            payload=payload,
            destinos=destinos,
        )

    def _doc_evento(
        self,
        tx: Any,
        envio: EnvioDTO,
        tipo: TipoEvento,
        fecha: datetime,
        p: Principal,
        *,
        tramo_id: uuid.UUID | None = None,
        ubicacion_id: uuid.UUID | None = None,
        descripcion: str | None = None,
        detalles: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Payload con la forma de un documento de `eventos_logisticos` (lo escribe en Mongo la fase 3)."""
        contenedores = sorted(
            {
                str(a.contenedor_id)
                for a in self._asignaciones.de_envio(tx, envio.id)
                if a.estado in ("RESERVADA", "EN_USO", "FINALIZADA")
            }
        )
        doc = {
            "tipo": tipo.value,
            "envio_id": envio.id,
            "fecha_ocurrencia": fecha,
            "usuario_id": p.usuario_id,
            "contenedor_ids": contenedores,
            "detalles": detalles or {},
        }
        if tramo_id is not None:
            doc["tramo_id"] = tramo_id
        if ubicacion_id is not None:
            doc["ubicacion_id"] = ubicacion_id
        if descripcion:
            doc["descripcion"] = descripcion
        return doc


def _alcance(p: Principal) -> AlcanceConsulta:
    if p.es_interno:
        return AlcanceConsulta()
    return AlcanceConsulta(
        cliente_id=p.tercero_id if Rol.CLIENTE in p.roles else None,
        operador_id=p.tercero_id if Rol.OPERADOR_LOGISTICO in p.roles else None,
    )


def _ref(t: TramoDTO) -> TramoRef:
    return TramoRef(t.orden, t.origen_id, t.destino_id, t.salida_estimada_en, t.llegada_estimada_en)


def _sin_costo(t: TramoDTO) -> TramoDTO:
    return replace(t, costo=None, moneda_codigo=None)
