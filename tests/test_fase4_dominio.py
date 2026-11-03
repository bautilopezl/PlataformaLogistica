"""Fase 4 — reglas de monitoreo puras (AGENTS.md §10.4). Sin servidores y sin reloj real: `ahora` se inyecta."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from logistica.domain import monitoreo as m  # noqa: E402

T0 = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)
ENVIO, CONT, ASIG, SENSOR = (uuid.uuid4() for _ in range(4))
CONTEXTO = m.Contexto(envio_id=ENVIO, contenedor_id=CONT, asignacion_sensor_id=ASIG, tramo_id=None)
CONDICION = m.Condicion(temperatura_min_c=Decimal("2"), temperatura_max_c=Decimal("8"), humedad_min_pct=Decimal("40"),
                        humedad_max_pct=Decimal("80"), vibracion_max_g=Decimal("2.5"), bateria_min_pct=Decimal("20"),
                        tiempo_max_sin_lecturas_seg=180, version_reglas="1.0", reglas={})


def s(n: float) -> datetime:
    return T0 + timedelta(seconds=n)


def lectura(seg: float, *, recibida: float | None = None, contexto=CONTEXTO, lid=None, **mediciones):
    return m.Lectura(id=lid or uuid.uuid4(), sensor_id=SENSOR, fecha_medicion=s(seg),
                     fecha_recepcion=s(seg if recibida is None else recibida), contexto=contexto,
                     mediciones={k: Decimal(str(v)) for k, v in mediciones.items()})


class Simulacion:
    """Encadena evaluaciones como lo haría el servicio: guarda el estado y las alertas activas entre lecturas."""

    def __init__(self, condicion=CONDICION):
        self.condicion = condicion
        self.estado = m.EstadoSensor.inicial()
        self.alertas: dict[str, m.AlertaActiva] = {}
        self.acciones: list[m.Accion] = []

    def leer(self, lect: m.Lectura) -> m.Resultado:
        r = m.evaluar_lectura(lect, self.estado, self.condicion, self.alertas)
        if r.actualiza_estado:
            self.estado = r.estado
        for a in r.acciones:
            self.acciones.append(a)
            if isinstance(a, (m.AbrirAlerta, m.EscalarAlerta)):
                self.alertas[a.tipo] = m.AlertaActiva(a.tipo, a.severidad, incidente=False)
            elif isinstance(a, m.CerrarAlerta):
                self.alertas.pop(a.tipo, None)
            elif isinstance(a, m.AbrirIncidente):
                self.alertas[a.tipo_alerta] = m.AlertaActiva(a.tipo_alerta, self.alertas[a.tipo_alerta].severidad,
                                                             incidente=True)
        return r

    def de_tipo(self, clase):
        return [a for a in self.acciones if isinstance(a, clase)]


# --------------------------------------------------------------------------- configuración versionada
def test_parametros_por_defecto_de_la_version_1_y_sobrescribibles():
    c = m.Configuracion.desde(version="1.0", reglas={})
    assert (c.apertura_s, c.escalamiento_s, c.normalizacion_s, c.continuidad_max_s) == (120, 300, 120, 120)
    assert (c.tardia_s, c.reloj_adelantado_s, c.histeresis_bateria, c.sin_comunicacion_alta_s) == (120, 120, 5, 600)
    c2 = m.Configuracion.desde(version="1.1", reglas={"apertura_s": 90})
    assert c2.apertura_s == 90 and c2.version == "1.1"
    with pytest.raises(m.ValidationError):
        m.Configuracion.desde(version="1.0", reglas={"inventado": 1})


# --------------------------------------------------------------------------- temperatura (caso principal)
def test_temperatura_sostenida_abre_a_120_escala_a_300_e_incidente_automatico():
    sim = Simulacion()
    for seg in (0, 60):
        sim.leer(lectura(seg, temperatura_c=12))
    assert not sim.de_tipo(m.AbrirAlerta)  # 60 s fuera de rango todavía no alcanza
    sim.leer(lectura(120, temperatura_c=12))
    abiertas = sim.de_tipo(m.AbrirAlerta)
    assert [(a.tipo, a.severidad, a.detectada_en) for a in abiertas] == [("TEMPERATURA", "MEDIA", s(120))]
    for seg in (180, 240):
        sim.leer(lectura(seg, temperatura_c=12))
    assert not sim.de_tipo(m.EscalarAlerta) and len(sim.de_tipo(m.ActualizarAlerta)) == 2
    sim.leer(lectura(300, temperatura_c=12))
    assert [(a.tipo, a.severidad) for a in sim.de_tipo(m.EscalarAlerta)] == [("TEMPERATURA", "ALTA")]
    incidentes = sim.de_tipo(m.AbrirIncidente)
    assert len(incidentes) == 1 and incidentes[0].tipo == "DESVIACION_TEMPERATURA"
    sim.leer(lectura(360, temperatura_c=12))
    assert len(sim.de_tipo(m.AbrirIncidente)) == 1  # nunca un segundo incidente por la misma alerta


def test_temperatura_vuelve_a_rango_120_s_cierra_la_alerta_pero_no_resuelve_el_incidente():
    sim = Simulacion()
    for seg in range(0, 301, 60):
        sim.leer(lectura(seg, temperatura_c=12))
    for seg in (360, 420):
        sim.leer(lectura(seg, temperatura_c=5))
    assert not sim.de_tipo(m.CerrarAlerta)  # 60 s en rango: todavía no
    sim.leer(lectura(480, temperatura_c=5))
    cierres = sim.de_tipo(m.CerrarAlerta)
    assert [(c.tipo, c.normalizada_en) for c in cierres] == [("TEMPERATURA", s(480))]
    assert not any(isinstance(a, m.ResolverIncidente) for a in sim.acciones)  # el sistema no resuelve incidentes


def test_humedad_sostenida_escala_pero_el_incidente_es_manual():
    sim = Simulacion()
    for seg in range(0, 301, 60):
        sim.leer(lectura(seg, humedad_pct=95))
    assert [a.severidad for a in sim.de_tipo(m.EscalarAlerta)] == ["ALTA"]
    assert not sim.de_tipo(m.AbrirIncidente)


# --------------------------------------------------------------------------- reglas finas
def test_lectura_mas_vieja_que_la_ultima_evaluada_no_retrocede_el_estado():
    sim = Simulacion()
    sim.leer(lectura(0, temperatura_c=12))
    sim.leer(lectura(120, temperatura_c=12))  # alerta abierta
    r = sim.leer(lectura(60, temperatura_c=5))  # llega tarde y desordenada: no "normaliza" nada
    assert r.clasificacion == m.Clasificacion.ANTERIOR and not r.actualiza_estado and not r.acciones


def test_lectura_tardia_se_conserva_como_historico_pero_no_actualiza_el_estado():
    sim = Simulacion()
    r = sim.leer(lectura(0, recibida=121, temperatura_c=12))
    assert r.clasificacion == m.Clasificacion.TARDIA and not r.actualiza_estado and not r.acciones
    assert lectura(0, recibida=120, temperatura_c=12)  # 120 s exactos todavía es válida
    assert m.clasificar(lectura(0, recibida=120), m.EstadoSensor.inicial(), CONDICION) == m.Clasificacion.EVALUAR


def test_reloj_adelantado_mas_de_120_s_se_rechaza_para_revision():
    assert m.clasificar(lectura(200, recibida=79), m.EstadoSensor.inicial(), CONDICION) == m.Clasificacion.RELOJ_ADELANTADO
    assert m.clasificar(lectura(200, recibida=80), m.EstadoSensor.inicial(), CONDICION) == m.Clasificacion.EVALUAR


def test_lectura_sin_contexto_queda_para_revision_y_nunca_inventa_uno():
    r = m.evaluar_lectura(lectura(0, contexto=None, temperatura_c=50), m.EstadoSensor.inicial(), CONDICION, {})
    assert r.clasificacion == m.Clasificacion.SIN_CONTEXTO and not r.acciones and not r.actualiza_estado


def test_hueco_mayor_a_120_s_reinicia_la_secuencia():
    sim = Simulacion()
    sim.leer(lectura(0, temperatura_c=12))
    sim.leer(lectura(60, temperatura_c=12))
    sim.leer(lectura(240, temperatura_c=12))  # hueco de 180 s: no se presume que siguió fuera de rango
    sim.leer(lectura(300, temperatura_c=12))
    assert not sim.de_tipo(m.AbrirAlerta)  # la nueva secuencia lleva 60 s
    sim.leer(lectura(360, temperatura_c=12))
    assert [a.detectada_en for a in sim.de_tipo(m.AbrirAlerta)] == [s(360)]  # 120 s desde el reinicio (240)


def test_cambio_de_contexto_reinicia_las_secuencias():
    sim = Simulacion()
    sim.leer(lectura(0, temperatura_c=12))
    sim.leer(lectura(60, temperatura_c=12))
    otro = m.Contexto(envio_id=uuid.uuid4(), contenedor_id=CONT, asignacion_sensor_id=ASIG, tramo_id=None)
    sim.leer(lectura(120, contexto=otro, temperatura_c=12))  # otro envío: no hereda la secuencia
    assert not sim.de_tipo(m.AbrirAlerta)
    assert sim.estado.contexto == otro


def test_campo_sin_medicion_no_rompe_ni_cuenta():
    sim = Simulacion()
    sim.leer(lectura(0, temperatura_c=12))
    sim.leer(lectura(60, bateria_pct=90))  # sin temperatura: esa variable no se actualiza
    sim.leer(lectura(120, temperatura_c=12))
    assert [a.detectada_en for a in sim.de_tipo(m.AbrirAlerta)] == [s(120)]


def test_condicion_sin_umbral_no_evalua_esa_variable():
    sin_temp = m.Condicion(**{**CONDICION.__dict__, "temperatura_min_c": None, "temperatura_max_c": None})
    sim = Simulacion(sin_temp)
    for seg in range(0, 301, 60):
        sim.leer(lectura(seg, temperatura_c=99))
    assert not sim.acciones


# --------------------------------------------------------------------------- vibración y batería
def test_vibracion_abre_con_una_sola_lectura_y_normaliza_tras_120_s_sin_superaciones():
    sim = Simulacion()
    sim.leer(lectura(0, vibracion_g=3))
    assert [(a.tipo, a.severidad) for a in sim.de_tipo(m.AbrirAlerta)] == [("VIBRACION", "MEDIA")]
    sim.leer(lectura(60, vibracion_g=1))
    sim.leer(lectura(120, vibracion_g=3))  # vuelve a superar: se reinicia la normalización
    sim.leer(lectura(180, vibracion_g=1))
    sim.leer(lectura(240, vibracion_g=1))
    assert not sim.de_tipo(m.CerrarAlerta)
    sim.leer(lectura(300, vibracion_g=1))
    assert [c.tipo for c in sim.de_tipo(m.CerrarAlerta)] == ["VIBRACION"]
    assert not sim.de_tipo(m.AbrirIncidente)  # incidente manual


def test_bateria_baja_abre_baja_y_cierra_con_histeresis_de_5_puntos():
    sim = Simulacion()
    sim.leer(lectura(0, bateria_pct=19))
    assert [(a.tipo, a.severidad) for a in sim.de_tipo(m.AbrirAlerta)] == [("BATERIA", "BAJA")]
    sim.leer(lectura(60, bateria_pct=24))  # sobre el mínimo pero dentro de la histéresis: sigue abierta
    assert not sim.de_tipo(m.CerrarAlerta)
    sim.leer(lectura(120, bateria_pct=25))
    assert [c.tipo for c in sim.de_tipo(m.CerrarAlerta)] == ["BATERIA"]


# --------------------------------------------------------------------------- sin comunicación (calculado)
def test_sin_comunicacion_media_a_180_alta_a_600():
    c = CONDICION
    assert m.evaluar_comunicacion(ultimo_contacto=s(0), ahora=s(180), condicion=c, activa=None) is None
    abrir = m.evaluar_comunicacion(ultimo_contacto=s(0), ahora=s(181), condicion=c, activa=None)
    assert isinstance(abrir, m.AbrirAlerta) and (abrir.tipo, abrir.severidad) == ("SIN_COMUNICACION", "MEDIA")
    media = m.AlertaActiva("SIN_COMUNICACION", "MEDIA", incidente=False)
    assert isinstance(m.evaluar_comunicacion(ultimo_contacto=s(0), ahora=s(400), condicion=c, activa=media),
                      m.ActualizarAlerta)
    escalar = m.evaluar_comunicacion(ultimo_contacto=s(0), ahora=s(601), condicion=c, activa=media)
    assert isinstance(escalar, m.EscalarAlerta) and escalar.severidad == "ALTA"


def test_lectura_nueva_y_reciente_normaliza_la_falta_de_comunicacion():
    sim = Simulacion()
    sim.alertas["SIN_COMUNICACION"] = m.AlertaActiva("SIN_COMUNICACION", "MEDIA", incidente=False)
    sim.leer(lectura(0, recibida=200, temperatura_c=5))  # tardía: NO normaliza
    assert not sim.de_tipo(m.CerrarAlerta)
    sim.leer(lectura(300, temperatura_c=5))
    assert [c.tipo for c in sim.de_tipo(m.CerrarAlerta)] == ["SIN_COMUNICACION"]


def test_contacto_ultimo_solo_avanza_con_lecturas_que_actualizan_el_estado():
    sim = Simulacion()
    sim.leer(lectura(100, temperatura_c=5))
    sim.leer(lectura(50, temperatura_c=5))  # anterior: no mueve el último contacto
    assert sim.estado.ultima_medicion_en == s(100)


# --------------------------------------------------------------------------- estado serializable
def test_estado_se_guarda_y_se_recupera_igual():
    sim = Simulacion()
    for seg in (0, 60):
        sim.leer(lectura(seg, temperatura_c=12, vibracion_g=3))
    recuperado = m.EstadoSensor.desde_json(sim.estado.a_json())
    assert recuperado == sim.estado
