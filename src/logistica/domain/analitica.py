"""Métricas, reportes y predicción de atrasos (AGENTS.md §10.5, §10.6, §12). Funciones puras, sin I/O.

Los repositorios traen HECHOS (una fila por envío, una por tramo, una por alerta) y este módulo los agrega.
Así cada número de un reporte se puede recalcular a mano a partir de los datos y probar sin base.

Reglas de §10.6:
- Duración de entrega = entrega real − salida real del PRIMER tramo (solo entregados, fechas coherentes).
- A tiempo = entrega real no posterior al compromiso, sobre los entregados CON compromiso.
- Atraso = max(0, entrega real − compromiso).
- Volumen por cliente = suma de `volumen_m3` de entregados, una vez por envío (no por tramo).
- Costo por transporte = tramos FINALIZADOS con costo, por medio y moneda (nunca se mezclan monedas).
- Sin casos válidos: SIN_DATOS y casos = 0 (no es lo mismo que una duración cero).
SUPUESTOS R1..R6: ver AGENTS.md §16 ("Supuestos de la fase 7").
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from logistica.core.errors import ValidationError

SIN_DATOS = "SIN_DATOS"
_UNA_HORA = Decimal(3600)


# --------------------------------------------------------------------------- hechos
@dataclass(frozen=True)
class TramoHecho:
    envio_id: uuid.UUID
    orden: int
    origen_id: uuid.UUID
    origen: str
    destino_id: uuid.UUID
    destino: str
    medio: str
    estado: str
    salida_real: datetime | None
    llegada_real: datetime | None
    costo: Decimal | None
    moneda: str | None


@dataclass(frozen=True)
class EnvioHecho:
    id: uuid.UUID
    codigo: str
    cliente_id: uuid.UUID
    cliente: str
    estado: str
    creado_en: datetime
    entrega_real: datetime | None
    compromiso: datetime | None
    volumen_m3: Decimal
    peso_kg: Decimal
    pais_origen: str
    pais_destino: str
    incidentes: int  # todos, cada uno una vez
    incidentes_abiertos: int
    tramos: tuple[TramoHecho, ...] = ()

    @property
    def tramos_vigentes(self) -> list[TramoHecho]:
        return sorted((t for t in self.tramos if t.estado != "CANCELADO"), key=lambda t: t.orden)

    @property
    def salida_real(self) -> datetime | None:
        vigentes = self.tramos_vigentes
        return vigentes[0].salida_real if vigentes else None

    @property
    def ruta_clave(self) -> str:
        """Secuencia ordenada de ubicaciones y medios (operador y vehículo no cuentan, §10.5)."""
        partes: list[str] = []
        for t in self.tramos_vigentes:
            if not partes:
                partes.append(str(t.origen_id))
            partes += [t.medio, str(t.destino_id)]
        return "|".join(partes)

    @property
    def ruta_nombre(self) -> str:
        partes: list[str] = []
        for t in self.tramos_vigentes:
            if not partes:
                partes.append(t.origen)
            partes.append(f"({t.medio.lower()}) {t.destino}")
        return " → ".join(partes) or "(sin tramos)"


def duracion_horas(e: EnvioHecho) -> Decimal | None:
    """Duración EXACTA de la entrega en horas (se redondea solo al mostrar), o None si no es un caso
    válido."""
    if (
        e.estado != "ENTREGADO"
        or e.entrega_real is None
        or e.salida_real is None
        or e.entrega_real < e.salida_real
    ):
        return None
    return _exactas(e.entrega_real - e.salida_real)


def a_tiempo(e: EnvioHecho) -> bool | None:
    """None = no cuenta (no entregado o sin compromiso válido)."""
    if e.estado != "ENTREGADO" or e.entrega_real is None or e.compromiso is None:
        return None
    return e.entrega_real <= e.compromiso


def atraso_horas(e: EnvioHecho) -> Decimal | None:
    if a_tiempo(e) is None:
        return None
    return _exactas(max(timedelta(0), e.entrega_real - e.compromiso))


def _exactas(td: timedelta) -> Decimal:
    """Horas sin redondear: los promedios, mínimos y máximos se calculan con el valor exacto."""
    return Decimal(str(td.total_seconds())) / _UNA_HORA


def redondear(valor: Decimal | None) -> Decimal | None:
    return None if valor is None else valor.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def horas(td: timedelta) -> Decimal:
    return redondear(_exactas(td))


def dia_utc(fecha: datetime) -> date:
    """Los días de los filtros y los meses de los reportes son días UTC (SUPUESTO R2): no dependen de la zona
    de la sesión de PostgreSQL ni del equipo."""
    return fecha.astimezone(timezone.utc).date()


def promedio(valores: list[Decimal], decimales: str = "0.1") -> Decimal | str:
    if not valores:
        return SIN_DATOS
    return (sum(valores, Decimal(0)) / len(valores)).quantize(Decimal(decimales), rounding=ROUND_HALF_UP)


def porcentaje(parte: int, total: int) -> Decimal | str:
    if total == 0:
        return SIN_DATOS
    return (Decimal(100) * parte / total).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- tabla de resultado
def celda(valor: Any) -> str:
    """Texto para mostrar o exportar. Las fechas se guardan en UTC y se muestran en hora local (AGENTS.md
    §18)."""
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.astimezone().strftime("%d/%m/%Y %H:%M")
    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, Decimal):
        return f"{valor:f}"
    return str(valor)


def celda_csv(valor: Any) -> str:
    """Como `celda`, pero un TEXTO que empieza con = + - @ se antepone con ' para que Excel no lo ejecute como
    fórmula (los nombres y descripciones los cargan usuarios)."""
    texto = celda(valor)
    if isinstance(valor, str) and texto[:1] in ("=", "+", "-", "@"):
        return "'" + texto
    return texto


@dataclass(frozen=True)
class Tabla:
    """Resultado de un reporte: lo que se ve en pantalla y lo que se exporta (CSV/PDF)."""

    titulo: str
    columnas: tuple[str, ...]
    filas: list[tuple[Any, ...]]
    notas: tuple[str, ...] = ()
    grafico: int | None = None  # índice de la columna numérica que se grafica (None = sin gráfico)

    @property
    def vacia(self) -> bool:
        return not self.filas


@dataclass(frozen=True)
class Parametros:
    """Filtros validados de un reporte (se guardan tal cual en `reporte.parametros`)."""

    desde: date | None = None
    hasta: date | None = None  # inclusive (se convierte a [desde, hasta + 1 día))
    cliente_id: uuid.UUID | None = None
    dimension: str = "PAIS"  # para el promedio de incidentes: PAIS, ESTADO o CLIENTE
    segun: str = "DESTINO"  # para envíos por país: ORIGEN o DESTINO

    def normalizado(self) -> Parametros:
        if self.desde and self.hasta and self.hasta < self.desde:
            raise ValidationError(
                "rango invertido", user_message="La fecha 'hasta' no puede ser anterior a 'desde'."
            )
        dimension, segun = (self.dimension or "PAIS").upper(), (self.segun or "DESTINO").upper()
        if dimension not in ("PAIS", "ESTADO", "CLIENTE") or segun not in ("ORIGEN", "DESTINO"):
            raise ValidationError("parámetro inválido", user_message="Parámetro de reporte inválido.")
        return Parametros(self.desde, self.hasta, self.cliente_id, dimension, segun)

    def a_json(self) -> dict[str, Any]:
        return {
            "desde": self.desde.isoformat() if self.desde else None,
            "hasta": self.hasta.isoformat() if self.hasta else None,
            "cliente_id": str(self.cliente_id) if self.cliente_id else None,
            "dimension": self.dimension,
            "segun": self.segun,
        }

    @staticmethod
    def desde_json(d: dict[str, Any]) -> Parametros:
        return Parametros(
            date.fromisoformat(d["desde"]) if d.get("desde") else None,
            date.fromisoformat(d["hasta"]) if d.get("hasta") else None,
            uuid.UUID(d["cliente_id"]) if d.get("cliente_id") else None,
            d.get("dimension") or "PAIS",
            d.get("segun") or "DESTINO",
        )

    def rango(self) -> str:
        return f"{_f(self.desde)} a {_f(self.hasta)}" if self.desde or self.hasta else "todo el período"


def _f(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else "…"


def _en_rango(fecha: datetime | None, p: Parametros) -> bool:
    if fecha is None:
        return False
    dia = dia_utc(fecha) if isinstance(fecha, datetime) else fecha
    return (p.desde is None or dia >= p.desde) and (p.hasta is None or dia <= p.hasta)


def _cohorte(envios: list[EnvioHecho], p: Parametros) -> list[EnvioHecho]:
    """SUPUESTO R2: los reportes de cantidad e incidentes toman la cohorte por fecha de CREACIÓN del envío."""
    return [e for e in envios if (p.desde is None and p.hasta is None) or _en_rango(e.creado_en, p)]


def _entregados(envios: list[EnvioHecho], p: Parametros) -> list[EnvioHecho]:
    """Los de tiempos y volumen toman los ENTREGADOS por fecha de entrega real."""
    return [
        e
        for e in envios
        if e.estado == "ENTREGADO" and ((p.desde is None and p.hasta is None) or _en_rango(e.entrega_real, p))
    ]


# --------------------------------------------------------------------------- reportes operativos
def envios_por_pais(envios: list[EnvioHecho], p: Parametros) -> Tabla:
    cuenta: dict[str, int] = defaultdict(int)
    for e in _cohorte(envios, p):
        cuenta[e.pais_destino if p.segun == "DESTINO" else e.pais_origen] += 1
    filas = sorted(cuenta.items(), key=lambda x: (-x[1], x[0]))
    return Tabla(
        f"Envíos por país de {p.segun.lower()}",
        ("País", "Envíos"),
        filas,
        (f"Envíos creados: {p.rango()}.",),
        grafico=1,
    )


def envios_por_estado(envios: list[EnvioHecho], p: Parametros) -> Tabla:
    orden = ("BORRADOR", "PLANIFICADO", "EN_TRANSITO", "ENTREGADO", "CANCELADO")
    cuenta: dict[str, int] = defaultdict(int)
    for e in _cohorte(envios, p):
        cuenta[e.estado] += 1
    filas = [(estado, cuenta[estado]) for estado in orden if cuenta[estado]]
    return Tabla(
        "Envíos por estado", ("Estado", "Envíos"), filas, (f"Envíos creados: {p.rango()}.",), grafico=1
    )


def envios_por_cliente(envios: list[EnvioHecho], p: Parametros) -> Tabla:
    grupos: dict[tuple[uuid.UUID, str], list[EnvioHecho]] = defaultdict(list)
    for e in _cohorte(envios, p):
        grupos[(e.cliente_id, e.cliente)].append(e)  # por id: la razón social no es única
    filas = sorted(
        (
            (
                c,
                len(g),
                sum(1 for e in g if e.estado == "ENTREGADO"),
                sum(1 for e in g if e.estado == "EN_TRANSITO"),
            )
            for (_, c), g in grupos.items()
        ),
        key=lambda x: (-x[1], x[0]),
    )
    return Tabla(
        "Envíos por cliente",
        ("Cliente", "Envíos", "Entregados", "En tránsito"),
        filas,
        (f"Envíos creados: {p.rango()}.",),
        grafico=1,
    )


def envios_demorados(envios: list[EnvioHecho], p: Parametros, ahora: datetime) -> Tabla:
    """Dos grupos: entregados fuera del compromiso (en el rango) y en curso que YA pasaron su compromiso
    (hecho actual, sin importar el rango). Un envío sin compromiso no se puede calificar de demorado."""
    filas: list[tuple[Any, ...]] = []
    for e in sorted(_entregados(envios, p), key=lambda e: e.entrega_real):
        if a_tiempo(e) is False:
            filas.append(
                (
                    e.codigo,
                    e.cliente,
                    "Entregado con atraso",
                    e.compromiso,
                    e.entrega_real,
                    redondear(atraso_horas(e)),
                )
            )
    for e in sorted(envios, key=lambda e: e.compromiso or ahora):
        if e.estado in ("PLANIFICADO", "EN_TRANSITO") and e.compromiso is not None and e.compromiso < ahora:
            filas.append(
                (
                    e.codigo,
                    e.cliente,
                    "En curso, compromiso vencido",
                    e.compromiso,
                    None,
                    horas(ahora - e.compromiso),
                )
            )
    return Tabla(
        "Envíos demorados",
        ("Envío", "Cliente", "Situación", "Compromiso", "Entrega real", "Atraso (h)"),
        filas,
        (
            f"Entregados: {p.rango()}. En curso: al momento de generar.",
            "El atraso es max(0, entrega real − compromiso).",
        ),
    )


@dataclass(frozen=True)
class IncidenteHecho:
    id: uuid.UUID
    envio_codigo: str
    cliente_id: uuid.UUID
    cliente: str
    tipo: str
    severidad: str
    estado: str
    ocurrido_en: datetime
    responsable: str | None


def incidentes_abiertos(
    incidentes: list[IncidenteHecho], ahora: datetime, *, mostrar_responsable: bool = True
) -> Tabla:
    """`mostrar_responsable=False` para externos: el email del responsable interno es un dato personal que el
    cliente no necesita (AGENTS.md §14)."""
    orden = {"ALTA": 0, "MEDIA": 1, "BAJA": 2}
    abiertos = sorted(
        (i for i in incidentes if i.estado != "RESUELTO"),
        key=lambda i: (orden.get(i.severidad, 9), i.ocurrido_en),
    )
    columnas = ("Envío", "Cliente", "Tipo", "Severidad", "Estado", "Ocurrido", "Abierto hace (h)")
    filas = [
        (
            i.envio_codigo,
            i.cliente,
            i.tipo,
            i.severidad,
            i.estado,
            i.ocurrido_en,
            horas(ahora - i.ocurrido_en),
        )
        + ((i.responsable or "(sin asignar)",) if mostrar_responsable else ())
        for i in abiertos
    ]
    return Tabla(
        "Incidentes abiertos",
        columnas + (("Responsable",) if mostrar_responsable else ()),
        filas,
        ("Situación al momento de generar.",),
    )


# --------------------------------------------------------------------------- reportes analíticos
def tiempos_por_cliente(envios: list[EnvioHecho], p: Parametros) -> Tabla:
    grupos: dict[tuple[uuid.UUID, str], list[Decimal]] = defaultdict(list)
    for e in _entregados(envios, p):
        d = duracion_horas(e)
        if d is not None:
            grupos[(e.cliente_id, e.cliente)].append(d)
    filas = sorted(
        ((c, len(v), redondear(min(v)), redondear(max(v)), promedio(v)) for (_, c), v in grupos.items()),
        key=lambda x: x[0],
    )
    return Tabla(
        "Tiempos de envío por cliente",
        ("Cliente", "Casos", "Mínimo (h)", "Máximo (h)", "Promedio (h)"),
        filas,
        (f"Entregados: {p.rango()}.", "Duración = entrega real − salida real del primer tramo."),
        grafico=4,
    )


def tiempos_promedio_entrega(envios: list[EnvioHecho], p: Parametros) -> Tabla:
    entregados = _entregados(envios, p)
    duraciones = [d for d in map(duracion_horas, entregados) if d is not None]
    calificados = [x for x in map(a_tiempo, entregados) if x is not None]
    atrasos = [a for a in map(atraso_horas, entregados) if a is not None]
    filas = [
        ("Entregas consideradas", len(duraciones)),
        ("Duración promedio (h)", promedio(duraciones)),
        ("Entregas con compromiso", len(calificados)),
        ("A tiempo (%)", porcentaje(sum(calificados), len(calificados))),
        ("Atraso promedio (h)", promedio(atrasos)),
    ]
    return Tabla(
        "Tiempos promedio de entrega",
        ("Indicador", "Valor"),
        filas,
        (f"Entregados: {p.rango()}.", "SIN_DATOS = no hay casos válidos (no es un tiempo cero)."),
    )


def incidentes_promedio(envios: list[EnvioHecho], p: Parametros) -> Tabla:
    grupos: dict[tuple[Any, str], list[EnvioHecho]] = defaultdict(list)
    for e in _cohorte(envios, p):
        clave = {
            "PAIS": (None, e.pais_destino),
            "ESTADO": (None, e.estado),
            "CLIENTE": (e.cliente_id, e.cliente),
        }[p.dimension]
        grupos[clave].append(e)
    filas = sorted(
        (
            (k, len(g), sum(e.incidentes for e in g), promedio([Decimal(e.incidentes) for e in g], "0.01"))
            for (_, k), g in grupos.items()
        ),
        key=lambda x: (-x[3], -x[2], x[0]),
    )  # por el promedio
    nombre = {"PAIS": "país de destino", "ESTADO": "estado", "CLIENTE": "cliente"}[p.dimension]
    return Tabla(
        f"Promedio de incidentes por {nombre}",
        (nombre.capitalize(), "Envíos", "Incidentes", "Incidentes por envío"),
        filas,
        (f"Envíos creados: {p.rango()}. Cada incidente cuenta una vez.",),
        grafico=3,
    )


def ranking_rutas_incidentes(envios: list[EnvioHecho], p: Parametros, limite: int = 10) -> Tabla:
    grupos: dict[str, list[EnvioHecho]] = defaultdict(list)
    for e in _cohorte(envios, p):
        if e.ruta_clave:
            grupos[e.ruta_clave].append(e)
    filas = [
        (
            g[0].ruta_nombre,
            sum(e.incidentes for e in g),
            len(g),
            promedio([Decimal(e.incidentes) for e in g], "0.01"),
        )
        for g in grupos.values()
    ]
    filas.sort(key=lambda x: (-x[1], -x[3] if isinstance(x[3], Decimal) else 0, x[0]))
    filas = [(i, *f) for i, f in enumerate(filas[:limite], start=1)]
    return Tabla(
        "Ranking de rutas con más incidentes",
        ("Puesto", "Ruta", "Incidentes", "Envíos", "Incidentes por envío"),
        filas,
        (f"Envíos creados: {p.rango()}. Ruta = ubicaciones y medios en orden.",),
        grafico=2,
    )


def ranking_clientes_volumen(envios: list[EnvioHecho], p: Parametros, limite: int = 10) -> Tabla:
    grupos: dict[tuple[uuid.UUID, str], list[EnvioHecho]] = defaultdict(list)
    for e in _entregados(envios, p):
        grupos[(e.cliente_id, e.cliente)].append(e)
    filas = [
        (c, sum((e.volumen_m3 for e in g), Decimal(0)), sum((e.peso_kg for e in g), Decimal(0)), len(g))
        for (_, c), g in grupos.items()
    ]
    filas.sort(key=lambda x: (-x[1], x[0]))
    filas = [(i, *f) for i, f in enumerate(filas[:limite], start=1)]
    return Tabla(
        "Ranking de clientes por volumen transportado",
        ("Puesto", "Cliente", "Volumen (m³)", "Peso (kg)", "Envíos"),
        filas,
        (f"Entregados: {p.rango()}. Volumen del envío, una vez por envío (no por tramo).",),
        grafico=2,
    )


def costos_por_transporte(envios: list[EnvioHecho], p: Parametros) -> Tabla:
    grupos: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for e in envios:
        for t in e.tramos:
            if (
                t.estado == "FINALIZADO"
                and t.costo is not None
                and ((p.desde is None and p.hasta is None) or _en_rango(t.llegada_real, p))
            ):
                grupos[(t.medio, t.moneda)].append(t.costo)
    filas = sorted(
        (
            (medio, moneda, len(v), sum(v, Decimal(0)), promedio(v, "0.01"))
            for (medio, moneda), v in grupos.items()
        ),
        key=lambda x: (x[0], x[1]),
    )
    return Tabla(
        "Costos por tipo de transporte",
        ("Medio", "Moneda", "Tramos", "Costo total", "Costo promedio"),
        filas,
        (
            f"Tramos finalizados por llegada real: {p.rango()}.",
            "Cada moneda por separado: nunca se suman monedas distintas.",
        ),
        grafico=3,
    )


def desempeno_historico(envios: list[EnvioHecho], p: Parametros) -> Tabla:
    """Análisis histórico de desempeño: los indicadores de §10.6 mes a mes (por mes de entrega real, UTC)."""
    meses: dict[str, list[EnvioHecho]] = defaultdict(list)
    for e in _entregados(envios, p):
        meses[e.entrega_real.astimezone(timezone.utc).strftime("%Y-%m")].append(e)
    filas = []
    for mes in sorted(meses):
        g = meses[mes]
        duraciones = [d for d in map(duracion_horas, g) if d is not None]
        calificados = [x for x in map(a_tiempo, g) if x is not None]
        filas.append(
            (
                mes,
                len(g),
                promedio(duraciones),
                porcentaje(sum(calificados), len(calificados)),
                promedio([a for a in map(atraso_horas, g) if a is not None]),
                sum((e.volumen_m3 for e in g), Decimal(0)),
            )
        )
    return Tabla(
        "Desempeño histórico",
        ("Mes", "Entregas", "Duración prom. (h)", "A tiempo (%)", "Atraso prom. (h)", "Volumen (m³)"),
        filas,
        (f"Entregados: {p.rango()}.",),
        grafico=3,
    )


@dataclass(frozen=True)
class AlertaHecho:
    envio_codigo: str
    cliente_id: uuid.UUID
    sensor: str | None
    severidad: str
    estado: str
    primera_deteccion: datetime
    ultima_deteccion: datetime
    normalizada_en: datetime | None
    incidente: bool
    cerrada_en: datetime | None = None


def anomalias_temperatura(alertas: list[AlertaHecho], p: Parametros, ahora: datetime) -> Tabla:
    """Detección de anomalías de temperatura (§10.5): método base = reglas de §10.4 (fuera de rango
    sostenido), que ya registran cada episodio como una alerta TEMPERATURA. El sistema detecta EXPOSICIÓN, no
    afirma daño."""
    filas = []
    for a in sorted(alertas, key=lambda a: a.primera_deteccion):
        if not ((p.desde is None and p.hasta is None) or _en_rango(a.primera_deteccion, p)):
            continue
        # Fin del episodio: normalización automática, o cierre manual, o sigue abierta (hasta ahora).
        fin = a.normalizada_en or (a.cerrada_en or a.ultima_deteccion if a.estado == "CERRADA" else ahora)
        filas.append(
            (
                a.envio_codigo,
                a.sensor or "",
                a.primera_deteccion,
                a.normalizada_en,
                horas(fin - a.primera_deteccion),
                a.severidad,
                "Sí" if a.incidente else "No",
                a.estado,
            )
        )
    return Tabla(
        "Anomalías de temperatura",
        ("Envío", "Sensor", "Inicio", "Normalizada", "Duración (h)", "Severidad máx.", "Incidente", "Estado"),
        filas,
        (
            f"Episodios detectados: {p.rango()}.",
            "Método: temperatura fuera del rango del envío sostenida 120 s (media) y 300 s (alta, "
            "incidente).",
            "Indica exposición de la carga; no afirma que esté dañada.",
        ),
    )


# --------------------------------------------------------------------------- predicción (§10.5)
@dataclass(frozen=True)
class MetodoPrediccion:
    """Método FRECUENCIA_HISTORICA 1.0. Todos los valores son parámetros (no constantes escondidas)."""

    codigo: str = "FRECUENCIA_HISTORICA"
    version: str = "1.0"
    dias: int = 180
    minimo_ruta: int = 10
    minimo_global: int = 20
    umbral_medio: Decimal = Decimal("0.20")
    umbral_alto: Decimal = Decimal("0.50")


@dataclass(frozen=True)
class Prediccion:
    probabilidad: Decimal | None  # NUMERIC(6,5); None = muestra insuficiente
    riesgo: str  # BAJO, MEDIO, ALTO o SIN_DATOS
    poblacion: str  # MISMA_RUTA, TODAS_LAS_RUTAS o INSUFICIENTE
    casos: int
    demorados: int
    datos_entrada: dict[str, Any] = field(default_factory=dict)
    explicacion: str = ""


def comparables(historial: list[EnvioHecho], corte: datetime, m: MetodoPrediccion) -> list[EnvioHecho]:
    """Entregas con resultado CONOCIDO al calcular: entregadas en [corte − días, corte), con compromiso y con
    fechas coherentes. Se excluyen cancelados, en curso y sin compromiso (nunca se inventa un compromiso)."""
    inicio = corte - timedelta(days=m.dias)
    return [
        e
        for e in historial
        if e.estado == "ENTREGADO"
        and e.entrega_real is not None
        and inicio <= e.entrega_real < corte
        and e.compromiso is not None
        and e.salida_real is not None
        and e.entrega_real >= e.salida_real
    ]


def clasificar(probabilidad: Decimal | None, m: MetodoPrediccion) -> str:
    if probabilidad is None:
        return SIN_DATOS
    if probabilidad >= m.umbral_alto:
        return "ALTO"
    return "MEDIO" if probabilidad >= m.umbral_medio else "BAJO"


def predecir(
    ruta_clave: str, historial: list[EnvioHecho], corte: datetime, m: MetodoPrediccion, ruta_nombre: str = ""
) -> Prediccion:
    """Probabilidad = entregas fuera del compromiso / entregas comparables. Población preferida: la MISMA ruta
    (mínimo `minimo_ruta`); si no alcanza, TODAS las rutas (mínimo `minimo_global`); si tampoco, SIN_DATOS.

    Es una frecuencia histórica, NO un modelo de aprendizaje automático validado ni una probabilidad
    individual calibrada: dice "con qué frecuencia se atrasaron envíos parecidos"."""
    base = comparables(historial, corte, m)
    misma = [e for e in base if e.ruta_clave == ruta_clave]
    if len(misma) >= m.minimo_ruta:
        poblacion, muestra = "MISMA_RUTA", misma
    elif len(base) >= m.minimo_global:
        poblacion, muestra = "TODAS_LAS_RUTAS", base
    else:
        poblacion, muestra = "INSUFICIENTE", []
    demorados = sum(1 for e in muestra if a_tiempo(e) is False)
    probabilidad = (
        (Decimal(demorados) / len(muestra)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
        if muestra
        else None
    )
    riesgo = clasificar(probabilidad, m)
    datos = {
        "ruta": ruta_clave,
        "ruta_nombre": ruta_nombre,
        "periodo": {"desde": (corte - timedelta(days=m.dias)).isoformat(), "hasta": corte.isoformat()},
        "poblacion": poblacion,
        "casos": len(muestra),
        "demorados": demorados,
        "comparables_misma_ruta": len(misma),
        "comparables_todas": len(base),
        "minimos": {"ruta": m.minimo_ruta, "global": m.minimo_global},
        "umbrales": {"medio": str(m.umbral_medio), "alto": str(m.umbral_alto)},
        "origen_datos": "REAL",
    }  # SUPUESTO R5: no hay escenarios simulados (el clima queda SIN_DATOS)
    if probabilidad is None:
        explicacion = (
            f"Muestra insuficiente: {len(misma)} entregas de la misma ruta (mínimo {m.minimo_ruta}) y "
            f"{len(base)} en total (mínimo {m.minimo_global}) en los últimos {m.dias} días."
        )
    else:
        donde = "de la misma ruta" if poblacion == "MISMA_RUTA" else "de todas las rutas"
        explicacion = (
            f"{demorados} de {len(muestra)} entregas {donde} en los últimos {m.dias} días llegaron "
            f"después del compromiso ({(probabilidad * 100).quantize(Decimal('0.1'))} %). "
            "Frecuencia histórica, no una probabilidad individual calibrada."
        )
    return Prediccion(probabilidad, riesgo, poblacion, len(muestra), demorados, datos, explicacion)
