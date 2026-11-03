"""Dominio de logística: estados, magnitudes y reglas puras de AGENTS.md §10.1 y §10.2 (sin I/O).

Los SUPUESTOS (S1..S10) son decisiones del agente que no figuran en la consigna ni en los Documentos 1/2;
están listados en AGENTS.md §16 y deben revisarse con el grupo.
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from logistica.core.errors import BusinessRuleError, ValidationError


# --------------------------------------------------------------------------- catálogos (CHECK del DDL)
class TipoUbicacion(StrEnum):
    DEPOSITO = "DEPOSITO"
    PUERTO = "PUERTO"
    AEROPUERTO = "AEROPUERTO"
    OTRO = "OTRO"


class EstadoRecurso(StrEnum):
    """Estado de vehículos, contenedores y sensores."""

    HABILITADO = "HABILITADO"
    MANTENIMIENTO = "MANTENIMIENTO"
    BAJA = "BAJA"


class TipoVehiculo(StrEnum):
    CAMION = "CAMION"
    BARCO = "BARCO"
    AERONAVE = "AERONAVE"


class TipoContenedor(StrEnum):
    SECO = "SECO"
    REFRIGERADO = "REFRIGERADO"
    CISTERNA = "CISTERNA"
    OTRO = "OTRO"


class TipoSensor(StrEnum):
    AMBIENTAL = "AMBIENTAL"
    GPS = "GPS"
    MULTISENSOR = "MULTISENSOR"
    OTRO = "OTRO"


class VariableSensor(StrEnum):
    TEMPERATURA = "TEMPERATURA"
    HUMEDAD = "HUMEDAD"
    VIBRACION = "VIBRACION"
    GPS = "GPS"
    BATERIA = "BATERIA"


class Medio(StrEnum):
    TERRESTRE = "TERRESTRE"
    MARITIMO = "MARITIMO"
    AEREO = "AEREO"


class EstadoEnvio(StrEnum):
    BORRADOR = "BORRADOR"
    PLANIFICADO = "PLANIFICADO"
    EN_TRANSITO = "EN_TRANSITO"
    ENTREGADO = "ENTREGADO"
    CANCELADO = "CANCELADO"


class Prioridad(StrEnum):
    BAJA = "BAJA"
    NORMAL = "NORMAL"
    ALTA = "ALTA"


class EstadoTramo(StrEnum):
    PENDIENTE = "PENDIENTE"
    EN_CURSO = "EN_CURSO"
    FINALIZADO = "FINALIZADO"
    CANCELADO = "CANCELADO"


class EstadoAsignacion(StrEnum):
    RESERVADA = "RESERVADA"
    EN_USO = "EN_USO"
    FINALIZADA = "FINALIZADA"
    CANCELADA = "CANCELADA"


# SUPUESTO S6: cada tipo de vehículo sirve para un solo medio.
MEDIO_DE_VEHICULO = {
    TipoVehiculo.CAMION: Medio.TERRESTRE,
    TipoVehiculo.BARCO: Medio.MARITIMO,
    TipoVehiculo.AERONAVE: Medio.AEREO,
}

# Transiciones de estado permitidas del envío. SUPUESTO S8: en tránsito no se cancela.
TRANSICIONES_ENVIO: dict[EstadoEnvio, frozenset[EstadoEnvio]] = {
    EstadoEnvio.BORRADOR: frozenset({EstadoEnvio.PLANIFICADO, EstadoEnvio.CANCELADO}),
    EstadoEnvio.PLANIFICADO: frozenset(
        {EstadoEnvio.BORRADOR, EstadoEnvio.EN_TRANSITO, EstadoEnvio.CANCELADO}
    ),
    EstadoEnvio.EN_TRANSITO: frozenset({EstadoEnvio.ENTREGADO}),
    EstadoEnvio.ENTREGADO: frozenset(),
    EstadoEnvio.CANCELADO: frozenset(),
}
# Mientras el envío está en estos estados se pueden cambiar tramos, contenedores y condiciones.
ESTADOS_EDITABLES = frozenset({EstadoEnvio.BORRADOR, EstadoEnvio.PLANIFICADO})


def exigir_transicion(actual: str, nuevo: EstadoEnvio) -> None:
    if nuevo not in TRANSICIONES_ENVIO[EstadoEnvio(actual)]:
        raise BusinessRuleError(
            f"transición {actual} -> {nuevo}",
            user_message=f"Un envío {_legible(actual)} no puede pasar a {_legible(nuevo)}.",
        )


def exigir_editable(estado: str) -> None:
    if EstadoEnvio(estado) not in ESTADOS_EDITABLES:
        raise BusinessRuleError(
            f"envío no editable en {estado}",
            user_message=f"El envío está {_legible(estado)}: ya no se puede modificar su planificación.",
        )


# --------------------------------------------------------------------------- magnitudes y fechas
_ESCALA = Decimal("0.001")  # NUMERIC(14,3)
_MAXIMO = Decimal("99999999999.999")


def magnitud(valor: Any, nombre: str) -> Decimal:
    """Peso o volumen positivo con 3 decimales (NUMERIC(14,3)). Nunca float: se parte del texto."""
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{nombre} inválido", user_message=f"El {nombre} debe ser un número.") from None
    if not d.is_finite() or d <= 0 or d > _MAXIMO:
        raise ValidationError(
            f"{nombre} fuera de rango", user_message=f"El {nombre} debe ser mayor que cero."
        )
    if d.quantize(_ESCALA) != d:
        raise ValidationError(
            f"{nombre} con más de 3 decimales", user_message=f"El {nombre} admite hasta 3 decimales."
        )
    return d


def importe(valor: Any) -> Decimal:
    """Importe >= 0 con 2 decimales (NUMERIC(18,2))."""
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValidationError("importe inválido", user_message="El costo debe ser un número.") from None
    if not d.is_finite() or d < 0 or d > Decimal("9999999999999999.99") or d.quantize(Decimal("0.01")) != d:
        raise ValidationError(
            "importe fuera de rango",
            user_message="El costo debe ser mayor o igual a cero, con hasta 2 decimales.",
        )
    return d


def exigir_con_zona(*fechas: datetime | None) -> None:
    for f in fechas:
        if f is not None and f.tzinfo is None:
            raise ValidationError(
                "fecha sin zona horaria", user_message="Las fechas deben tener zona horaria."
            )


def generar_codigo_envio(ahora: datetime) -> str:
    """SUPUESTO S10: ENV-AAAAMMDD-XXXXXX (6 caracteres aleatorios en mayúsculas y dígitos)."""
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O ni 1/I para que se pueda dictar
    sufijo = "".join(secrets.choice(alfabeto) for _ in range(6))
    return f"ENV-{ahora:%Y%m%d}-{sufijo}"


# --------------------------------------------------------------------------- datos de entrada
def _texto(valor: str | None, nombre: str, maximo: int, obligatorio: bool = True) -> str | None:
    v = (valor or "").strip()
    if not v:
        if obligatorio:
            raise ValidationError(f"{nombre} vacío", user_message=f"El campo {nombre} es obligatorio.")
        return None
    if len(v) > maximo:
        raise ValidationError(
            f"{nombre} largo", user_message=f"El campo {nombre} admite hasta {maximo} caracteres."
        )
    return v


def _pais(valor: str) -> str:
    p = (valor or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", p):
        raise ValidationError(
            "país", user_message="El país debe ser un código ISO de 2 letras (por ejemplo AR)."
        )
    return p


def _opcion(valor: str, enum: type[StrEnum], nombre: str) -> str:
    v = (valor or "").strip().upper()
    if v not in {e.value for e in enum}:
        raise ValidationError(
            f"{nombre} inválido: {valor}", user_message=f"El valor de {nombre} no es válido."
        )
    return v


@dataclass(frozen=True)
class UbicacionDatos:
    nombre: str
    tipo: str
    pais_codigo: str
    ciudad: str
    direccion: str | None = None
    latitud: Any = None
    longitud: Any = None

    def normalizado(self) -> UbicacionDatos:
        lat = _coordenada(self.latitud, -90, 90, "latitud")
        lon = _coordenada(self.longitud, -180, 180, "longitud")
        if (lat is None) != (lon is None):
            raise ValidationError("coordenadas", user_message="Latitud y longitud van juntas (o ninguna).")
        return UbicacionDatos(
            _texto(self.nombre, "nombre", 150),
            _opcion(self.tipo, TipoUbicacion, "tipo"),
            _pais(self.pais_codigo),
            _texto(self.ciudad, "ciudad", 150),
            _texto(self.direccion, "dirección", 10_000, obligatorio=False),
            lat,
            lon,
        )


def _coordenada(valor: Any, minimo: int, maximo: int, nombre: str) -> Decimal | None:
    if valor is None or str(valor).strip() == "":
        return None
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except InvalidOperation:
        raise ValidationError(nombre, user_message=f"La {nombre} debe ser un número.") from None
    if not d.is_finite() or not minimo <= d <= maximo:
        raise ValidationError(nombre, user_message=f"La {nombre} debe estar entre {minimo} y {maximo}.")
    return d.quantize(Decimal("0.000001"))  # NUMERIC(9,6)


@dataclass(frozen=True)
class VehiculoDatos:
    operador_id: uuid.UUID
    tipo: str
    identificacion: str

    def normalizado(self) -> VehiculoDatos:
        return VehiculoDatos(
            self.operador_id,
            _opcion(self.tipo, TipoVehiculo, "tipo de vehículo"),
            _texto(self.identificacion, "identificación", 80).upper(),
        )


@dataclass(frozen=True)
class ContenedorDatos:
    codigo_internacional: str
    tipo: str
    capacidad_kg: Any
    capacidad_m3: Any

    def normalizado(self) -> ContenedorDatos:
        codigo = _texto(self.codigo_internacional, "código internacional", 20).upper().replace(" ", "")
        return ContenedorDatos(
            codigo,
            _opcion(self.tipo, TipoContenedor, "tipo de contenedor"),
            magnitud(self.capacidad_kg, "capacidad en kg"),
            magnitud(self.capacidad_m3, "capacidad en m³"),
        )


@dataclass(frozen=True)
class SensorDatos:
    codigo: str
    tipo: str
    fabricante: str
    modelo: str | None = None
    intervalo_esperado_seg: int = 60
    capacidades: frozenset[str] = field(default_factory=frozenset)

    def normalizado(self) -> SensorDatos:
        caps = frozenset(_opcion(c, VariableSensor, "variable") for c in self.capacidades)
        if not caps:
            raise ValidationError(
                "sin capacidades", user_message="El sensor debe medir al menos una variable."
            )
        try:
            intervalo = int(self.intervalo_esperado_seg)
        except (TypeError, ValueError):
            raise ValidationError(
                "intervalo", user_message="El intervalo debe ser un número entero."
            ) from None
        if not 0 < intervalo <= 86_400:  # hasta un día; INTEGER en el DDL
            raise ValidationError("intervalo", user_message="El intervalo esperado debe ser mayor que cero.")
        return SensorDatos(
            _texto(self.codigo, "código", 80).upper(),
            _opcion(self.tipo, TipoSensor, "tipo de sensor"),
            _texto(self.fabricante, "fabricante", 100),
            _texto(self.modelo, "modelo", 100, False),
            intervalo,
            caps,
        )


@dataclass(frozen=True)
class EnvioDatos:
    cliente_id: uuid.UUID
    origen_id: uuid.UUID
    destino_id: uuid.UUID
    descripcion_carga: str
    peso_kg: Any
    volumen_m3: Any
    prioridad: str = Prioridad.NORMAL

    def normalizado(self) -> EnvioDatos:
        if self.origen_id == self.destino_id:
            raise ValidationError(
                "origen = destino", user_message="El origen y el destino deben ser distintos."
            )
        return EnvioDatos(
            self.cliente_id,
            self.origen_id,
            self.destino_id,
            _texto(self.descripcion_carga, "descripción de la carga", 10_000),
            magnitud(self.peso_kg, "peso"),
            magnitud(self.volumen_m3, "volumen"),
            _opcion(self.prioridad, Prioridad, "prioridad"),
        )


@dataclass(frozen=True)
class TramoPlan:
    """Un tramo propuesto al planificar. El orden lo da su posición en la lista (1, 2, ...)."""

    origen_id: uuid.UUID
    destino_id: uuid.UUID
    medio: str
    operador_id: uuid.UUID
    salida_estimada_en: datetime
    llegada_estimada_en: datetime
    vehiculo_id: uuid.UUID | None = None  # puede faltar al planificar; es obligatorio al iniciar
    costo: Any = None
    moneda_codigo: str | None = None

    def normalizado(self) -> TramoPlan:
        exigir_con_zona(self.salida_estimada_en, self.llegada_estimada_en)
        if self.origen_id == self.destino_id:
            raise ValidationError(
                "tramo origen = destino", user_message="Un tramo une dos ubicaciones distintas."
            )
        if self.llegada_estimada_en < self.salida_estimada_en:
            raise ValidationError(
                "fechas del tramo",
                user_message="La llegada estimada no puede ser anterior a la salida estimada.",
            )
        costo = None if self.costo in (None, "") else importe(self.costo)
        moneda = (self.moneda_codigo or "").strip().upper() or None
        if (costo is None) != (moneda is None):
            raise ValidationError("costo y moneda", user_message="El costo y su moneda van juntos.")
        if moneda is not None and not re.fullmatch(r"[A-Z]{3}", moneda):
            raise ValidationError(
                "moneda", user_message="La moneda es un código ISO de 3 letras (por ejemplo USD)."
            )
        return TramoPlan(
            self.origen_id,
            self.destino_id,
            _opcion(self.medio, Medio, "medio"),
            self.operador_id,
            self.salida_estimada_en,
            self.llegada_estimada_en,
            self.vehiculo_id,
            costo,
            moneda,
        )


@dataclass(frozen=True)
class AsignacionDatos:
    contenedor_id: uuid.UUID
    peso_asignado_kg: Any
    volumen_asignado_m3: Any
    reserva_desde: datetime
    reserva_hasta: datetime

    def normalizado(self) -> AsignacionDatos:
        exigir_con_zona(self.reserva_desde, self.reserva_hasta)
        if self.reserva_hasta <= self.reserva_desde:
            raise ValidationError(
                "reserva", user_message="El fin de la reserva debe ser posterior a su inicio."
            )
        return AsignacionDatos(
            self.contenedor_id,
            magnitud(self.peso_asignado_kg, "peso asignado"),
            magnitud(self.volumen_asignado_m3, "volumen asignado"),
            self.reserva_desde,
            self.reserva_hasta,
        )


# --------------------------------------------------------------------------- reglas de §10.2
@dataclass(frozen=True)
class TramoRef:
    """Lo mínimo de un tramo para validar continuidad y secuencia."""

    orden: int
    origen_id: uuid.UUID
    destino_id: uuid.UUID
    salida_estimada_en: datetime
    llegada_estimada_en: datetime


def validar_continuidad(origen_envio: uuid.UUID, destino_envio: uuid.UUID, tramos: list[TramoRef]) -> None:
    """El recorrido va del origen al destino del envío y el destino de cada tramo es el origen del siguiente.

    SUPUESTO S5: además, un tramo no sale antes de la llegada estimada del anterior.
    """
    if not tramos:
        raise BusinessRuleError("sin tramos", user_message="El envío necesita al menos un tramo.")
    ordenados = sorted(tramos, key=lambda t: t.orden)
    if [t.orden for t in ordenados] != list(range(1, len(ordenados) + 1)):
        raise BusinessRuleError(
            "orden con huecos", user_message="Los tramos deben numerarse 1, 2, 3... sin huecos."
        )
    if ordenados[0].origen_id != origen_envio:
        raise BusinessRuleError(
            "primer tramo", user_message="El primer tramo debe salir del origen del envío."
        )
    if ordenados[-1].destino_id != destino_envio:
        raise BusinessRuleError(
            "último tramo", user_message="El último tramo debe llegar al destino del envío."
        )
    for anterior, siguiente in zip(ordenados, ordenados[1:]):
        if anterior.destino_id != siguiente.origen_id:
            raise BusinessRuleError(
                f"tramos {anterior.orden}-{siguiente.orden} no continuos",
                user_message=(
                    f"El tramo {siguiente.orden} debe salir de donde llega el tramo {anterior.orden}."
                ),
            )
        if siguiente.salida_estimada_en < anterior.llegada_estimada_en:
            raise BusinessRuleError(
                f"tramo {siguiente.orden} sale antes de llegar el {anterior.orden}",
                user_message=(
                    f"El tramo {siguiente.orden} no puede salir antes de que llegue el tramo "
                    f"{anterior.orden}."
                ),
            )


def validar_vehiculo_para_tramo(
    medio: str,
    operador_tramo: uuid.UUID,
    tipo_vehiculo: str,
    operador_vehiculo: uuid.UUID,
    estado_vehiculo: str,
) -> None:
    if operador_vehiculo != operador_tramo:
        raise BusinessRuleError(
            "vehículo de otro operador", user_message="El vehículo debe pertenecer al operador del tramo."
        )
    if MEDIO_DE_VEHICULO[TipoVehiculo(tipo_vehiculo)] != Medio(medio):
        raise BusinessRuleError(
            "vehículo incompatible",
            user_message=f"Un {_legible(tipo_vehiculo)} no sirve para un tramo {_legible(medio)}.",
        )
    if estado_vehiculo != EstadoRecurso.HABILITADO:
        raise BusinessRuleError("vehículo no habilitado", user_message="El vehículo no está habilitado.")


def validar_capacidad(peso: Decimal, volumen: Decimal, capacidad_kg: Decimal, capacidad_m3: Decimal) -> None:
    """La carga asignada a un contenedor respeta su capacidad total (SUPUESTO S7)."""
    if peso > capacidad_kg:
        raise BusinessRuleError(
            "excede kg",
            user_message=f"El peso asignado supera la capacidad del contenedor ({capacidad_kg} kg).",
        )
    if volumen > capacidad_m3:
        raise BusinessRuleError(
            "excede m3",
            user_message=f"El volumen asignado supera la capacidad del contenedor ({capacidad_m3} m³).",
        )


def validar_total_asignado(
    peso_envio: Decimal, volumen_envio: Decimal, peso_total: Decimal, volumen_total: Decimal
) -> None:
    """No se asigna a contenedores más carga de la que tiene el envío (SUPUESTO S7)."""
    if peso_total > peso_envio or volumen_total > volumen_envio:
        raise BusinessRuleError(
            "asignado > envío",
            user_message="La carga asignada a contenedores supera el peso o el volumen del envío.",
        )


def validar_inicio_tramo(
    orden: int, estado: str, vehiculo_id: uuid.UUID | None, estado_anterior: str | None
) -> None:
    if estado != EstadoTramo.PENDIENTE:
        raise BusinessRuleError("tramo no pendiente", user_message="Solo se inicia un tramo pendiente.")
    if vehiculo_id is None:
        raise BusinessRuleError("sin vehículo", user_message="Asigne un vehículo antes de iniciar el tramo.")
    if orden > 1 and estado_anterior != EstadoTramo.FINALIZADO:
        raise BusinessRuleError(
            "anterior sin finalizar",
            user_message=f"El tramo {orden - 1} debe estar finalizado antes de iniciar el {orden}.",
        )


def validar_fin_tramo(estado: str, salida_real: datetime | None, llegada_real: datetime) -> None:
    exigir_con_zona(llegada_real)
    if estado != EstadoTramo.EN_CURSO or salida_real is None:
        raise BusinessRuleError("tramo no en curso", user_message="Solo se finaliza un tramo en curso.")
    if llegada_real < salida_real:
        raise BusinessRuleError(
            "llegada < salida", user_message="La llegada real no puede ser anterior a la salida real."
        )


def validar_confirmacion_entrega(
    estados_tramos: list[str], ultima_llegada_real: datetime | None, entrega_real: datetime
) -> None:
    """La confirma Operaciones; llegar al último destino no la sustituye (AGENTS.md §10.1)."""
    exigir_con_zona(entrega_real)
    if not estados_tramos or any(e != EstadoTramo.FINALIZADO for e in estados_tramos):
        raise BusinessRuleError(
            "tramos sin finalizar",
            user_message="Todos los tramos deben estar finalizados para confirmar la entrega.",
        )
    if ultima_llegada_real is None or entrega_real < ultima_llegada_real:
        raise BusinessRuleError(
            "entrega antes de llegar",
            user_message="La entrega no puede ser anterior a la última llegada real.",
        )


def validar_cobertura_total(
    peso_envio: Decimal, volumen_envio: Decimal, peso_total: Decimal, volumen_total: Decimal
) -> None:
    """Para planificar (y salir), los contenedores deben llevar TODO el envío.

    S7, DECIDIDO POR EL GRUPO: en borrador se puede asignar carga parcial; planificar exige cobertura total.
    Junto con `validar_total_asignado` (nunca más que el envío), esto significa: asignado == envío.
    """
    if peso_total != peso_envio or volumen_total != volumen_envio:
        raise BusinessRuleError(
            f"cobertura parcial: {peso_total}/{peso_envio} kg, {volumen_total}/{volumen_envio} m3",
            user_message=(
                f"Los contenedores deben llevar toda la carga para planificar: asignados {peso_total} de "
                f"{peso_envio} kg y {volumen_total} de {volumen_envio} m³."
            ),
        )


# --------------------------------------------------------------------------- eventos logísticos
class TipoEvento(StrEnum):
    """Tipos de `eventos_logisticos` (deben coincidir con db/mongo/setup_mongo.py)."""

    CARGA_INICIADA = "CARGA_INICIADA"
    EN_TRANSITO = "EN_TRANSITO"
    TRAMO_INICIADO = "TRAMO_INICIADO"
    TRAMO_FINALIZADO = "TRAMO_FINALIZADO"
    DESCARGA = "DESCARGA"
    ENTREGADO = "ENTREGADO"
    CANCELADO = "CANCELADO"
    RETRASO = "RETRASO"
    ADUANA = "ADUANA"
    INCIDENTE_ABIERTO = "INCIDENTE_ABIERTO"  # los genera el monitoreo (fase 4)
    INCIDENTE_RESUELTO = "INCIDENTE_RESUELTO"


# Los que una persona registra a mano; el resto los genera el sistema al cambiar de estado.
EVENTOS_MANUALES = frozenset(
    {TipoEvento.CARGA_INICIADA, TipoEvento.DESCARGA, TipoEvento.RETRASO, TipoEvento.ADUANA}
)
ETAPAS_ADUANA = frozenset({"INGRESO", "INSPECCION", "LIBERACION"})


def validar_detalles_evento(tipo: str, detalles: dict[str, Any]) -> dict[str, Any]:
    """Mismas reglas que el validador $jsonSchema de Mongo, para rechazar antes de llegar a la outbox.

    Si algo pasara acá pero no en Mongo, el evento quedaría FALLIDO recién en la fase 3: por eso se normaliza
    igual que el esquema (etapa vacía = sin etapa, referencia como texto, demora entera y no negativa).
    """
    tipo = _opcion(tipo, TipoEvento, "tipo de evento")
    detalles = dict(detalles or {})
    if tipo == TipoEvento.RETRASO:
        motivo = str(detalles.get("motivo") or "").strip()
        if not motivo:
            raise ValidationError("retraso sin motivo", user_message="Un retraso debe indicar el motivo.")
        detalles["motivo"] = motivo
        demora = detalles.pop("demora_estimada_min", None)
        if demora not in (None, ""):
            detalles["demora_estimada_min"] = _minutos(demora)
    elif tipo == TipoEvento.ADUANA:
        etapa = str(detalles.pop("etapa", None) or "").strip().upper()
        if etapa:
            if etapa not in ETAPAS_ADUANA:
                raise ValidationError("etapa aduana", user_message="La etapa de aduana no es válida.")
            detalles["etapa"] = etapa
        if detalles.get("referencia") is not None:
            detalles["referencia"] = str(detalles["referencia"]).strip()
    return detalles


def _minutos(valor: Any) -> int:
    """Entero de minutos >= 0. No se trunca: 2.7 es un error, no 2."""
    if isinstance(valor, bool) or isinstance(valor, float):
        raise ValidationError(
            "demora no entera", user_message="La demora estimada es un número entero de minutos."
        )
    texto = str(valor).strip()
    if not re.fullmatch(r"\d+", texto):
        raise ValidationError(
            "demora inválida", user_message="La demora estimada es un número entero de minutos, sin signo."
        )
    return int(texto)


def _legible(valor: str) -> str:
    return str(valor).replace("_", " ").lower()
