"""Catálogos logísticos: ubicaciones, vehículos, contenedores, sensores y su instalación en contenedores.

SUPUESTO S9: no hay un permiso específico para administrar estos recursos; se usa `envios.planificar`
(la matriz de AGENTS.md §11 se lo da a Administración y Operaciones). Consultarlos lo puede hacer también
quien crea envíos. Los usuarios externos no los consultan.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from logistica.core.errors import BusinessRuleError, NotFoundError, PermissionDeniedError, ValidationError
from logistica.domain.acceso import Permiso, RolComercial
from logistica.domain.logistica import (
    ContenedorDatos,
    EstadoRecurso,
    SensorDatos,
    UbicacionDatos,
    VehiculoDatos,
    exigir_con_zona,
)
from logistica.domain.logistica_dtos import (
    ContenedorDTO,
    InstalacionDTO,
    SensorDTO,
    UbicacionDTO,
    VehiculoDTO,
)
from logistica.repositories.connections import Connections
from logistica.repositories.postgres.catalogos import (
    ContenedorRepository,
    SensorRepository,
    UbicacionRepository,
    VehiculoRepository,
)
from logistica.repositories.postgres.terceros import TerceroRepository
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador

GESTIONAR = Permiso.ENVIOS_PLANIFICAR
CONSULTAR = (Permiso.ENVIOS_PLANIFICAR, Permiso.ENVIOS_CREAR)


class CatalogoService:
    def __init__(self, connections: Connections, autorizador: Autorizador):
        self._conn = connections
        self._aut = autorizador
        self._ubicaciones = UbicacionRepository()
        self._vehiculos = VehiculoRepository()
        self._contenedores = ContenedorRepository()
        self._sensores = SensorRepository()
        self._terceros = TerceroRepository()

    # --- ubicaciones --------------------------------------------------------
    def listar_ubicaciones(
        self, sesion_id: uuid.UUID, *, texto: str | None = None, incluir_inactivas: bool = False
    ) -> list[UbicacionDTO]:
        with self._consulta(sesion_id) as tx:
            return self._ubicaciones.listar(
                tx, texto=(texto or "").strip() or None, incluir_inactivas=incluir_inactivas
            )

    def crear_ubicacion(self, sesion_id: uuid.UUID, datos: UbicacionDatos) -> UbicacionDTO:
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            datos = datos.normalizado()
            uid = uuid.uuid4()
            self._ubicaciones.crear(tx, uid, datos)
            auditoria.registrar(tx, "crear_ubicacion", p, entidad="ubicacion", entidad_id=uid)
            return self._ubicaciones.obtener(tx, uid)

    def actualizar_ubicacion(
        self, sesion_id: uuid.UUID, ubicacion_id: uuid.UUID, datos: UbicacionDatos
    ) -> UbicacionDTO:
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            datos = datos.normalizado()
            if not self._ubicaciones.actualizar(tx, ubicacion_id, datos):
                raise NotFoundError(f"ubicación {ubicacion_id}", user_message="La ubicación no existe.")
            auditoria.registrar(tx, "actualizar_ubicacion", p, entidad="ubicacion", entidad_id=ubicacion_id)
            return self._ubicaciones.obtener(tx, ubicacion_id)

    def set_ubicacion_activa(
        self, sesion_id: uuid.UUID, ubicacion_id: uuid.UUID, activa: bool
    ) -> UbicacionDTO:
        """Baja lógica: los envíos y tramos que ya la usan la conservan."""
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            if not self._ubicaciones.set_activa(tx, ubicacion_id, activa):
                raise NotFoundError(f"ubicación {ubicacion_id}", user_message="La ubicación no existe.")
            auditoria.registrar(
                tx,
                "activar_ubicacion" if activa else "desactivar_ubicacion",
                p,
                entidad="ubicacion",
                entidad_id=ubicacion_id,
            )
            return self._ubicaciones.obtener(tx, ubicacion_id)

    # --- vehículos ----------------------------------------------------------
    def listar_vehiculos(
        self, sesion_id: uuid.UUID, *, operador_id: uuid.UUID | None = None
    ) -> list[VehiculoDTO]:
        with self._consulta(sesion_id) as tx:
            return self._vehiculos.listar(tx, operador_id=operador_id)

    def crear_vehiculo(self, sesion_id: uuid.UUID, datos: VehiculoDatos) -> VehiculoDTO:
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            datos = datos.normalizado()
            operador = self._terceros.obtener(tx, datos.operador_id)
            if (
                operador is None
                or not operador.activo
                or RolComercial.OPERADOR not in operador.roles_comerciales
            ):
                raise BusinessRuleError(
                    "no es operador",
                    user_message="El vehículo debe pertenecer a un operador logístico activo.",
                )
            if self._vehiculos.existe(tx, datos):
                raise ValidationError(
                    "vehículo duplicado",
                    user_message="Ese operador ya tiene un vehículo de ese tipo con esa identificación.",
                )
            vid = uuid.uuid4()
            self._vehiculos.crear(tx, vid, datos)
            auditoria.registrar(tx, "crear_vehiculo", p, entidad="vehiculo", entidad_id=vid)
            return self._vehiculos.obtener(tx, vid)

    def set_estado_vehiculo(self, sesion_id: uuid.UUID, vehiculo_id: uuid.UUID, estado: str) -> VehiculoDTO:
        def guarda(tx: Any) -> None:
            if self._vehiculos.tiene_tramos_abiertos(tx, vehiculo_id):
                raise BusinessRuleError("vehículo con tramos sin terminar",
                                        user_message="El vehículo está asignado a tramos sin terminar.")

        return self._set_estado(sesion_id, "vehiculo", vehiculo_id, estado, self._vehiculos, guarda)

    def listar_contenedores(self, sesion_id: uuid.UUID, *, texto: str | None = None) -> list[ContenedorDTO]:
        with self._consulta(sesion_id) as tx:
            return self._contenedores.listar(tx, texto=(texto or "").strip() or None)

    def crear_contenedor(self, sesion_id: uuid.UUID, datos: ContenedorDatos) -> ContenedorDTO:
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            datos = datos.normalizado()
            if self._contenedores.existe_codigo(tx, datos.codigo_internacional):
                raise ValidationError(
                    "contenedor duplicado", user_message="Ya existe un contenedor con ese código."
                )
            cid = uuid.uuid4()
            self._contenedores.crear(tx, cid, datos)
            auditoria.registrar(tx, "crear_contenedor", p, entidad="contenedor", entidad_id=cid)
            return self._contenedores.obtener(tx, cid)

    def set_estado_contenedor(self, sesion_id: uuid.UUID, contenedor_id: uuid.UUID, estado: str) -> ContenedorDTO:
        def guarda(tx: Any) -> None:
            self._contenedores.obtener(tx, contenedor_id, bloquear=True)  # serializa con asignar_contenedor
            if self._contenedores.tiene_asignaciones_activas(tx, contenedor_id):
                raise BusinessRuleError("contenedor con asignaciones activas",
                                        user_message="El contenedor tiene asignaciones activas (reservado o en uso).")

        return self._set_estado(sesion_id, "contenedor", contenedor_id, estado, self._contenedores, guarda)

    def listar_sensores(self, sesion_id: uuid.UUID) -> list[SensorDTO]:
        with self._consulta(sesion_id) as tx:
            return self._sensores.listar(tx)

    def crear_sensor(self, sesion_id: uuid.UUID, datos: SensorDatos) -> SensorDTO:
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            datos = datos.normalizado()
            if self._sensores.existe_codigo(tx, datos.codigo):
                raise ValidationError("sensor duplicado", user_message="Ya existe un sensor con ese código.")
            sid = uuid.uuid4()
            self._sensores.crear(tx, sid, datos)
            auditoria.registrar(
                tx, "crear_sensor", p, entidad="sensor", entidad_id=sid, capacidades=sorted(datos.capacidades)
            )
            return self._sensores.obtener(tx, sid)

    def set_estado_sensor(self, sesion_id: uuid.UUID, sensor_id: uuid.UUID, estado: str) -> SensorDTO:
        def guarda(tx: Any) -> None:
            sensor = self._sensores.obtener(tx, sensor_id, bloquear=True)
            if sensor is not None and sensor.contenedor_actual is not None:
                raise BusinessRuleError("sensor instalado",
                                        user_message="El sensor está instalado en un contenedor. Retírelo primero.")

        return self._set_estado(sesion_id, "sensor", sensor_id, estado, self._sensores, guarda)

    def instalar_sensor(
        self,
        sesion_id: uuid.UUID,
        sensor_id: uuid.UUID,
        contenedor_id: uuid.UUID,
        instalado_en: datetime,
        observaciones: str | None = None,
    ) -> InstalacionDTO:
        """Un sensor no puede estar instalado en dos contenedores a la vez (respaldo:
        ex_sensorcont_solapamiento).
        """
        exigir_con_zona(instalado_en)
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            sensor = self._sensores.obtener(tx, sensor_id, bloquear=True)
            if sensor is None:
                raise NotFoundError(f"sensor {sensor_id}", user_message="El sensor no existe.")
            if sensor.estado != EstadoRecurso.HABILITADO:
                raise BusinessRuleError("sensor no habilitado", user_message="El sensor no está habilitado.")
            contenedor = self._contenedores.obtener(tx, contenedor_id)
            if contenedor is None:
                raise NotFoundError(f"contenedor {contenedor_id}", user_message="El contenedor no existe.")
            if contenedor.estado == EstadoRecurso.BAJA:
                raise BusinessRuleError("contenedor de baja", user_message="El contenedor está dado de baja.")
            if self._sensores.hay_solapamiento(tx, sensor_id, instalado_en):
                raise BusinessRuleError(
                    "instalación superpuesta",
                    user_message="El sensor ya está instalado en ese período. Retírelo primero.",
                )
            iid = uuid.uuid4()
            self._sensores.instalar(
                tx,
                iid,
                sensor_id,
                contenedor_id,
                instalado_en,
                p.usuario_id,
                (observaciones or "").strip() or None,
            )
            auditoria.registrar(
                tx,
                "instalar_sensor",
                p,
                entidad="sensor",
                entidad_id=sensor_id,
                contenedor=contenedor.codigo_internacional,
            )
            return self._sensores.instalacion(tx, iid)

    def retirar_sensor(
        self, sesion_id: uuid.UUID, instalacion_id: uuid.UUID, retirado_en: datetime
    ) -> InstalacionDTO:
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            exigir_con_zona(retirado_en)
            inst = self._sensores.instalacion(tx, instalacion_id)
            if inst is None:
                raise NotFoundError(f"instalación {instalacion_id}", user_message="La instalación no existe.")
            self._sensores.obtener(tx, inst.sensor_id, bloquear=True)  # serializa con instalar_sensor
            inst = self._sensores.instalacion(tx, instalacion_id)  # releída con el sensor bloqueado
            if inst.retirado_en is not None:
                raise BusinessRuleError("ya retirado", user_message="El sensor ya fue retirado de ese contenedor.")
            if retirado_en <= inst.instalado_en:
                raise BusinessRuleError("retiro antes de instalar",
                                        user_message="El retiro debe ser posterior a la instalación.")
            if not self._sensores.retirar(tx, instalacion_id, retirado_en):
                raise BusinessRuleError("ya retirado", user_message="El sensor ya fue retirado de ese contenedor.")
            auditoria.registrar(tx, "retirar_sensor", p, entidad="sensor", entidad_id=inst.sensor_id,
                                contenedor=inst.contenedor_codigo)
            return self._sensores.instalacion(tx, instalacion_id)

    def historial_sensor(self, sesion_id: uuid.UUID, sensor_id: uuid.UUID) -> list[InstalacionDTO]:
        """Instalaciones del sensor, la más reciente primero."""
        with self._consulta(sesion_id) as tx:
            return self._sensores.historial(tx, sensor_id)

    # --- auxiliares ---------------------------------------------------------
    @contextmanager
    def _consulta(self, sesion_id: uuid.UUID) -> Iterator[Any]:
        """Consulta de catálogos: sesión → permiso → alcance (solo usuarios internos)."""
        with self._aut.operacion(sesion_id, *CONSULTAR) as (tx, p):
            if not p.es_interno:
                raise PermissionDeniedError(f"usuario externo {p.usuario_id} consultando catálogos")
            yield tx

    def _set_estado(self, sesion_id: uuid.UUID, entidad: str, recurso_id: uuid.UUID, estado: str, repo: Any,
                    guarda: Any = None):
        """Cambia el estado de un recurso. `guarda(tx)` se ejecuta antes si el recurso deja de estar habilitado."""
        with self._aut.operacion(sesion_id, GESTIONAR) as (tx, p):
            estado = (estado or "").strip().upper()
            if estado not in {e.value for e in EstadoRecurso}:
                raise ValidationError(f"estado {estado}", user_message="Estado inválido.")
            if guarda is not None and estado != EstadoRecurso.HABILITADO:
                guarda(tx)
            if not repo.set_estado(tx, recurso_id, estado):
                raise NotFoundError(f"{entidad} {recurso_id}", user_message="El registro no existe.")
            auditoria.registrar(tx, f"estado_{entidad}", p, entidad=entidad, entidad_id=recurso_id, estado=estado)
            return repo.obtener(tx, recurso_id)

