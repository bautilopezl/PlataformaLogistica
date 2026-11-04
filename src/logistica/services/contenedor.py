"""Arma los servicios de la aplicación a partir de la configuración (raíz de composición).

La UI recibe un `Servicios` y nunca crea repositorios ni conexiones por su cuenta.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from logistica.core.config import AppConfig
from logistica.domain.passwords import Hasher
from logistica.repositories.connections import Connections
from logistica.services.acceso_service import AccesoService
from logistica.services.auditoria_service import AuditoriaService
from logistica.services.catalogo_service import CatalogoService
from logistica.services.comunicacion_service import ComunicacionService
from logistica.services.envio_service import EnvioService
from logistica.services.monitoreo_service import MonitoreoService
from logistica.services.sincronizacion_service import SincronizacionService
from logistica.services.autorizacion import Autorizador
from logistica.services.health_service import HealthService
from logistica.services.trazabilidad_service import TrazabilidadService
from logistica.services.tercero_service import TerceroService
from logistica.services.usuario_service import UsuarioService


@dataclass(frozen=True)
class Servicios:
    salud: HealthService
    acceso: AccesoService
    usuarios: UsuarioService
    terceros: TerceroService
    auditoria: AuditoriaService
    catalogo: CatalogoService
    envios: EnvioService
    sincronizacion: SincronizacionService
    monitoreo: MonitoreoService
    trazabilidad: TrazabilidadService
    comunicacion: ComunicacionService


def crear_servicios(config: AppConfig, connections: Connections, instancia_id: uuid.UUID) -> Servicios:
    s = config.security
    hasher = Hasher(s.argon2_time_cost, s.argon2_memory_kib, s.argon2_parallelism)
    autorizador = Autorizador(connections)
    return Servicios(
        salud=HealthService(connections, timeout_s=config.db_timeout_s),
        acceso=AccesoService(connections, hasher, s, instancia_id, autorizador),
        usuarios=UsuarioService(connections, hasher, autorizador),
        terceros=TerceroService(connections, autorizador),
        auditoria=AuditoriaService(connections, autorizador),
        catalogo=CatalogoService(connections, autorizador),
        sincronizacion=SincronizacionService(connections),
        envios=EnvioService(connections, autorizador),
        monitoreo=MonitoreoService(connections, autorizador),
        trazabilidad=TrazabilidadService(connections, autorizador),
        comunicacion=ComunicacionService(connections, autorizador),
    )
