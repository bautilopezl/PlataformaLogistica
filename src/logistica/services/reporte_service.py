"""Reportes operativos, analíticos y comerciales (AGENTS.md §12): consulta en pantalla, generación registrada
en `reporte` y apertura del archivo.

- Catálogo = Strategy: cada tipo declara su permiso, si un perfil externo puede pedirlo, qué filtros usa y qué
  función pura del dominio (`domain/analitica.py`) arma la tabla.
- Generar y abrir comprueban permisos POR SEPARADO (§11). Generar pasa por PENDIENTE → EN_PROCESO → COMPLETADO
  o ERROR, cada paso confirmado: si la sesión se revoca durante la generación, el resultado se descarta
  (ERROR).
- Un cliente solo ve SUS datos (el filtro de cliente se fuerza a su empresa) y nunca reportes con costos
  internos.
SUPUESTOS R1..R6: ver AGENTS.md §16 ("Supuestos de la fase 7").
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from logistica.core.errors import BusinessRuleError, LogisticaError, NotFoundError, PermissionDeniedError
from logistica.domain import analitica as da
from logistica.domain.acceso import Permiso, Principal, Rol
from logistica.repositories.archivos import ArchivosReportes
from logistica.repositories.connections import Connections
from logistica.repositories.postgres.analitica import (
    HechosRepository,
    PrediccionRepository,
    ReporteDTO,
    ReporteRepository,
)
from logistica.repositories.postgres.monitoreo import ahora as ahora_pg
from logistica.services import auditoria
from logistica.services.autorizacion import Autorizador
from logistica.services.envio_service import _alcance

log = logging.getLogger(__name__)

OPERATIVO, ANALITICO, COMERCIAL = "Operativo", "Analítico", "Comercial"
FORMATOS = ("PDF", "CSV")


@dataclass(frozen=True)
class TipoReporte:
    codigo: str
    titulo: str
    grupo: str
    permiso: str
    externos: bool  # un cliente puede pedirlo (con SUS datos)
    usa: frozenset[str]  # filtros que muestra la pantalla: rango, cliente, dimension, segun
    fuente: str  # envios | incidentes | alertas | predicciones
    construir: Callable[..., da.Tabla]


@dataclass(frozen=True)
class TipoReporteDTO:
    codigo: str
    titulo: str
    grupo: str
    usa: frozenset[str]


def _t(
    codigo,
    titulo,
    grupo,
    construir,
    *,
    fuente="envios",
    usa=("rango", "cliente"),
    externos=True,
    permiso=Permiso.REPORTES_OPERATIVOS,
):
    return TipoReporte(codigo, titulo, grupo, permiso, externos, frozenset(usa), fuente, construir)


# SUPUESTO R6: qué reportes puede pedir un CLIENTE (siempre con sus datos). Los que comparan clientes entre sí
# (por cliente, rankings, tiempos por cliente) y los de costos internos son solo para usuarios internos.
CATALOGO: dict[str, TipoReporte] = {
    t.codigo: t
    for t in (
        _t(
            "ENVIOS_POR_PAIS",
            "Envíos por país",
            OPERATIVO,
            lambda d, p, a, _interno: da.envios_por_pais(d, p),
            usa=("rango", "cliente", "segun"),
        ),
        _t(
            "ENVIOS_POR_ESTADO",
            "Envíos por estado",
            OPERATIVO,
            lambda d, p, a, _interno: da.envios_por_estado(d, p),
        ),
        _t(
            "ENVIOS_POR_CLIENTE",
            "Envíos por cliente",
            OPERATIVO,
            lambda d, p, a, _interno: da.envios_por_cliente(d, p),
            externos=False,
        ),
        _t(
            "ENVIOS_DEMORADOS",
            "Envíos demorados",
            OPERATIVO,
            lambda d, p, a, _interno: da.envios_demorados(d, p, a),
        ),
        _t(
            "INCIDENTES_ABIERTOS",
            "Incidentes abiertos",
            OPERATIVO,
            lambda d, p, a, interno: da.incidentes_abiertos(d, a, mostrar_responsable=interno),
            fuente="incidentes",
            usa=("cliente",),
        ),
        _t(
            "TIEMPOS_POR_CLIENTE",
            "Tiempos máximo y mínimo de envío por cliente",
            ANALITICO,
            lambda d, p, a, _interno: da.tiempos_por_cliente(d, p),
            externos=False,
        ),
        _t(
            "INCIDENTES_PROMEDIO",
            "Promedio de incidentes por país, estado o cliente",
            ANALITICO,
            lambda d, p, a, _interno: da.incidentes_promedio(d, p),
            usa=("rango", "cliente", "dimension"),
            externos=False,
        ),
        _t(
            "TIEMPOS_PROMEDIO_ENTREGA",
            "Tiempos promedio de entrega",
            ANALITICO,
            lambda d, p, a, _interno: da.tiempos_promedio_entrega(d, p),
        ),
        _t(
            "RANKING_RUTAS_INCIDENTES",
            "Ranking de rutas con más incidentes",
            ANALITICO,
            lambda d, p, a, _interno: da.ranking_rutas_incidentes(d, p),
            externos=False,
        ),
        _t(
            "RANKING_CLIENTES_VOLUMEN",
            "Ranking de clientes por volumen transportado",
            ANALITICO,
            lambda d, p, a, _interno: da.ranking_clientes_volumen(d, p),
            usa=("rango",),
            externos=False,
        ),
        _t(
            "DESEMPENO_HISTORICO",
            "Análisis histórico de desempeño",
            ANALITICO,
            lambda d, p, a, _interno: da.desempeno_historico(d, p),
        ),
        _t(
            "ANOMALIAS_TEMPERATURA",
            "Detección de anomalías de temperatura",
            ANALITICO,
            lambda d, p, a, _interno: da.anomalias_temperatura(d, p, a),
            fuente="alertas",
        ),
        _t(
            "PREDICCION_ATRASOS",
            "Predicción de atrasos (riesgo vigente)",
            ANALITICO,
            None,
            fuente="predicciones",
            usa=(),
            permiso=Permiso.PREDICCIONES_CONSULTAR,
        ),
        _t(
            "COSTOS_POR_TRANSPORTE",
            "Costos por tipo de transporte",
            COMERCIAL,
            lambda d, p, a, _interno: da.costos_por_transporte(d, p),
            usa=("rango", "cliente"),
            externos=False,
            permiso=Permiso.REPORTES_COMERCIALES,
        ),
    )
}
PERMISOS_REPORTES = (
    Permiso.REPORTES_OPERATIVOS,
    Permiso.REPORTES_COMERCIALES,
    Permiso.PREDICCIONES_CONSULTAR,
)


class ReporteService:
    def __init__(self, connections: Connections, autorizador: Autorizador, carpeta: Path):
        self._conn = connections
        self._aut = autorizador
        self._hechos = HechosRepository()
        self._reportes = ReporteRepository()
        self._predicciones = PrediccionRepository()
        self._archivos = ArchivosReportes(carpeta)

    # ------------------------------------------------------------------ catálogo
    def tipos(self, sesion_id: uuid.UUID) -> list[TipoReporteDTO]:
        with self._aut.operacion(sesion_id, *PERMISOS_REPORTES) as (_tx, p):
            return [
                TipoReporteDTO(t.codigo, t.titulo, t.grupo, t.usa) for t in CATALOGO.values() if _puede(p, t)
            ]

    # ------------------------------------------------------------------ consulta en pantalla
    def consultar(self, sesion_id: uuid.UUID, codigo: str, parametros: da.Parametros) -> da.Tabla:
        tipo = _tipo(codigo)
        with self._aut.operacion(sesion_id, tipo.permiso) as (tx, p):
            return self._tabla(tx, p, tipo, parametros)

    def _tabla(self, tx: Any, p: Principal, tipo: TipoReporte, parametros: da.Parametros) -> da.Tabla:
        if not _puede(p, tipo):
            raise PermissionDeniedError(f"{p.usuario_id} no puede pedir {tipo.codigo}")
        parametros = parametros.normalizado()
        if not p.es_interno and p.tercero_id is None:  # nunca "todos los clientes" para un externo
            raise PermissionDeniedError(f"{p.usuario_id} externo sin tercero")
        cliente = p.tercero_id if not p.es_interno else parametros.cliente_id  # externo: SIEMPRE su empresa
        ahora = ahora_pg(tx)
        if tipo.fuente == "predicciones":
            return _tabla_predicciones(self._predicciones.vigentes(tx, alcance=_alcance(p)), ahora)
        datos = {
            "envios": lambda: self._hechos.envios(tx, cliente_id=cliente),
            "incidentes": lambda: self._hechos.incidentes(tx, cliente_id=cliente),
            "alertas": lambda: self._hechos.alertas_temperatura(tx, cliente_id=cliente),
        }[tipo.fuente]()
        return tipo.construir(datos, parametros, ahora, p.es_interno)

    # ------------------------------------------------------------------ generación registrada
    def generar(
        self, sesion_id: uuid.UUID, codigo: str, parametros: da.Parametros, formato: str
    ) -> ReporteDTO:
        """Genera el archivo y lo registra. Un fallo deja el reporte en ERROR (nunca un COMPLETADO sin
        archivo)."""
        tipo = _tipo(codigo)
        formato = (formato or "").upper()
        if formato not in FORMATOS:
            raise BusinessRuleError(
                f"formato {formato}", user_message="Formato de reporte inválido (PDF o CSV)."
            )
        reporte_id = uuid.uuid4()
        clave: str | None = None
        with self._aut.operacion(sesion_id, tipo.permiso) as (tx, p):  # 1) solicitud registrada
            if not _puede(p, tipo):
                raise PermissionDeniedError(f"{p.usuario_id} no puede pedir {tipo.codigo}")
            parametros = parametros.normalizado()
            if Rol.CLIENTE in p.roles:  # se registra el filtro que realmente se aplicó
                parametros = da.Parametros(
                    parametros.desde, parametros.hasta, p.tercero_id, parametros.dimension, parametros.segun
                )
            self._reportes.crear(tx, reporte_id, p.usuario_id, tipo.codigo, parametros.a_json(), formato)
            auditoria.registrar(
                tx,
                "solicitar_reporte",
                p,
                entidad="reporte",
                entidad_id=reporte_id,
                tipo=tipo.codigo,
                formato=formato,
            )
        try:
            with self._aut.operacion(sesion_id, tipo.permiso) as (tx, p):  # 2) cálculo (sesión revalidada)
                self._reportes.iniciar(tx, reporte_id)
                tabla = self._tabla(tx, p, tipo, parametros)
                generado = ahora_pg(tx)
            subtitulo = (
                f"Filtros: {parametros.rango()}. Generado el {da.celda(generado)} (hora local) por {p.email}."
            )
            clave = self._archivos.guardar(
                f"{tipo.codigo}_{generado:%Y%m%d_%H%M%S}_{reporte_id.hex[:8]}", tabla, formato, subtitulo
            )
            with self._aut.operacion(sesion_id, tipo.permiso) as (tx, _p):  # 3) recién ahora COMPLETADO
                if not self._reportes.completar(tx, reporte_id, clave):
                    raise BusinessRuleError(
                        f"reporte {reporte_id} no estaba EN_PROCESO",
                        user_message="No se pudo registrar el reporte.",
                    )
                return self._reportes.obtener(tx, reporte_id)
        except Exception as exc:
            if clave is not None:
                self._archivos.borrar(clave)  # un reporte en ERROR no deja su archivo en la carpeta
            self._marcar_error(reporte_id, exc)
            if isinstance(exc, LogisticaError):
                raise
            log.exception("No se pudo generar el reporte %s (%s)", reporte_id, tipo.codigo)
            raise BusinessRuleError(
                f"reporte {reporte_id}: {type(exc).__name__}",
                user_message="No se pudo generar el reporte. Ver logs.",
            ) from exc

    def _marcar_error(self, reporte_id: uuid.UUID, exc: Exception) -> None:
        try:
            from logistica.repositories.postgres import unit_of_work

            with unit_of_work(
                self._conn
            ) as tx:  # técnico: registra el fallo aunque la sesión ya no sea válida
                self._reportes.fallar(tx, reporte_id, type(exc).__name__)
        except Exception:  # noqa: BLE001 — si PostgreSQL cayó, queda PENDIENTE/EN_PROCESO y se ve en la lista
            log.exception("No se pudo registrar el error del reporte %s", reporte_id)

    def repetir(self, sesion_id: uuid.UUID, reporte_id: uuid.UUID) -> ReporteDTO:
        """Vuelve a generar con los MISMOS parámetros registrados (por ejemplo, si el archivo no está en este
        equipo: SUPUESTO R1)."""
        with self._aut.operacion(sesion_id, *PERMISOS_REPORTES) as (tx, p):
            r = self._visible(tx, p, reporte_id)
        return self.generar(sesion_id, r.tipo, da.Parametros.desde_json(r.parametros), r.formato)

    # ------------------------------------------------------------------ registro y apertura
    def reportes(self, sesion_id: uuid.UUID) -> list[ReporteDTO]:
        """Los propios; el administrador ve los de todos."""
        with self._aut.operacion(sesion_id, *PERMISOS_REPORTES) as (tx, p):
            return self._reportes.listar(
                tx, usuario_id=None if Rol.ADMINISTRADOR in p.roles else p.usuario_id
            )

    def abrir(self, sesion_id: uuid.UUID, reporte_id: uuid.UUID) -> Path:
        """Ruta local del archivo. Comprueba de nuevo el permiso del TIPO (no alcanza con haberlo
        generado)."""
        with self._aut.operacion(sesion_id, *PERMISOS_REPORTES) as (tx, p):
            r = self._visible(tx, p, reporte_id)
            tipo = _tipo(r.tipo)
            if tipo.permiso not in p.permisos or not _puede(p, tipo):
                raise PermissionDeniedError(f"{p.usuario_id} ya no puede abrir {r.tipo}")
            if r.estado != "COMPLETADO":
                raise BusinessRuleError(
                    f"reporte {r.estado}", user_message="El reporte no terminó de generarse."
                )
            ruta = self._archivos.ruta(r.archivo_clave)
            if ruta is None:
                raise NotFoundError(
                    f"archivo {r.archivo_clave}",
                    user_message="El archivo no está en este equipo: use «Generar de nuevo».",
                )
            auditoria.registrar(tx, "abrir_reporte", p, entidad="reporte", entidad_id=reporte_id)
            return ruta

    def _visible(self, tx: Any, p: Principal, reporte_id: uuid.UUID) -> ReporteDTO:
        r = self._reportes.obtener(tx, reporte_id)
        if r is None or (r.solicitado_por_id != p.usuario_id and Rol.ADMINISTRADOR not in p.roles):
            raise NotFoundError(f"reporte {reporte_id}", user_message="El reporte no existe.")
        return r


def _tipo(codigo: str) -> TipoReporte:
    tipo = CATALOGO.get((codigo or "").upper())
    if tipo is None:
        raise NotFoundError(f"tipo de reporte {codigo}", user_message="Tipo de reporte desconocido.")
    return tipo


def _puede(p: Principal, t: TipoReporte) -> bool:
    if t.permiso not in p.permisos:
        return False
    return p.es_interno or (t.externos and Rol.CLIENTE in p.roles) or t.fuente == "predicciones"


def _tabla_predicciones(predicciones: list[Any], ahora: datetime) -> da.Tabla:
    filas = []
    for x in sorted(
        predicciones, key=lambda x: (x.probabilidad is None, -(x.probabilidad or 0), x.envio_codigo)
    ):
        if x.estado_envio not in ("PLANIFICADO", "EN_TRANSITO"):
            continue
        ya = "Sí" if x.compromiso is not None and x.compromiso < ahora else "No"
        porcentaje = (
            (x.probabilidad * 100).quantize(Decimal("0.1")) if x.probabilidad is not None else da.SIN_DATOS
        )
        filas.append(
            (
                x.envio_codigo,
                x.estado_envio,
                x.compromiso,
                ya,
                porcentaje,
                x.riesgo_operativo,
                x.riesgo_climatico,
                x.datos_entrada.get("poblacion", ""),
                x.datos_entrada.get("casos", 0),
                x.fecha_evaluacion,
                x.explicacion,
            )
        )
    return da.Tabla(
        "Predicción de atrasos",
        (
            "Envío",
            "Estado",
            "Compromiso",
            "Ya demorado",
            "Prob. demora (%)",
            "Riesgo operativo",
            "Riesgo climático",
            "Población",
            "Casos",
            "Evaluado",
            "Explicación",
        ),
        filas,
        (
            "Método FRECUENCIA_HISTORICA 1.0: frecuencia de atrasos de entregas comparables de los últimos "
            "180 días (misma ruta, mínimo 10; si no, todas las rutas, mínimo 20).",
            "No es un modelo de aprendizaje automático validado ni una probabilidad individual calibrada.",
            "«Ya demorado»: el compromiso ya pasó; es un hecho actual, no una predicción.",
        ),
        grafico=4,
    )
