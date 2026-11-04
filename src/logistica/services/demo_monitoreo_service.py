"""Demo explícita, aislada y auditable mediante los servicios públicos. No modifica datos preexistentes."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Callable

from logistica.core.errors import BusinessRuleError, PermissionDeniedError
from logistica.domain.acceso import Permiso, TerceroDatos
from logistica.domain.logistica import (
    AsignacionDatos, ContenedorDatos, EnvioDatos, SensorDatos, TramoPlan, UbicacionDatos, VehiculoDatos,
    exigir_con_zona,
)
from logistica.domain.monitoreo_dtos import CondicionDatos
from logistica.domain.simulacion_iot import pasos_escenario
from logistica.repositories.connections import Connections
from logistica.services.autorizacion import Autorizador
from logistica.services.catalogo_service import CatalogoService
from logistica.services.envio_service import EnvioService
from logistica.services.monitoreo_service import LecturaEntrada, MonitoreoService
from logistica.services.tercero_service import TerceroService

log = logging.getLogger(__name__)
PREFIJO = "DEMO-MON-"


def preparar_demo(connections: Connections, autorizador: Autorizador, sesion_id: uuid.UUID,
                  escenario: str = "completo") -> uuid.UUID:
    """Crea un viaje nuevo y reproduce 21 minutos SIMULADOS en el pasado; queda en tránsito.

    Cada llamada crea recursos propios con prefijo único. Si falla, quedan los pasos ya confirmados con ese
    prefijo (se informa el error, no se borran). Reenviar lecturas del mismo viaje es idempotente.
    """
    with autorizador.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (_tx, p):
        if "ADMINISTRADOR" not in p.roles:
            raise PermissionDeniedError("la preparación de demo requiere Administración")
    etiqueta = PREFIJO + uuid.uuid4().hex[:10].upper()
    log.info("Preparando escenario independiente %s", etiqueta)
    cat = CatalogoService(connections, autorizador)
    env = EnvioService(connections, autorizador)
    terceros = TerceroService(connections, autorizador)
    pasos = pasos_escenario(escenario)
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    inicio = ahora - timedelta(seconds=pasos[-1].segundo + 5)
    empresa = terceros.crear(sesion_id, TerceroDatos(
        etiqueta + " SIMULADOR", "AR", "DEMO", etiqueta), {"CLIENTE", "OPERADOR"})
    origen = cat.crear_ubicacion(sesion_id, UbicacionDatos(
        etiqueta + " Buenos Aires", "DEPOSITO", "AR", "Buenos Aires", latitud="-34.60", longitud="-58.38"))
    destino = cat.crear_ubicacion(sesion_id, UbicacionDatos(
        etiqueta + " Rosario", "DEPOSITO", "AR", "Rosario", latitud="-32.95", longitud="-60.64"))
    vehiculo = cat.crear_vehiculo(sesion_id, VehiculoDatos(empresa.id, "CAMION", etiqueta))
    contenedor = cat.crear_contenedor(sesion_id, ContenedorDatos(etiqueta, "REFRIGERADO", "1000", "10"))
    sensor = cat.crear_sensor(sesion_id, SensorDatos(
        etiqueta, "MULTISENSOR", "SIMULADOR", capacidades=frozenset({"TEMPERATURA", "GPS", "BATERIA"})))
    cat.instalar_sensor(sesion_id, sensor.id, contenedor.id, inicio)
    envio = env.crear(sesion_id, EnvioDatos(
        empresa.id, origen.id, destino.id, etiqueta + " · carga SIMULADOR independiente", "100", "1"))
    env.definir_tramos(sesion_id, envio.id, [TramoPlan(
        origen.id, destino.id, "TERRESTRE", empresa.id, ahora + timedelta(minutes=1),
        ahora + timedelta(days=1), vehiculo.id)])
    env.asignar_contenedor(sesion_id, envio.id, AsignacionDatos(
        contenedor.id, "100", "1", inicio, ahora + timedelta(days=2)))
    mon = MonitoreoService(connections, autorizador)
    mon.definir_condiciones(sesion_id, envio.id, CondicionDatos(
        temperatura_min_c="2", temperatura_max_c="8", bateria_min_pct="20"))
    env.planificar(sesion_id, envio.id)
    env.iniciar_tramo(sesion_id, env.obtener(sesion_id, envio.id).tramos[0].id, inicio)
    reproducir_demo(connections, autorizador, sesion_id, envio.id, escenario, reloj_simulado=True)
    log.info("Demo %s lista: envío %s (%s), sensor %s", etiqueta, envio.codigo, envio.id, sensor.id)
    return envio.id


def reproducir_demo(connections: Connections, autorizador: Autorizador, sesion_id: uuid.UUID,
                     envio_id: uuid.UUID, escenario: str, *, reloj_simulado: bool,
                     detener: Event | None = None,
                     inicio_escenario: datetime | None = None,
                     informar: Callable[[str], None] = log.info) -> int:
    """Reenvía UUID estables, solo sobre una demo identificada. Reloj acelerado exclusivamente SIMULADOR."""
    with autorizador.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR) as (_tx, p):
        if not p.es_interno:
            raise PermissionDeniedError("simulador reservado a perfiles internos")
    env = EnvioService(connections, autorizador).obtener(sesion_id, envio_id)
    if not env.envio.descripcion_carga.startswith(PREFIJO) or env.envio.estado != "EN_TRANSITO":
        raise BusinessRuleError("envío ajeno a la demo o cerrado",
                                user_message="Solo se reproducen viajes DEMO-MON en tránsito.")
    sensores = MonitoreoService(connections, autorizador).estado_sensores(sesion_id, envio_id=envio_id)
    if len(sensores) != 1 or not sensores[0].sensor_codigo.startswith(PREFIJO):
        raise BusinessRuleError(
            "sensor ajeno a la demo", user_message="La demo requiere su sensor exclusivo.")
    sensor_id = sensores[0].sensor_id
    salida = env.tramos[0].salida_real_en
    if salida is None:
        raise BusinessRuleError("demo sin salida", user_message="Primero iniciá el tramo de la demo.")
    exigir_con_zona(inicio_escenario)
    inicio = inicio_escenario or salida
    if inicio < salida:
        raise BusinessRuleError("escenario anterior a la salida",
                                user_message="El escenario no puede empezar antes de la salida de la demo.")
    parada = detener if detener is not None else Event()
    reloj = [inicio]
    mon = MonitoreoService(connections, autorizador, reloj=(lambda: reloj[0]) if reloj_simulado else None)
    nuevas = 0
    for paso in pasos_escenario(escenario):
        fecha = inicio + timedelta(seconds=paso.segundo)
        if not reloj_simulado and parada.wait(max(0, (fecha - datetime.now(timezone.utc)).total_seconds())):
            break
        if parada.is_set():
            break
        with autorizador.operacion(sesion_id, Permiso.ENVIOS_PLANIFICAR):
            pass
        reloj[0] = fecha + timedelta(seconds=1)
        if paso.temperatura is None:
            cambios = mon.evaluar_comunicacion(sensor_ids=[sensor_id])
            informar(f"+{paso.segundo}s silencio: {cambios} cambios de comunicación")
            continue
        lectura_id = uuid.uuid5(sensor_id, f"{escenario}:{fecha.isoformat()}")
        avance = paso.segundo / 1260
        entrada = LecturaEntrada(
            lectura_id, sensor_id, fecha, {"temperatura_c": paso.temperatura, "bateria_pct": 90},
            latitud=-34.60 + 1.65 * avance, longitud=-58.38 - 2.26 * avance, origen="SIMULADOR")
        nueva = mon.registrar_lectura(entrada)
        nuevas += nueva
        resultado = mon.evaluar_pendientes(sensor_ids=[sensor_id], detener=parada)
        if resultado.reintentos or resultado.revision:
            raise BusinessRuleError(
                "demo pendiente de revisión/reintento",
                user_message="La demo tiene lecturas pendientes o para revisión. Ver logs.")
        informar(f"+{paso.segundo}s {paso.temperatura} °C · "
                 f"{'nueva' if nueva else 'reenvío'} · {lectura_id}")
    return nuevas
