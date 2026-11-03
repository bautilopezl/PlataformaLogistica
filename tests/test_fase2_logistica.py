"""Fase 2 — logística contra PostgreSQL real (identidad PG_APP_USER), sin dejar datos.

Aceptación (AGENTS.md §17): reglas de §10.2 verificadas (continuidad, capacidad, solapamientos, compromiso
inmutable) y un envío completo de punta a punta. Requiere LOGISTICA_TEST_CONNECTIONS=1.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from logistica.core.errors import BusinessRuleError, NotFoundError, PermissionDeniedError, ValidationError
from logistica.domain.acceso import UsuarioDatos
from logistica.domain.logistica import (
    AsignacionDatos,
    ContenedorDatos,
    EnvioDatos,
    SensorDatos,
    TramoPlan,
    UbicacionDatos,
    VehiculoDatos,
)
from logistica.services.catalogo_service import CatalogoService
from logistica.services.envio_service import EnvioService
from tests.test_fase1_acceso import CLAVE, Entorno

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y PostgreSQL levantado)",
)

# Fechas futuras: el DDL exige que las estimaciones no sean anteriores a la creación del envío.
T0 = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)


def h(n: float) -> datetime:
    return T0 + timedelta(hours=n)


class Logistica(Entorno):
    """Entorno de la fase 1 + catálogos: 3 ubicaciones, 2 operadores con vehículos, 2 contenedores."""

    def __init__(self, conns):
        super().__init__(conns)
        self.cat = CatalogoService(conns, self.aut)
        self.env = EnvioService(conns, self.aut)
        s = self.admin.sesion_id
        self.operador2 = self.terceros.crear(s, self._tercero("Aerolinea SA"), {"OPERADOR"})
        self.A = self.cat.crear_ubicacion(
            s, UbicacionDatos(f"Depósito A {self.sufijo}", "DEPOSITO", "AR", "Rosario")
        )
        self.B = self.cat.crear_ubicacion(
            s,
            UbicacionDatos(
                f"Puerto B {self.sufijo}", "PUERTO", "AR", "Buenos Aires", latitud="-34.6", longitud="-58.37"
            ),
        )
        self.C = self.cat.crear_ubicacion(
            s, UbicacionDatos(f"Puerto C {self.sufijo}", "PUERTO", "BR", "Santos")
        )
        self.camion = self.cat.crear_vehiculo(
            s, VehiculoDatos(self.operador_sa.id, "CAMION", f"AA{self.sufijo}")
        )
        self.barco = self.cat.crear_vehiculo(
            s, VehiculoDatos(self.operador_sa.id, "BARCO", f"IMO{self.sufijo}")
        )
        self.avion = self.cat.crear_vehiculo(
            s, VehiculoDatos(self.operador2.id, "AERONAVE", f"LV{self.sufijo}")
        )
        self.cont1 = self.cat.crear_contenedor(
            s, ContenedorDatos(f"MSKU{self.sufijo}1", "SECO", "20000", "33")
        )
        self.cont2 = self.cat.crear_contenedor(
            s, ContenedorDatos(f"MSKU{self.sufijo}2", "REFRIGERADO", "5000", "10")
        )
        self.ops = self.login(self.emails["OPERACIONES"])

    def nuevo_envio(self, peso="8000", volumen="20"):
        return self.env.crear(
            self.ops.sesion_id, EnvioDatos(self.cliente_sa.id, self.A.id, self.C.id, "Café", peso, volumen)
        )

    def ruta(self, envio_id, vehiculos=True, **cambios):
        """A -(camión)-> B -(barco)-> C, ambos tramos del operador 1."""
        t1 = TramoPlan(
            self.A.id,
            self.B.id,
            "TERRESTRE",
            self.operador_sa.id,
            h(0),
            h(5),
            self.camion.id if vehiculos else None,
            costo="1500.00",
            moneda_codigo="USD",
        )
        t2 = TramoPlan(
            self.B.id,
            self.C.id,
            "MARITIMO",
            self.operador_sa.id,
            h(6),
            h(80),
            self.barco.id if vehiculos else None,
            costo="9000.00",
            moneda_codigo="USD",
        )
        return self.env.definir_tramos(
            self.ops.sesion_id, envio_id, [cambios.get("t1", t1), cambios.get("t2", t2)]
        )

    def asignar(self, envio_id, contenedor, peso="8000", volumen="20", desde=-1, hasta=90):
        return self.env.asignar_contenedor(
            self.ops.sesion_id, envio_id, AsignacionDatos(contenedor.id, peso, volumen, h(desde), h(hasta))
        )

    def planificado(self):
        e = self.nuevo_envio()
        self.ruta(e.id)
        self.asignar(e.id, self.cont1)
        return self.env.planificar(self.ops.sesion_id, e.id)

    def outbox(self, envio_id):
        from sqlalchemy import text

        with self.conns.pg_session() as s:
            return s.execute(
                text(
                    "SELECT e.evento_tipo, e.agregado_version, array_agg(x.destino ORDER BY x.destino) AS "
                    "destinos,"
                    " e.payload FROM outbox_evento e JOIN outbox_entrega x ON x.evento_id = e.id"
                    " WHERE e.agregado_id = :id GROUP BY e.id ORDER BY e.agregado_version, e.creado_en"
                ),
                {"id": envio_id},
            ).all()


@pytest.fixture()
def g(tx_connections):
    return Logistica(tx_connections)


# --------------------------------------------------------------------------- catálogos
def test_vehiculo_solo_de_un_tercero_operador(g):
    with pytest.raises(BusinessRuleError, match="no es operador"):
        g.cat.crear_vehiculo(g.admin.sesion_id, VehiculoDatos(g.cliente_sa.id, "CAMION", "XX1"))


def test_identificadores_de_catalogo_repetidos_se_rechazan(g):
    with pytest.raises(ValidationError, match="contenedor duplicado"):
        g.cat.crear_contenedor(
            g.admin.sesion_id, ContenedorDatos(g.cont1.codigo_internacional, "SECO", "1", "1")
        )
    with pytest.raises(ValidationError, match="vehículo duplicado"):
        g.cat.crear_vehiculo(
            g.admin.sesion_id, VehiculoDatos(g.operador_sa.id, "CAMION", g.camion.identificacion)
        )


def test_catalogos_requieren_permiso_de_planificacion(g):
    comercial = g.login(g.emails["COMERCIAL"])
    with pytest.raises(PermissionDeniedError):
        g.cat.crear_ubicacion(comercial.sesion_id, UbicacionDatos("X", "OTRO", "AR", "Y"))
    cliente = g.login(g.emails["CLIENTE"])
    with pytest.raises(PermissionDeniedError):
        g.cat.listar_contenedores(cliente.sesion_id)


def test_sensor_no_se_instala_en_dos_contenedores_a_la_vez(g):
    s = g.ops.sesion_id
    sensor = g.cat.crear_sensor(
        s,
        SensorDatos(f"SEN-{g.sufijo}", "MULTISENSOR", "Acme", capacidades=frozenset({"TEMPERATURA", "GPS"})),
    )
    assert sensor.capacidades == frozenset({"TEMPERATURA", "GPS"})
    inst = g.cat.instalar_sensor(s, sensor.id, g.cont1.id, h(0))
    with pytest.raises(BusinessRuleError, match="instalación superpuesta"):  # superpuesto: sigue instalado en cont1
        g.cat.instalar_sensor(s, sensor.id, g.cont2.id, h(10))
    g.cat.retirar_sensor(s, inst.id, h(5))
    with pytest.raises(BusinessRuleError, match="instalación superpuesta"):  # se superpone con [h0, h5)
        g.cat.instalar_sensor(s, sensor.id, g.cont2.id, h(4))
    g.cat.instalar_sensor(s, sensor.id, g.cont2.id, h(5))  # [h5, ∞) no se superpone
    historial = g.cat.historial_sensor(s, sensor.id)
    assert [(i.contenedor_id, i.retirado_en is None) for i in historial] == [
        (g.cont2.id, True),
        (g.cont1.id, False),
    ]


def test_sensor_dado_de_baja_no_se_instala(g):
    s = g.ops.sesion_id
    sensor = g.cat.crear_sensor(
        s, SensorDatos(f"SEN-B{g.sufijo}", "GPS", "Acme", capacidades=frozenset({"GPS"}))
    )
    g.cat.set_estado_sensor(s, sensor.id, "BAJA")
    with pytest.raises(BusinessRuleError, match="sensor no habilitado"):
        g.cat.instalar_sensor(s, sensor.id, g.cont1.id, h(0))


# --------------------------------------------------------------------------- creación y tramos
def test_crear_envio_en_borrador_con_codigo_y_outbox(g):
    e = g.nuevo_envio()
    assert e.estado == "BORRADOR" and e.codigo.startswith("ENV-") and e.peso_kg == Decimal("8000.000")
    eventos = g.outbox(e.id)
    assert [(ev.evento_tipo, list(ev.destinos)) for ev in eventos] == [("ENVIO_CREADO", ["NEO4J_LOGISTICA"])]


def test_envio_solo_para_un_cliente_activo(g):
    with pytest.raises(BusinessRuleError, match="sin rol"):
        g.env.crear(g.ops.sesion_id, EnvioDatos(g.operador_sa.id, g.A.id, g.C.id, "x", "1", "1"))


def test_tramos_continuos_se_aceptan_y_los_discontinuos_no(g):
    e = g.nuevo_envio()
    det = g.ruta(e.id)
    assert [(t.orden, t.origen_id, t.destino_id) for t in det.tramos] == [
        (1, g.A.id, g.B.id),
        (2, g.B.id, g.C.id),
    ]
    roto = TramoPlan(g.A.id, g.C.id, "MARITIMO", g.operador_sa.id, h(6), h(80))  # sale de A, no de B
    with pytest.raises(BusinessRuleError, match="no continuos"):
        g.ruta(e.id, t2=roto)


def test_vehiculo_del_tramo_debe_ser_compatible(g):
    e = g.nuevo_envio()
    avion_para_barco = TramoPlan(g.B.id, g.C.id, "MARITIMO", g.operador_sa.id, h(6), h(80), g.avion.id)
    with pytest.raises(BusinessRuleError, match="otro operador"):
        g.ruta(e.id, t2=avion_para_barco)


def test_operador_del_tramo_debe_tener_rol_operador(g):
    e = g.nuevo_envio()
    malo = TramoPlan(g.B.id, g.C.id, "MARITIMO", g.cliente_sa.id, h(6), h(80))
    with pytest.raises(BusinessRuleError, match="sin rol"):
        g.ruta(e.id, t2=malo)


# --------------------------------------------------------------------------- contenedores
def test_capacidad_del_contenedor_se_respeta(g):
    e = g.nuevo_envio(peso="8000", volumen="20")
    with pytest.raises(BusinessRuleError, match="excede"):
        g.asignar(e.id, g.cont2, peso="6000", volumen="5")  # cont2: 5000 kg


def test_no_se_asigna_mas_carga_que_la_del_envio(g):
    e = g.nuevo_envio(peso="8000", volumen="20")
    g.asignar(e.id, g.cont1, peso="6000", volumen="15")
    with pytest.raises(BusinessRuleError, match="asignado > envío"):
        g.asignar(e.id, g.cont2, peso="2500", volumen="3")


def test_contenedor_no_se_reserva_en_periodos_superpuestos(g):
    e1, e2 = g.nuevo_envio(), g.nuevo_envio()
    g.asignar(e1.id, g.cont1, desde=0, hasta=50)
    with pytest.raises(BusinessRuleError, match="reservado"):
        g.asignar(e2.id, g.cont1, desde=40, hasta=60)
    g.asignar(e2.id, g.cont1, desde=50, hasta=60)  # [h50, h60) contigua, no superpuesta


def test_respaldo_de_la_base_si_falla_la_validacion_del_servicio(g):
    """Aunque el servicio no chequeara el solapamiento, la restricción EXCLUDE de PostgreSQL lo rechaza."""
    from sqlalchemy import text

    from logistica.core.errors import IntegrityError
    from logistica.repositories.postgres import unit_of_work

    e1, e2 = g.nuevo_envio(), g.nuevo_envio()
    g.asignar(e1.id, g.cont1, desde=0, hasta=50)
    with pytest.raises(IntegrityError, match="ex_envcont_reserva"):
        with unit_of_work(g.conns) as tx:
            tx.execute(
                text(
                    "INSERT INTO envio_contenedor (id, envio_id, contenedor_id, reserva_desde, reserva_hasta,"
                    " peso_asignado_kg, volumen_asignado_m3) VALUES (gen_random_uuid(), :e, :c, :d, :h, 1, 1)"
                ),
                {"e": e2.id, "c": g.cont1.id, "d": h(10), "h": h(20)},
            )


def test_contenedor_en_mantenimiento_no_se_asigna(g):
    g.cat.set_estado_contenedor(g.ops.sesion_id, g.cont2.id, "MANTENIMIENTO")
    e = g.nuevo_envio()
    with pytest.raises(BusinessRuleError, match="contenedor no habilitado"):
        g.asignar(e.id, g.cont2, peso="100", volumen="1")


# --------------------------------------------------------------------------- planificación
def test_planificar_exige_contenedor_y_fija_la_estimada(g):
    e = g.nuevo_envio()
    g.ruta(e.id)
    with pytest.raises(BusinessRuleError, match="contenedor"):
        g.env.planificar(g.ops.sesion_id, e.id)
    g.asignar(e.id, g.cont1)
    p = g.env.planificar(g.ops.sesion_id, e.id)
    assert p.estado == "PLANIFICADO" and p.entrega_estimada_en == h(80)
    assert p.entrega_comprometida_en is None  # el compromiso se fija al iniciar (SUPUESTO S3)


def test_planificar_sin_tramos_falla(g):
    e = g.nuevo_envio()
    g.asignar(e.id, g.cont1)
    with pytest.raises(BusinessRuleError, match="sin tramos"):
        g.env.planificar(g.ops.sesion_id, e.id)


def test_redefinir_tramos_de_un_planificado_lo_vuelve_a_borrador(g):
    p = g.planificado()
    det = g.ruta(p.id)
    assert det.envio.estado == "BORRADOR"


# --------------------------------------------------------------------------- ejecución
def test_iniciar_tramo_exige_vehiculo_y_orden(g):
    e = g.nuevo_envio()
    det = g.ruta(e.id, vehiculos=False)
    g.asignar(e.id, g.cont1)
    g.env.planificar(g.ops.sesion_id, e.id)
    t1, t2 = det.tramos
    with pytest.raises(BusinessRuleError, match="sin vehículo"):
        g.env.iniciar_tramo(g.ops.sesion_id, t1.id, h(0))
    g.env.asignar_vehiculo(g.ops.sesion_id, t1.id, g.camion.id)
    g.env.asignar_vehiculo(g.ops.sesion_id, t2.id, g.barco.id)
    with pytest.raises(BusinessRuleError, match="anterior"):
        g.env.iniciar_tramo(g.ops.sesion_id, t2.id, h(1))


def test_compromiso_de_entrega_se_fija_al_iniciar_y_no_cambia(g):
    from sqlalchemy import text

    from logistica.core.errors import IntegrityError
    from logistica.repositories.postgres import unit_of_work

    p = g.planificado()
    t1 = g.env.obtener(g.ops.sesion_id, p.id).tramos[0]
    g.env.iniciar_tramo(g.ops.sesion_id, t1.id, h(0.5))
    e = g.env.obtener(g.ops.sesion_id, p.id).envio
    assert e.estado == "EN_TRANSITO" and e.entrega_comprometida_en == h(80)
    # Ni el servicio ofrece cambiarlo ni la base lo permite (trigger trg_envio_compromiso_inmutable).
    with pytest.raises(IntegrityError, match="inmutable"):
        with unit_of_work(g.conns) as tx:
            tx.execute(
                text(
                    "UPDATE envio SET entrega_comprometida_en = entrega_comprometida_en + interval '1 day'"
                    " WHERE id = :id"
                ),
                {"id": p.id},
            )


def test_envio_en_transito_no_se_cancela_ni_se_replanifica(g):
    p = g.planificado()
    t1 = g.env.obtener(g.ops.sesion_id, p.id).tramos[0]
    g.env.iniciar_tramo(g.ops.sesion_id, t1.id, h(0.5))
    with pytest.raises(BusinessRuleError, match="transición"):
        g.env.cancelar(g.ops.sesion_id, p.id, "ya no")
    with pytest.raises(BusinessRuleError, match="no editable"):
        g.ruta(p.id)


def test_finalizar_tramo_llegada_no_anterior_a_salida(g):
    p = g.planificado()
    t1 = g.env.obtener(g.ops.sesion_id, p.id).tramos[0]
    g.env.iniciar_tramo(g.ops.sesion_id, t1.id, h(1))
    with pytest.raises(BusinessRuleError, match="llegada < salida"):
        g.env.finalizar_tramo(g.ops.sesion_id, t1.id, h(0.5))


def test_no_se_confirma_la_entrega_con_tramos_pendientes(g):
    p = g.planificado()
    t1 = g.env.obtener(g.ops.sesion_id, p.id).tramos[0]
    g.env.iniciar_tramo(g.ops.sesion_id, t1.id, h(0))
    g.env.finalizar_tramo(g.ops.sesion_id, t1.id, h(5))
    with pytest.raises(BusinessRuleError, match="sin finalizar"):
        g.env.confirmar_entrega(g.ops.sesion_id, p.id, h(6))


def test_cancelar_libera_los_contenedores(g):
    p = g.planificado()
    g.env.cancelar(g.ops.sesion_id, p.id, "el cliente desistió")
    det = g.env.obtener(g.ops.sesion_id, p.id)
    assert det.envio.estado == "CANCELADO"
    assert {a.estado for a in det.asignaciones} == {"CANCELADA"} and {t.estado for t in det.tramos} == {
        "CANCELADO"
    }
    otro = g.nuevo_envio()
    g.asignar(otro.id, g.cont1)  # el contenedor quedó libre en ese período


# --------------------------------------------------------------------------- de punta a punta
def test_envio_completo_de_punta_a_punta(g):
    s = g.ops.sesion_id
    e = g.nuevo_envio()
    det = g.ruta(e.id)
    g.asignar(e.id, g.cont1)
    g.env.planificar(s, e.id)
    t1, t2 = det.tramos
    g.env.iniciar_tramo(s, t1.id, h(0.5))
    g.env.registrar_evento(s, e.id, "CARGA_INICIADA", h(0.4), {}, tramo_id=t1.id)
    g.env.finalizar_tramo(s, t1.id, h(5.5))
    g.env.iniciar_tramo(s, t2.id, h(7))
    g.env.registrar_evento(
        s,
        e.id,
        "RETRASO",
        h(30),
        {"motivo": "Temporal en el Río de la Plata", "demora_estimada_min": 120},
        tramo_id=t2.id,
    )
    g.env.registrar_evento(s, e.id, "ADUANA", h(82), {"etapa": "LIBERACION"}, ubicacion_id=g.C.id)
    g.env.finalizar_tramo(s, t2.id, h(82))
    g.env.registrar_evento(s, e.id, "DESCARGA", h(83), {}, ubicacion_id=g.C.id)
    final = g.env.confirmar_entrega(s, e.id, h(84))

    assert final.estado == "ENTREGADO" and final.entrega_real_en == h(84)
    assert final.entrega_comprometida_en == h(80)  # se entregó 4 h tarde: el compromiso no se movió
    det = g.env.obtener(s, e.id)
    assert [t.estado for t in det.tramos] == ["FINALIZADO", "FINALIZADO"]
    a = det.asignaciones[0]
    assert (a.estado, a.uso_desde, a.uso_hasta) == ("FINALIZADA", h(0.5), h(84))

    eventos = g.outbox(e.id)
    tipos = [ev.evento_tipo for ev in eventos]
    assert tipos == [
        "ENVIO_CREADO",
        "TRAMOS_DEFINIDOS",
        "CONTENEDOR_ASIGNADO",
        "ENVIO_PLANIFICADO",
        "TRAMO_INICIADO",
        "EVENTO_REGISTRADO",
        "TRAMO_FINALIZADO",
        "TRAMO_INICIADO",
        "EVENTO_REGISTRADO",
        "EVENTO_REGISTRADO",
        "TRAMO_FINALIZADO",
        "EVENTO_REGISTRADO",
        "ENVIO_ENTREGADO",
    ]
    versiones = [ev.agregado_version for ev in eventos]
    assert versiones == sorted(versiones) and len(set(versiones)) == len(versiones)  # versión por agregado
    manuales = [ev for ev in eventos if ev.evento_tipo == "EVENTO_REGISTRADO"]
    assert all(list(ev.destinos) == ["MONGO_EVENTOS"] for ev in manuales)
    retraso = next(ev.payload for ev in manuales if ev.payload["tipo"] == "RETRASO")
    assert retraso["detalles"] == {"motivo": "Temporal en el Río de la Plata", "demora_estimada_min": 120}
    assert retraso["envio_id"] == str(e.id) and retraso["tramo_id"] == str(t2.id)


def test_evento_invalido_no_llega_a_la_outbox(g):
    p = g.planificado()
    antes = len(g.outbox(p.id))
    with pytest.raises(ValidationError, match="retraso sin motivo"):
        g.env.registrar_evento(g.ops.sesion_id, p.id, "RETRASO", h(1), {})
    with pytest.raises(ValidationError, match="no manual"):
        g.env.registrar_evento(g.ops.sesion_id, p.id, "ENTREGADO", h(1), {})  # lo genera el sistema
    assert len(g.outbox(p.id)) == antes


# --------------------------------------------------------------------------- alcance por perfil
def test_cliente_ve_solo_sus_envios_y_sin_costos(g):
    p = g.planificado()
    cliente = g.login(g.emails["CLIENTE"])
    assert p.id in {x.id for x in g.env.listar(cliente.sesion_id)}
    det = g.env.obtener(cliente.sesion_id, p.id)
    assert all(t.costo is None and t.moneda_codigo is None for t in det.tramos)
    with pytest.raises(PermissionDeniedError):
        g.env.crear(cliente.sesion_id, EnvioDatos(g.cliente_sa.id, g.A.id, g.C.id, "x", "1", "1"))


def test_cliente_no_ve_envios_de_otro_cliente(g):
    otro_cliente = g.terceros.crear(g.admin.sesion_id, g._tercero("Otro Cliente"), {"CLIENTE"})
    e = g.env.crear(g.ops.sesion_id, EnvioDatos(otro_cliente.id, g.A.id, g.C.id, "x", "1", "1"))
    cliente = g.login(g.emails["CLIENTE"])
    assert e.id not in {x.id for x in g.env.listar(cliente.sesion_id)}
    with pytest.raises(PermissionDeniedError):
        g.env.obtener(cliente.sesion_id, e.id)


def test_operador_solo_actualiza_sus_tramos(g):
    e = g.nuevo_envio()
    t2 = TramoPlan(
        g.B.id, g.C.id, "AEREO", g.operador2.id, h(6), h(9), g.avion.id, costo="700", moneda_codigo="USD"
    )
    det = g.ruta(e.id, t2=t2)
    g.asignar(e.id, g.cont1)
    g.env.planificar(g.ops.sesion_id, e.id)
    operador1 = g.login(g.emails["OPERADOR_LOGISTICO"])  # usuario de "Transportes SA" (operador 1)
    vista = g.env.obtener(operador1.sesion_id, e.id)
    assert [t.orden for t in vista.tramos] == [1]  # no ve el tramo del otro operador
    assert vista.tramos[0].costo == Decimal("1500.00")  # su propio costo sí
    g.env.iniciar_tramo(operador1.sesion_id, det.tramos[0].id, h(0))
    g.env.finalizar_tramo(operador1.sesion_id, det.tramos[0].id, h(5))
    with pytest.raises(PermissionDeniedError):
        g.env.iniciar_tramo(operador1.sesion_id, det.tramos[1].id, h(6))
    with pytest.raises(PermissionDeniedError):
        g.env.confirmar_entrega(operador1.sesion_id, e.id, h(10))  # la confirma Operaciones


def test_operador_no_ve_envios_sin_tramos_suyos(g):
    e = g.nuevo_envio()
    operador1 = g.login(g.emails["OPERADOR_LOGISTICO"])
    assert e.id not in {x.id for x in g.env.listar(operador1.sesion_id)}
    with pytest.raises(PermissionDeniedError):
        g.env.obtener(operador1.sesion_id, e.id)


def test_comercial_consulta_pero_no_planifica(g):
    p = g.planificado()
    comercial = g.login(g.emails["COMERCIAL"])
    assert g.env.obtener(comercial.sesion_id, p.id).envio.id == p.id
    with pytest.raises(PermissionDeniedError):
        g.env.cancelar(comercial.sesion_id, p.id, "x")


def test_envio_inexistente(g):
    with pytest.raises(NotFoundError):
        g.env.obtener(g.ops.sesion_id, uuid.uuid4())


def test_cada_cambio_queda_auditado(g):
    from logistica.services.auditoria_service import AuditoriaService

    p = g.planificado()
    acciones = {
        r.accion
        for r in AuditoriaService(g.conns, g.aut)
        .listar(g.admin.sesion_id, usuario_id=g.ops.usuario_id)
        .registros
    }
    assert {"crear_envio", "definir_tramos", "asignar_contenedor", "planificar_envio"} <= acciones
    assert p.id  # (el envío existe)


def test_alta_de_usuario_externo_no_afecta(g):
    """Sanidad: la fase 1 sigue funcionando con los datos de la fase 2."""
    u = g.usuarios.crear(
        g.admin.sesion_id,
        UsuarioDatos("Op", "Dos", f"op2-{g.sufijo}@test.local", g.operador2.id),
        {"OPERADOR_LOGISTICO"},
        CLAVE,
    )
    assert u.tercero_id == g.operador2.id
