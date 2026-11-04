"""Reglas de monitoreo IoT (AGENTS.md §10.4), puras: sin I/O y sin reloj propio.

Todo instante llega como dato (`fecha_medicion`, `fecha_recepcion`, `ahora`): está PROHIBIDO usar
`datetime.now()` o `time.sleep` acá. Así cada regla se prueba con un reloj controlado.

Piezas:
- `Configuracion`: parámetros de la versión de reglas (`version_reglas` + `reglas_evaluacion` de
  condicion_monitoreo); los valores por defecto son los de §10.4.
- `clasificar`: decide si una lectura actualiza el estado actual o queda solo como histórico.
- Una Strategy por condición (`ReglaRango` para temperatura/humedad/vibración, `ReglaBateria`) y
  `evaluar_comunicacion` para la falta de comunicación, que se CALCULA (último contacto + umbral).
- `evaluar_lectura`: aplica las reglas y devuelve el estado nuevo + acciones; quien llama (el servicio) las
  ejecuta en una transacción. El sistema detecta EXPOSICIÓN, no afirma daño de la carga.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from logistica.core.errors import ValidationError

__all__ = ["ValidationError"]  # reexportada: los llamadores no necesitan importar core.errors

MEDIA, ALTA, BAJA = "MEDIA", "ALTA", "BAJA"
TEMPERATURA, HUMEDAD, VIBRACION, BATERIA = "TEMPERATURA", "HUMEDAD", "VIBRACION", "BATERIA"
SIN_COMUNICACION = "SIN_COMUNICACION"
INCIDENTE_TEMPERATURA = "DESVIACION_TEMPERATURA"


# --------------------------------------------------------------------------- configuración versionada
@dataclass(frozen=True)
class Configuracion:
    """Parámetros de una versión de reglas. Ninguno es una constante suelta en el código de las reglas."""

    version: str
    apertura_s: int = 120  # anomalía sostenida para abrir (temperatura/humedad)
    escalamiento_s: int = 300  # anomalía sostenida para pasar a ALTA
    normalizacion_s: int = 120  # tiempo en rango para cerrar la alerta
    continuidad_max_s: int = 120  # separación máxima entre lecturas de una misma secuencia
    tardia_s: int = 120  # recepción - medición: más que esto, la lectura no actualiza el estado actual
    reloj_adelantado_s: int = 120  # medición - recepción: más que esto, se rechaza para revisión
    histeresis_bateria: int = 5  # puntos sobre el mínimo para normalizar la batería
    sin_comunicacion_alta_s: int = 600  # sin contacto: a partir de acá la alerta es ALTA

    @classmethod
    def desde(cls, *, version: str, reglas: dict[str, Any]) -> Configuracion:
        conocidos = {f.name for f in fields(cls)} - {"version"}
        desconocidos = set(reglas) - conocidos
        if desconocidos:
            raise ValidationError(f"parámetros de reglas desconocidos: {sorted(desconocidos)}",
                                  user_message="La configuración de reglas tiene parámetros desconocidos.")
        valores = {}
        for clave, valor in reglas.items():
            if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
                raise ValidationError(f"{clave} inválido", user_message=f"El parámetro {clave} debe ser un entero >= 0.")
            valores[clave] = valor
        return cls(version=version, **valores)


# --------------------------------------------------------------------------- datos
@dataclass(frozen=True)
class Contexto:
    """Dónde estaba el sensor AL MEDIR (asignación vigente en ese instante, no al recibir)."""

    envio_id: uuid.UUID
    contenedor_id: uuid.UUID
    asignacion_sensor_id: uuid.UUID
    tramo_id: uuid.UUID | None

    def a_json(self) -> dict[str, Any]:
        return {k: (str(v) if v is not None else None) for k, v in self.__dict__.items()}

    @classmethod
    def desde_json(cls, d: dict[str, Any] | None) -> Contexto | None:
        if not d:
            return None
        return cls(**{k: (uuid.UUID(v) if v else None) for k, v in d.items()})


@dataclass(frozen=True)
class Condicion:
    """Condición de monitoreo del envío (tabla condicion_monitoreo). None = esa variable no se controla."""

    temperatura_min_c: Decimal | None
    temperatura_max_c: Decimal | None
    humedad_min_pct: Decimal | None
    humedad_max_pct: Decimal | None
    vibracion_max_g: Decimal | None
    bateria_min_pct: Decimal | None
    tiempo_max_sin_lecturas_seg: int
    version_reglas: str
    reglas: dict[str, Any] = field(default_factory=dict)

    @property
    def configuracion(self) -> Configuracion:
        return Configuracion.desde(version=self.version_reglas, reglas=self.reglas)


@dataclass(frozen=True)
class Lectura:
    id: uuid.UUID
    sensor_id: uuid.UUID
    fecha_medicion: datetime
    fecha_recepcion: datetime
    contexto: Contexto | None
    mediciones: dict[str, Decimal]  # temperatura_c, humedad_pct, vibracion_g, bateria_pct (las ausentes no están)


@dataclass(frozen=True)
class Secuencia:
    """Seguimiento de una variable: desde cuándo está anómala o normal, y su última lectura."""

    anomala_desde: datetime | None = None
    normal_desde: datetime | None = None
    ultima: datetime | None = None


@dataclass(frozen=True)
class EstadoSensor:
    """Lo que se guarda en sensor_estado_monitoreo.evaluacion_condiciones (JSON) entre lecturas."""

    contexto: Contexto | None
    ultima_medicion_en: datetime | None
    ultima_lectura_id: uuid.UUID | None
    secuencias: dict[str, Secuencia]

    @classmethod
    def inicial(cls) -> EstadoSensor:
        return cls(None, None, None, {})

    def a_json(self) -> dict[str, Any]:
        return {
            "contexto": self.contexto.a_json() if self.contexto else None,
            "ultima_medicion_en": _iso(self.ultima_medicion_en),
            "ultima_lectura_id": str(self.ultima_lectura_id) if self.ultima_lectura_id else None,
            "secuencias": {k: {c: _iso(getattr(v, c)) for c in ("anomala_desde", "normal_desde", "ultima")}
                           for k, v in self.secuencias.items()},
        }

    @classmethod
    def desde_json(cls, d: dict[str, Any] | None) -> EstadoSensor:
        if not d:
            return cls.inicial()
        return cls(
            contexto=Contexto.desde_json(d.get("contexto")),
            ultima_medicion_en=_fecha(d.get("ultima_medicion_en")),
            ultima_lectura_id=uuid.UUID(d["ultima_lectura_id"]) if d.get("ultima_lectura_id") else None,
            secuencias={k: Secuencia(*(_fecha(v.get(c)) for c in ("anomala_desde", "normal_desde", "ultima")))
                        for k, v in (d.get("secuencias") or {}).items()},
        )


@dataclass(frozen=True)
class AlertaActiva:
    """Alerta ABIERTA o RECONOCIDA de un tipo, para el contexto de la lectura (la sabe PostgreSQL)."""

    tipo: str
    severidad: str
    incidente: bool  # ya tiene incidente automático asociado


# --------------------------------------------------------------------------- acciones
@dataclass(frozen=True)
class AbrirAlerta:
    tipo: str
    severidad: str
    detectada_en: datetime
    valor: Decimal | None = None


@dataclass(frozen=True)
class ActualizarAlerta:
    """Detección repetida del mismo problema: actualiza la alerta activa, no crea otra (§10.2)."""

    tipo: str
    detectada_en: datetime
    valor: Decimal | None = None


@dataclass(frozen=True)
class EscalarAlerta:
    tipo: str
    severidad: str
    detectada_en: datetime


@dataclass(frozen=True)
class CerrarAlerta:
    """Normalización: cierra la alerta (cierre AUTOMÁTICO). NO resuelve el incidente."""

    tipo: str
    normalizada_en: datetime


@dataclass(frozen=True)
class AbrirIncidente:
    tipo: str
    tipo_alerta: str
    severidad: str
    descripcion: str
    ocurrido_en: datetime


@dataclass(frozen=True)
class ResolverIncidente:
    """Existe para dejar explícito que el EVALUADOR nunca la emite: resolver es una decisión humana."""

    incidente_id: uuid.UUID


Accion = AbrirAlerta | ActualizarAlerta | EscalarAlerta | CerrarAlerta | AbrirIncidente


class Clasificacion(StrEnum):
    EVALUAR = "EVALUAR"  # actualiza el estado actual
    TARDIA = "TARDIA"  # llegó tarde: histórico válido, no toca el estado actual
    ANTERIOR = "ANTERIOR"  # más vieja que la última evaluada: no hace retroceder el estado
    RELOJ_ADELANTADO = "RELOJ_ADELANTADO"  # medición "del futuro": se rechaza para revisión
    SIN_CONTEXTO = "SIN_CONTEXTO"  # el sensor no estaba en un envío al medir: queda para revisión


@dataclass(frozen=True)
class Resultado:
    clasificacion: Clasificacion
    estado: EstadoSensor
    acciones: list[Accion]

    @property
    def actualiza_estado(self) -> bool:
        return self.clasificacion == Clasificacion.EVALUAR


# --------------------------------------------------------------------------- clasificación
def clasificar(lectura: Lectura, estado: EstadoSensor, condicion: Condicion) -> Clasificacion:
    cfg = condicion.configuracion
    adelanto = (lectura.fecha_medicion - lectura.fecha_recepcion).total_seconds()
    if adelanto > cfg.reloj_adelantado_s:
        return Clasificacion.RELOJ_ADELANTADO
    if lectura.contexto is None:
        return Clasificacion.SIN_CONTEXTO  # nunca se inventa un contexto
    if (lectura.fecha_recepcion - lectura.fecha_medicion).total_seconds() > cfg.tardia_s:
        return Clasificacion.TARDIA
    if estado.ultima_medicion_en is not None and lectura.fecha_medicion <= estado.ultima_medicion_en:
        return Clasificacion.ANTERIOR
    return Clasificacion.EVALUAR


# --------------------------------------------------------------------------- strategies
@dataclass(frozen=True)
class ReglaRango:
    """Variable fuera de [mínimo, máximo] sostenida en el tiempo (temperatura, humedad, vibración).

    - Abre al cumplir `apertura` segundos fuera de rango (0 = con una sola lectura).
    - Si `escala`, pasa a ALTA al cumplir `escalamiento_s`; con `incidente`, ahí pide el incidente automático.
    - Cierra (normaliza) tras `normalizacion_s` en rango con lecturas continuas.
    - Un hueco mayor a `continuidad_max_s` reinicia la secuencia: sin lecturas no se presume continuidad.
    """

    tipo: str
    variable: str
    minimo: Decimal | None
    maximo: Decimal | None
    apertura: str  # "apertura_s" (parámetro de la configuración) o "0"
    escala: bool
    incidente: str | None = None
    descripcion_incidente: str = ""

    def anomala(self, valor: Decimal) -> bool:
        return (self.minimo is not None and valor < self.minimo) or (self.maximo is not None and valor > self.maximo)

    def evaluar(self, valor: Decimal | None, t: datetime, seq: Secuencia, activa: AlertaActiva | None,
                cfg: Configuracion) -> tuple[Secuencia, list[Accion]]:
        if valor is None:
            return seq, []  # sin medición de esta variable: no cuenta ni rompe
        if seq.ultima is not None and (t - seq.ultima).total_seconds() > cfg.continuidad_max_s:
            seq = Secuencia()  # hueco: la secuencia arranca de nuevo
        apertura_s = getattr(cfg, self.apertura) if self.apertura != "0" else 0
        acciones: list[Accion] = []
        if self.anomala(valor):
            desde = seq.anomala_desde or t
            duracion = (t - desde).total_seconds()
            nueva = Secuencia(anomala_desde=desde, normal_desde=None, ultima=t)
            alta = self.escala and duracion >= cfg.escalamiento_s
            if activa is None:
                if duracion >= apertura_s:
                    severidad = ALTA if alta else MEDIA
                    acciones.append(AbrirAlerta(self.tipo, severidad, t, valor))
                    if alta and self.incidente:
                        acciones.append(self._incidente(t))
            elif alta and activa.severidad != ALTA:
                acciones.append(EscalarAlerta(self.tipo, ALTA, t))
                if self.incidente and not activa.incidente:
                    acciones.append(self._incidente(t))
            else:
                acciones.append(ActualizarAlerta(self.tipo, t, valor))
                if alta and self.incidente and not activa.incidente:
                    acciones.append(self._incidente(t))  # p. ej. ya era ALTA sin incidente
            return nueva, acciones
        # En rango.
        if activa is None:
            return Secuencia(anomala_desde=None, normal_desde=None, ultima=t), []
        normal_desde = seq.normal_desde or t
        if (t - normal_desde).total_seconds() >= cfg.normalizacion_s:
            return Secuencia(ultima=t), [CerrarAlerta(self.tipo, t)]
        return Secuencia(anomala_desde=None, normal_desde=normal_desde, ultima=t), []

    def _incidente(self, t: datetime) -> AbrirIncidente:
        return AbrirIncidente(self.incidente, self.tipo, ALTA, self.descripcion_incidente, t)


@dataclass(frozen=True)
class ReglaBateria:
    """Batería bajo el mínimo: abre BAJA con una lectura; normaliza al superar mínimo + histéresis."""

    minimo: Decimal | None
    tipo: str = BATERIA
    variable: str = "bateria_pct"

    def evaluar(self, valor: Decimal | None, t: datetime, seq: Secuencia, activa: AlertaActiva | None,
                cfg: Configuracion) -> tuple[Secuencia, list[Accion]]:
        if valor is None or self.minimo is None:
            return seq, []
        nueva = Secuencia(ultima=t)
        if valor < self.minimo:
            accion = ActualizarAlerta(self.tipo, t, valor) if activa else AbrirAlerta(self.tipo, BAJA, t, valor)
            return replace(nueva, anomala_desde=seq.anomala_desde or t), [accion]
        if activa is not None and valor >= self.minimo + cfg.histeresis_bateria:
            return nueva, [CerrarAlerta(self.tipo, t)]
        return nueva, []


def reglas_de(condicion: Condicion) -> list[ReglaRango | ReglaBateria]:
    """Las Strategies que aplican a una condición (una por variable controlada)."""
    reglas: list[ReglaRango | ReglaBateria] = []
    if condicion.temperatura_min_c is not None or condicion.temperatura_max_c is not None:
        reglas.append(ReglaRango(TEMPERATURA, "temperatura_c", condicion.temperatura_min_c,
                                 condicion.temperatura_max_c, "apertura_s", escala=True,
                                 incidente=INCIDENTE_TEMPERATURA,
                                 descripcion_incidente="Desviación prolongada de temperatura"))
    if condicion.humedad_min_pct is not None or condicion.humedad_max_pct is not None:
        reglas.append(ReglaRango(HUMEDAD, "humedad_pct", condicion.humedad_min_pct, condicion.humedad_max_pct,
                                 "apertura_s", escala=True))
    if condicion.vibracion_max_g is not None:
        reglas.append(ReglaRango(VIBRACION, "vibracion_g", None, condicion.vibracion_max_g, "0", escala=False))
    if condicion.bateria_min_pct is not None:
        reglas.append(ReglaBateria(condicion.bateria_min_pct))
    return reglas


# --------------------------------------------------------------------------- evaluación
def evaluar_lectura(lectura: Lectura, estado: EstadoSensor, condicion: Condicion,
                    alertas_activas: dict[str, AlertaActiva]) -> Resultado:
    """Evalúa UNA lectura nueva. `alertas_activas`: las del sensor en el contexto de la lectura, por tipo."""
    clasificacion = clasificar(lectura, estado, condicion)
    if clasificacion != Clasificacion.EVALUAR:
        return Resultado(clasificacion, estado, [])
    cfg = condicion.configuracion
    # Cambio de contexto (otra instalación u otro envío): las secuencias arrancan de cero; el histórico queda.
    secuencias = dict(estado.secuencias) if estado.contexto == lectura.contexto else {}
    acciones: list[Accion] = []
    t = lectura.fecha_medicion
    for regla in reglas_de(condicion):
        seq, nuevas = regla.evaluar(lectura.mediciones.get(regla.variable), t,
                                    secuencias.get(regla.tipo, Secuencia()), alertas_activas.get(regla.tipo), cfg)
        secuencias[regla.tipo] = seq
        acciones.extend(nuevas)
    if SIN_COMUNICACION in alertas_activas:  # llegó una lectura nueva y reciente: hay contacto
        acciones.append(CerrarAlerta(SIN_COMUNICACION, t))
    nuevo = EstadoSensor(lectura.contexto, t, lectura.id, secuencias)
    return Resultado(clasificacion, nuevo, acciones)


def evaluar_comunicacion(*, ultimo_contacto: datetime, ahora: datetime, condicion: Condicion,
                         activa: AlertaActiva | None) -> Accion | None:
    """Falta de comunicación CALCULADA: no es un estado administrativo del sensor.

    Abre MEDIA al superar `tiempo_max_sin_lecturas_seg` (180 s por defecto) y pasa a ALTA al superar
    `sin_comunicacion_alta_s` (600 s). La normaliza una lectura nueva y reciente (`evaluar_lectura`).
    """
    sin_contacto = (ahora - ultimo_contacto).total_seconds()
    if sin_contacto <= condicion.tiempo_max_sin_lecturas_seg:
        return None
    alta = sin_contacto > condicion.configuracion.sin_comunicacion_alta_s
    if activa is None:
        return AbrirAlerta(SIN_COMUNICACION, ALTA if alta else MEDIA, ahora)
    if alta and activa.severidad != ALTA:
        return EscalarAlerta(SIN_COMUNICACION, ALTA, ahora)
    return ActualizarAlerta(SIN_COMUNICACION, ahora)


def _iso(valor: datetime | None) -> str | None:
    return valor.isoformat() if valor else None


def _fecha(valor: str | None) -> datetime | None:
    return datetime.fromisoformat(valor) if valor else None


# --------------------------------------------------------------------------- validación de entradas
# Mismos rangos que el validador $jsonSchema de lecturas_sensor (db/mongo/setup_mongo.py): se rechaza antes de
# llegar a Mongo con un mensaje claro.
RANGOS_MEDICION = {
    "temperatura_c": (Decimal("-100"), Decimal("150")),
    "humedad_pct": (Decimal("0"), Decimal("100")),
    "vibracion_g": (Decimal("0"), None),
    "bateria_pct": (Decimal("0"), Decimal("100")),
}
ORIGENES = ("SIMULADOR", "IMPORTADOR", "DISPOSITIVO")


def validar_mediciones(mediciones: dict[str, Any]) -> dict[str, Decimal]:
    """Normaliza a Decimal y valida rangos. Las variables sin medición simplemente no se incluyen."""
    desconocidas = set(mediciones) - set(RANGOS_MEDICION)
    if desconocidas:
        raise ValidationError(f"mediciones desconocidas: {sorted(desconocidas)}",
                              user_message="La lectura tiene variables desconocidas.")
    salida: dict[str, Decimal] = {}
    for variable, valor in mediciones.items():
        if valor is None:
            continue
        try:
            d = Decimal(str(valor))
        except ArithmeticError:
            raise ValidationError(f"{variable} no numérica", user_message=f"{variable} debe ser un número.") from None
        minimo, maximo = RANGOS_MEDICION[variable]
        if not d.is_finite() or d < minimo or (maximo is not None and d > maximo):
            raise ValidationError(f"{variable} fuera de rango: {valor}",
                                  user_message=f"{variable} fuera del rango físico admitido.")
        salida[variable] = d
    if not salida:
        raise ValidationError("lectura sin mediciones", user_message="La lectura no trae ninguna medición.")
    return salida


def validar_posicion(latitud: Any, longitud: Any) -> tuple[float, float] | None:
    if latitud is None and longitud is None:
        return None
    if latitud is None or longitud is None:
        raise ValidationError("posición incompleta", user_message="La posición necesita latitud y longitud.")
    lat, lon = float(latitud), float(longitud)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValidationError("posición fuera de rango", user_message="Latitud o longitud fuera de rango.")
    return lat, lon


def validar_condicion(datos: Any) -> dict[str, Any]:
    """Umbrales de un envío, normalizados (Decimal) y coherentes (mínimo <= máximo, rangos físicos)."""
    def dec(nombre: str) -> Decimal | None:
        valor = getattr(datos, nombre)
        if valor is None or str(valor).strip() == "":
            return None
        try:
            numero = Decimal(str(valor).strip().replace(",", "."))
        except ArithmeticError:
            raise ValidationError(nombre, user_message=f"El umbral {nombre} debe ser un número.") from None
        if not numero.is_finite():
            raise ValidationError(nombre, user_message=f"El umbral {nombre} debe ser un número finito.")
        return numero

    v = {n: dec(n) for n in ("temperatura_min_c", "temperatura_max_c", "humedad_min_pct", "humedad_max_pct",
                             "vibracion_max_g", "bateria_min_pct")}
    for minimo, maximo in (("temperatura_min_c", "temperatura_max_c"), ("humedad_min_pct", "humedad_max_pct")):
        if v[minimo] is not None and v[maximo] is not None and v[minimo] > v[maximo]:
            raise ValidationError(f"{minimo} > {maximo}", user_message="El mínimo no puede superar al máximo.")
    for nombre in ("humedad_min_pct", "humedad_max_pct", "bateria_min_pct"):
        if v[nombre] is not None and not Decimal("0") <= v[nombre] <= Decimal("100"):
            raise ValidationError(nombre, user_message="Humedad y batería son porcentajes entre 0 y 100.")
    if v["vibracion_max_g"] is not None and v["vibracion_max_g"] < 0:
        raise ValidationError("vibración negativa", user_message="La vibración máxima no puede ser negativa.")
    try:
        tiempo = int(datos.tiempo_max_sin_lecturas_seg)
    except (TypeError, ValueError):
        raise ValidationError("tiempo", user_message="El tiempo sin lecturas es un número de segundos.") from None
    if tiempo <= 0:
        raise ValidationError("tiempo", user_message="El tiempo sin lecturas debe ser mayor que cero.")
    return {**v, "tiempo_max_sin_lecturas_seg": tiempo}


def condicion_vacia(version_reglas: str = "1.0") -> Condicion:
    """Envío sin condición cargada: no se controla ninguna variable, pero sí el contacto (180 s)."""
    return Condicion(None, None, None, None, None, None, 180, version_reglas, {})
