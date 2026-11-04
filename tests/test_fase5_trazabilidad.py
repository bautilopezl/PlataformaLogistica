"""Fase 5 — consultas de trazabilidad y límites de alcance entre motores.

PostgreSQL corre en la transacción revertida de ``tx_connections``; los nodos y documentos propios de cada
prueba se limpian al finalizar. Requiere LOGISTICA_TEST_CONNECTIONS=1 y los tres motores.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Iterator

import pytest
from sqlalchemy import text

from logistica.core.errors import (
    DatabaseUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    SessionExpiredError,
)
from logistica.domain.acceso import UsuarioDatos
from logistica.domain.logistica import EnvioDatos, SensorDatos, TramoPlan, UbicacionDatos
from logistica.domain.logistica_dtos import ContenedorDTO, EnvioDTO
from logistica.domain.trazabilidad_dtos import RedTrazabilidadDTO, RelacionRedDTO
from logistica.repositories.neo4j.proyeccion import ProyeccionRepository
from logistica.repositories.postgres.outbox import MONGO, NEO4J
from logistica.services.sincronizacion_service import SincronizacionService
from logistica.services.trazabilidad_service import TrazabilidadService
from tests.test_fase2_logistica import CLAVE, Logistica, h
from tests.test_fase3_sincronizacion import _limpiar

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y los tres motores levantados)",
)


@pytest.fixture(scope="module")
def qapp() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _esperar_ui(qapp: Any, condicion: Callable[[], bool], segundos: float = 10) -> bool:
    limite = time.monotonic() + segundos
    while not condicion() and time.monotonic() < limite:
        qapp.processEvents()
        time.sleep(0.01)
    return condicion()


@pytest.fixture()
def g(tx_connections: Any) -> Iterator[Logistica]:
    entorno = Logistica(tx_connections)
    entorno.envios_creados = []
    yield entorno
    ids = list(entorno.envios_creados)
    if ids:
        entorno.conns.mongo_db["lecturas_sensor"].delete_many({"envio_id": {"$in": ids}})
    _limpiar(entorno)


def _trazabilidad(g: Logistica) -> TrazabilidadService:
    return TrazabilidadService(g.conns, g.aut)


def _sincronizar(g: Logistica, destino: str, envio_id: uuid.UUID) -> None:
    resultado = SincronizacionService(g.conns).procesar(destino, agregado_id=envio_id)
    assert resultado.fallidos == 0


def _crear_ruta_dos_operadores(
    g: Logistica,
    *,
    cliente_id: uuid.UUID | None = None,
    contenedor: ContenedorDTO | None = None,
    desde: int = -1,
    hasta: int = 90,
    peso: str = "8000",
    volumen: str = "20",
) -> EnvioDTO:
    envio = g.env.crear(
        g.ops.sesion_id,
        EnvioDatos(cliente_id or g.cliente_sa.id, g.A.id, g.C.id, "Carga de prueba", peso, volumen),
    )
    g.envios_creados.append(envio.id)
    g.env.definir_tramos(
        g.ops.sesion_id,
        envio.id,
        [
            TramoPlan(
                g.A.id,
                g.B.id,
                "TERRESTRE",
                g.operador_sa.id,
                h(0),
                h(5),
                g.camion.id,
                costo="1500.00",
                moneda_codigo="USD",
            ),
            TramoPlan(
                g.B.id,
                g.C.id,
                "AEREO",
                g.operador2.id,
                h(6),
                h(80),
                g.avion.id,
                costo="9000.00",
                moneda_codigo="USD",
            ),
        ],
    )
    g.asignar(envio.id, contenedor or g.cont1, peso=peso, volumen=volumen, desde=desde, hasta=hasta)
    return g.env.planificar(g.ops.sesion_id, envio.id)


def _insertar_posicion(
    g: Logistica,
    *,
    envio_id: uuid.UUID,
    tramo_id: uuid.UUID,
    sensor_id: uuid.UUID,
    medicion: datetime,
) -> None:
    g.conns.mongo_db["lecturas_sensor"].insert_one(
        {
            "_id": uuid.uuid4(),
            "sensor_id": sensor_id,
            "contenedor_id": g.cont1.id,
            "envio_id": envio_id,
            "tramo_id": tramo_id,
            "fecha_medicion": medicion,
            "fecha_recepcion": medicion,
            "mediciones": {},
            "posicion": {"type": "Point", "coordinates": [-58.4, -34.6]},
            "origen": "SIMULADOR",
        }
    )


def test_eventos_ordenan_y_paginan_establemente_por_fecha_e_identificador(g):
    envio = g.nuevo_envio()
    g.envios_creados.append(envio.id)
    ocurrencia = h(1)
    ids = sorted(uuid.uuid4() for _ in range(5))
    g.conns.mongo_db["eventos_logisticos"].insert_many(
        [
            {
                "_id": event_id,
                "envio_id": envio.id,
                "tipo": "EN_TRANSITO",
                "fecha_ocurrencia": ocurrencia,
                "fecha_registro": h(2),
                "descripcion": f"Evento {indice}",
                "detalles": {"secuencia": indice},
            }
            for indice, event_id in enumerate(ids)
        ]
    )
    service = _trazabilidad(g)

    primera = service.eventos(g.ops.sesion_id, envio.id, limite=2)
    segunda = service.eventos(g.ops.sesion_id, envio.id, limite=2, despues=primera.siguiente)
    tercera = service.eventos(g.ops.sesion_id, envio.id, limite=2, despues=segunda.siguiente)

    assert [evento.id for pagina in (primera, segunda, tercera) for evento in pagina.eventos] == ids
    assert primera.siguiente == (ocurrencia, ids[1])
    assert tercera.siguiente is None
    assert all(evento.sincronizacion == "EN_MONGO_SIN_OUTBOX" for evento in primera.eventos)


def test_outbox_pendiente_no_es_historial_y_publicacion_no_duplica(g):
    envio = g.planificado()
    g.envios_creados.append(envio.id)
    tramo = g.env.obtener(g.ops.sesion_id, envio.id).tramos[0]
    g.env.registrar_evento(
        g.ops.sesion_id, envio.id, "ADUANA", h(1), {"etapa": "INGRESO"}, tramo_id=tramo.id
    )
    service = _trazabilidad(g)

    pendiente = service.eventos(g.ops.sesion_id, envio.id)
    assert pendiente.eventos == []
    assert pendiente.pendientes_mongo == 1

    _sincronizar(g, MONGO, envio.id)
    publicado = service.eventos(g.ops.sesion_id, envio.id)
    assert len(publicado.eventos) == 1
    assert publicado.eventos[0].tipo == "ADUANA"
    assert publicado.eventos[0].sincronizacion == "EN_MONGO · OUTBOX_COMPLETADA"
    assert publicado.pendientes_mongo == 0
    _sincronizar(g, MONGO, envio.id)
    assert len(service.eventos(g.ops.sesion_id, envio.id).eventos) == 1


def test_ruta_neo4j_multitramos_d6_desfase_caida_y_recuperacion(g, monkeypatch):
    envio = _crear_ruta_dos_operadores(g)
    _sincronizar(g, NEO4J, envio.id)
    nombre_actual = f"Depósito renombrado {g.sufijo}"
    g.cat.actualizar_ubicacion(
        g.admin.sesion_id,
        g.A.id,
        UbicacionDatos(
            nombre_actual,
            g.A.tipo,
            g.A.pais_codigo,
            g.A.ciudad,
            g.A.direccion,
            g.A.latitud,
            g.A.longitud,
        ),
    )
    service = _trazabilidad(g)

    ruta = service.ruta(g.ops.sesion_id, envio.id)
    assert ruta.estado_proyeccion == "ACTUALIZADA"
    assert ruta.version_proyectada == ruta.version_operativa
    assert [(tramo.orden, tramo.operador_id) for tramo in ruta.tramos] == [
        (1, g.operador_sa.id),
        (2, g.operador2.id),
    ]
    assert ruta.tramos[0].origen_nombre == nombre_actual
    assert "decisión D6" in ruta.nota_catalogo

    primer_tramo = g.env.obtener(g.ops.sesion_id, envio.id).tramos[0]
    g.env.iniciar_tramo(g.ops.sesion_id, primer_tramo.id, h(1))
    atrasada = service.ruta(g.ops.sesion_id, envio.id)
    assert atrasada.estado_proyeccion == "ATRASADA"
    assert atrasada.tramos[0].estado == "PENDIENTE"

    _sincronizar(g, NEO4J, envio.id)
    recuperada = service.ruta(g.ops.sesion_id, envio.id)
    assert recuperada.estado_proyeccion == "ACTUALIZADA"
    assert recuperada.tramos[0].estado == "EN_CURSO"
    assert len(recuperada.tramos) == 2
    _sincronizar(g, NEO4J, envio.id)
    assert len(service.ruta(g.ops.sesion_id, envio.id).tramos) == 2

    original = ProyeccionRepository.ruta

    def neo4j_no_disponible(self, envio_id, **kwargs):
        assert isinstance(self, ProyeccionRepository)
        raise DatabaseUnavailableError("neo4j", detail=f"{envio_id} {kwargs.get('limite')}")

    monkeypatch.setattr(ProyeccionRepository, "ruta", neo4j_no_disponible)
    sin_grafo = service.ruta(g.ops.sesion_id, envio.id)
    assert sin_grafo.estado_proyeccion == "NO_DISPONIBLE"
    assert sin_grafo.tramos == []
    monkeypatch.setattr(ProyeccionRepository, "ruta", original)
    assert service.ruta(g.ops.sesion_id, envio.id).estado_proyeccion == "ACTUALIZADA"


def test_contenedor_reutilizado_separa_asignaciones_rutas_y_gps_por_uso(g):
    primero = g.planificado()
    g.envios_creados.append(primero.id)
    t1, t2 = g.env.obtener(g.ops.sesion_id, primero.id).tramos
    g.env.iniciar_tramo(g.ops.sesion_id, t1.id, h(1))
    g.env.finalizar_tramo(g.ops.sesion_id, t1.id, h(5))
    g.env.iniciar_tramo(g.ops.sesion_id, t2.id, h(6))
    g.env.finalizar_tramo(g.ops.sesion_id, t2.id, h(80))
    g.env.confirmar_entrega(g.ops.sesion_id, primero.id, h(81))

    segundo = g.nuevo_envio()
    g.envios_creados.append(segundo.id)
    g.ruta(
        segundo.id,
        t1=TramoPlan(
            g.A.id, g.B.id, "TERRESTRE", g.operador_sa.id, h(100), h(105), g.camion.id,
            costo="1500.00", moneda_codigo="USD",
        ),
        t2=TramoPlan(
            g.B.id, g.C.id, "MARITIMO", g.operador_sa.id, h(106), h(170), g.barco.id,
            costo="9000.00", moneda_codigo="USD",
        ),
    )
    g.asignar(segundo.id, g.cont1, desde=99, hasta=190)
    segundo = g.env.planificar(g.ops.sesion_id, segundo.id)
    u1, u2 = g.env.obtener(g.ops.sesion_id, segundo.id).tramos
    g.env.iniciar_tramo(g.ops.sesion_id, u1.id, h(101))
    g.env.finalizar_tramo(g.ops.sesion_id, u1.id, h(105))
    g.env.iniciar_tramo(g.ops.sesion_id, u2.id, h(106))
    g.env.finalizar_tramo(g.ops.sesion_id, u2.id, h(170))
    g.env.confirmar_entrega(g.ops.sesion_id, segundo.id, h(171))

    sensor = g.cat.crear_sensor(
        g.ops.sesion_id,
        SensorDatos(f"GPS-TRAZA-{g.sufijo}", "GPS", "Acme", capacidades=frozenset({"GPS"})),
    )
    g.cat.instalar_sensor(g.ops.sesion_id, sensor.id, g.cont1.id, h(-2))
    _insertar_posicion(g, envio_id=primero.id, tramo_id=t1.id, sensor_id=sensor.id, medicion=h(2))
    _insertar_posicion(g, envio_id=primero.id, tramo_id=t1.id, sensor_id=sensor.id, medicion=h(100))
    _insertar_posicion(g, envio_id=segundo.id, tramo_id=u1.id, sensor_id=sensor.id, medicion=h(102))
    _insertar_posicion(g, envio_id=segundo.id, tramo_id=u1.id, sensor_id=sensor.id, medicion=h(50))

    service = _trazabilidad(g)
    recorrido = service.recorrido_contenedor(g.admin.sesion_id, g.cont1.codigo_internacional)
    por_envio = {asignacion.envio_id: asignacion for asignacion in recorrido.asignaciones}
    assert set(por_envio) == {primero.id, segundo.id}
    assert {tramo.id for tramo in por_envio[primero.id].tramos} == {t1.id, t2.id}
    assert {tramo.id for tramo in por_envio[segundo.id].tramos} == {u1.id, u2.id}
    assert all(
        tramo.realizado_durante_uso
        for asignacion in por_envio.values()
        for tramo in asignacion.tramos
    )
    gps_primero = service.posiciones_gps(
        g.admin.sesion_id, g.cont1.id, por_envio[primero.id].id
    )
    gps_segundo = service.posiciones_gps(
        g.admin.sesion_id, g.cont1.id, por_envio[segundo.id].id
    )
    assert [posicion.fecha_medicion for posicion in gps_primero] == [h(2)]
    assert [posicion.fecha_medicion for posicion in gps_segundo] == [h(102)]


def test_red_y_recorrido_respetan_empresa_y_operador(g):
    otro_cliente = g.terceros.crear(
        g.admin.sesion_id, g._tercero("Cliente aislado"), {"CLIENTE"}
    )
    segundo_operador_email = f"operador2-{g.sufijo}@test.local"
    g.usuarios.crear(
        g.admin.sesion_id,
        UsuarioDatos("Operador", "Dos", segundo_operador_email, g.operador2.id),
        {"OPERADOR_LOGISTICO"},
        CLAVE,
    )
    envio_a = _crear_ruta_dos_operadores(g, contenedor=g.cont1)
    envio_b = g.env.crear(
        g.ops.sesion_id,
        EnvioDatos(otro_cliente.id, g.A.id, g.C.id, "Carga aislada", "1000", "2"),
    )
    g.envios_creados.append(envio_b.id)
    g.env.definir_tramos(
        g.ops.sesion_id,
        envio_b.id,
        [
            TramoPlan(g.A.id, g.B.id, "AEREO", g.operador2.id, h(90), h(95), g.avion.id),
            TramoPlan(g.B.id, g.C.id, "AEREO", g.operador2.id, h(96), h(100), g.avion.id),
        ],
    )
    g.asignar(envio_b.id, g.cont2, peso="1000", volumen="2", desde=89, hasta=110)
    g.env.planificar(g.ops.sesion_id, envio_b.id)
    _sincronizar(g, NEO4J, envio_a.id)
    _sincronizar(g, NEO4J, envio_b.id)

    service = _trazabilidad(g)
    cliente = g.login(g.emails["CLIENTE"])
    operador_uno = g.login(g.emails["OPERADOR_LOGISTICO"])
    operador_dos = g.login(segundo_operador_email)
    red_cliente = service.red(cliente.sesion_id)
    red_operador_uno = service.red(operador_uno.sesion_id)
    red_operador_dos = service.red(operador_dos.sesion_id)
    assert red_cliente.estado_grafo == "ACTUALIZADA"
    assert {rel.envio_id for rel in red_cliente.relaciones} == {envio_a.id}
    assert {rel.operador_id for rel in red_operador_uno.relaciones} == {g.operador_sa.id}
    assert {rel.envio_id for rel in red_operador_uno.relaciones} == {envio_a.id}
    assert {rel.operador_id for rel in red_operador_dos.relaciones} == {g.operador2.id}
    assert {rel.envio_id for rel in red_operador_dos.relaciones} == {envio_a.id, envio_b.id}
    with pytest.raises(PermissionDeniedError):
        service.ruta(cliente.sesion_id, envio_b.id)
    with pytest.raises(NotFoundError):
        service.recorrido_contenedor(cliente.sesion_id, g.cont2.codigo_internacional)


def test_red_resuelve_nombres_actuales_de_cliente_y_operador_incluso_con_roles_compartidos(g):
    envio = _crear_ruta_dos_operadores(g)
    _sincronizar(g, NEO4J, envio.id)
    service = _trazabilidad(g)

    red = service.red(g.admin.sesion_id, envio_id=envio.id)
    assert red.estado_grafo == "ACTUALIZADA"
    assert {
        relacion.cliente_razon_social for relacion in red.relaciones
    } == {g.cliente_sa.razon_social}
    assert {
        relacion.operador_razon_social for relacion in red.relaciones
    } == {g.operador_sa.razon_social, g.operador2.razon_social}

    g.terceros.set_roles_comerciales(
        g.admin.sesion_id, g.operador_sa.id, {"CLIENTE", "OPERADOR"}
    )
    envio_doble_rol = _crear_ruta_dos_operadores(
        g,
        cliente_id=g.operador_sa.id,
        contenedor=g.cont2,
        peso="1000",
        volumen="2",
    )
    _sincronizar(g, NEO4J, envio_doble_rol.id)
    red_doble_rol = service.red(g.admin.sesion_id, envio_id=envio_doble_rol.id)
    relacion_compartida = next(
        relacion
        for relacion in red_doble_rol.relaciones
        if relacion.cliente_id == g.operador_sa.id == relacion.operador_id
    )
    assert relacion_compartida.cliente_razon_social == g.operador_sa.razon_social
    assert relacion_compartida.operador_razon_social == g.operador_sa.razon_social


@pytest.mark.parametrize("cantidad", [4, 100])
def test_grafico_red_conserva_legibilidad_y_navegacion(qapp, g, cantidad):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsTextItem, QGraphicsView

    from logistica.ui.trazabilidad_view import TrazabilidadView

    cliente_ids = [uuid.uuid4() for _ in range(1 if cantidad == 4 else cantidad)]
    operador_ids = [uuid.uuid4() for _ in range(2 if cantidad == 4 else cantidad)]
    relaciones = [
        RelacionRedDTO(
            cliente_id=cliente_ids[i if cantidad > 4 else 0],
            cliente_razon_social=f"Cliente de prueba {i if cantidad > 4 else 1}",
            envio_id=uuid.uuid4(),
            envio_codigo=f"ENV-GRAFICO-{i:03}",
            envio_version_proyectada=8,
            tramo_id=uuid.uuid4(),
            tramo_orden=1,
            operador_id=operador_ids[i if cantidad > 4 else i % 2],
            operador_razon_social=f"Operador de prueba {i if cantidad > 4 else i % 2 + 1}",
        )
        for i in range(cantidad)
    ]
    vista = TrazabilidadView(_trazabilidad(g), g.env, g.ops.sesion_id)
    vista.resize(1100, 700)
    vista.secciones.setCurrentIndex(3)
    vista._mostrar_red(RedTrazabilidadDTO("ACTUALIZADA", relaciones, False, False, 0))
    vista.show()
    qapp.processEvents()
    viewport = vista.grafo_red.viewport()
    punto = QPointF(viewport.rect().center())
    evento_zoom = QWheelEvent(
        punto,
        QPointF(viewport.mapToGlobal(punto.toPoint())),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    qapp.sendEvent(viewport, evento_zoom)

    textos = {
        item.toPlainText()
        for item in vista.escena_red.items()
        if isinstance(item, QGraphicsTextItem)
    }
    assert f"ENV-GRAFICO-{cantidad - 1:03}" in textos
    assert f"Cliente de prueba {cantidad - 1 if cantidad > 4 else 1}" in textos
    assert f"Operador de prueba {cantidad - 1 if cantidad > 4 else 2}" in textos
    assert sum(isinstance(item, QGraphicsRectItem) for item in vista.escena_red.items()) == (
        cantidad * 3 if cantidad > 4 else 7
    )
    assert vista.grafo_red.transform().m11() == pytest.approx(1.15)
    assert vista.grafo_red.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
    if cantidad == 100:
        assert vista.grafo_red.verticalScrollBar().maximum() > 0

    vista.close()
    vista.deleteLater()
    qapp.processEvents()


def test_ruta_descarta_resultado_si_cambia_el_alcance_durante_neo4j(g, monkeypatch):
    envio = _crear_ruta_dos_operadores(g)
    _sincronizar(g, NEO4J, envio.id)
    operador = g.login(g.emails["OPERADOR_LOGISTICO"])
    original = ProyeccionRepository.ruta

    def cambiar_operador_tras_leer(self, envio_id, **kwargs):
        proyeccion = original(self, envio_id, **kwargs)
        tramo = g.env.obtener(g.ops.sesion_id, envio_id).tramos[0]
        with g.conns.pg_session() as tx:
            tx.execute(
                text("UPDATE tramo SET operador_id = :operador, vehiculo_id = NULL WHERE id = :tramo"),
                {"operador": g.operador2.id, "tramo": tramo.id},
            )
        return proyeccion

    monkeypatch.setattr(ProyeccionRepository, "ruta", cambiar_operador_tras_leer)
    with pytest.raises(PermissionDeniedError, match="fuera del alcance"):
        _trazabilidad(g).ruta(operador.sesion_id, envio.id)


def test_ruta_descarta_resultado_si_revocan_sesion_durante_neo4j(g, monkeypatch):
    envio = _crear_ruta_dos_operadores(g)
    _sincronizar(g, NEO4J, envio.id)
    original = ProyeccionRepository.ruta

    def revocar_durante_consulta(self, envio_id, **kwargs):
        proyeccion = original(self, envio_id, **kwargs)
        with g.conns.pg_session() as tx:
            tx.execute(
                text("UPDATE sesion SET estado = 'REVOCADA', fin_en = clock_timestamp() WHERE id = :id"),
                {"id": g.ops.sesion_id},
            )
        return proyeccion

    monkeypatch.setattr(ProyeccionRepository, "ruta", revocar_durante_consulta)
    with pytest.raises(SessionExpiredError):
        _trazabilidad(g).ruta(g.ops.sesion_id, envio.id)


def test_pantalla_trazabilidad_consulta_los_cuatro_recorridos(qapp, g):
    from PySide6.QtCore import QCoreApplication, QEvent

    envio = g.planificado()
    g.envios_creados.append(envio.id)
    tramos = g.env.obtener(g.ops.sesion_id, envio.id).tramos
    g.env.iniciar_tramo(g.ops.sesion_id, tramos[0].id, h(1))
    g.env.finalizar_tramo(g.ops.sesion_id, tramos[0].id, h(5))
    g.env.iniciar_tramo(g.ops.sesion_id, tramos[1].id, h(6))
    g.env.finalizar_tramo(g.ops.sesion_id, tramos[1].id, h(80))
    g.env.confirmar_entrega(g.ops.sesion_id, envio.id, h(81))
    _sincronizar(g, MONGO, envio.id)
    _sincronizar(g, NEO4J, envio.id)

    from logistica.ui.trazabilidad_view import TrazabilidadView

    vista = TrazabilidadView(_trazabilidad(g), g.env, g.ops.sesion_id)
    vista.selector_envio.blockSignals(True)
    vista.selector_envio.addItem(envio.codigo, envio.id)
    vista.selector_envio.blockSignals(False)

    vista._buscar_eventos()
    assert _esperar_ui(qapp, lambda: not vista.ocupado())
    assert vista.tabla_eventos.rowCount() == 5
    vista._consultar_ruta()
    assert _esperar_ui(qapp, lambda: not vista.ocupado())
    assert vista.tabla_ruta.rowCount() == 2
    assert "Neo4j: ACTUALIZADA" in vista.estado_ruta.text()

    vista.codigo_contenedor.setText(g.cont1.codigo_internacional)
    vista._buscar_contenedor()
    assert _esperar_ui(qapp, lambda: not vista.ocupado())
    assert vista.tabla_asignaciones.rowCount() == 1
    vista.tabla_asignaciones.selectRow(0)
    assert vista.tabla_tramos_contenedor.rowCount() == 2
    vista._cargar_gps()
    assert _esperar_ui(qapp, lambda: not vista.ocupado())
    assert vista.tabla_gps.rowCount() == 0

    vista.filtro_envio_red.setText(str(envio.id))
    vista._consultar_red()
    assert _esperar_ui(qapp, lambda: not vista.ocupado())
    assert vista.tabla_red.rowCount() == 2
    vista.cerrar()
    vista.close()
    vista.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
