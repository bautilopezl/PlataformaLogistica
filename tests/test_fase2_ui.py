"""Fase 2 — UI de logística (offscreen): botones según estado y permisos, y un caso de punta a punta real."""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from logistica.domain.logistica_dtos import EnvioDetalleDTO, EnvioDTO, TramoDTO  # noqa: E402

T0 = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def esperar(qapp, condicion, segundos=10.0):
    limite = time.monotonic() + segundos
    while not condicion() and time.monotonic() < limite:
        qapp.processEvents()
        time.sleep(0.01)
    return condicion()


def envio(estado="BORRADOR"):
    return EnvioDTO(
        uuid.uuid4(),
        "ENV-20261201-ABCDEF",
        uuid.uuid4(),
        "Cliente SA",
        uuid.uuid4(),
        "Rosario",
        uuid.uuid4(),
        "Santos",
        "Café",
        estado,
        "NORMAL",
        Decimal("8000"),
        Decimal("20"),
        T0,
        None,
        None,
        None,
        1,
    )


def tramo(estado="PENDIENTE", orden=1, costo=None):
    return TramoDTO(
        uuid.uuid4(),
        uuid.uuid4(),
        orden,
        uuid.uuid4(),
        "A",
        uuid.uuid4(),
        "B",
        "TERRESTRE",
        uuid.uuid4(),
        "Transportes SA",
        uuid.uuid4(),
        "AA123",
        estado,
        T0,
        T0 + timedelta(hours=5),
        None,
        None,
        costo,
        "USD" if costo else None,
    )


class FakeEnvios:
    def __init__(self, det):
        self.det, self.llamadas = det, []

    def obtener(self, sesion_id, envio_id):
        return self.det

    def eventos(self, sesion_id, envio_id):
        return []

    def planificar(self, sesion_id, envio_id):
        self.llamadas.append("planificar")
        return self.det.envio

    def listar(self, sesion_id, **filtros):
        self.llamadas.append(filtros)
        return [self.det.envio]


def detalle_view(det, permisos):
    from logistica.ui.envios_view import EnvioDetalleView

    fake = FakeEnvios(det)
    return EnvioDetalleView(fake, object(), object(), uuid.uuid4(), det.envio.id, frozenset(permisos)), fake


def test_botones_segun_estado_del_envio(qapp):
    planificar = {"envios.planificar", "tramos.actualizar"}
    vista, _ = detalle_view(EnvioDetalleDTO(envio("BORRADOR"), [tramo()]), planificar)
    vista.recargar()
    assert esperar(qapp, lambda: vista.det is not None and not vista.ocupado())
    b = vista.botones
    assert b["tramos"].isEnabled() and b["planificar"].isEnabled() and b["cancelar"].isEnabled()
    assert not b["entregar"].isEnabled() and not b["evento"].isEnabled()

    vista, _ = detalle_view(EnvioDetalleDTO(envio("EN_TRANSITO"), [tramo("EN_CURSO")]), planificar)
    vista.recargar()
    assert esperar(qapp, lambda: vista.det is not None and not vista.ocupado())
    vista.tramos.selectRow(0)
    b = vista.botones
    assert not b["tramos"].isEnabled() and not b["cancelar"].isEnabled()  # SUPUESTO S8
    assert b["finalizar"].isEnabled() and b["evento"].isEnabled() and b["entregar"].isEnabled()


def test_operador_no_ve_botones_de_planificacion(qapp):
    vista, _ = detalle_view(
        EnvioDetalleDTO(envio("PLANIFICADO"), [tramo(costo=Decimal("100"))]),
        {"tramos.actualizar", "envios.consultar"},
    )
    b = vista.botones
    assert b["iniciar"].isVisibleTo(vista) and b["evento"].isVisibleTo(vista)
    for oculto in ("tramos", "contenedor", "planificar", "entregar", "cancelar", "vehiculo"):
        assert not b[oculto].isVisibleTo(vista), oculto


def test_planificar_desde_el_detalle_llama_al_servicio_y_recarga(qapp):
    vista, fake = detalle_view(EnvioDetalleDTO(envio("BORRADOR"), [tramo()]), {"envios.planificar"})
    vista.recargar()
    assert esperar(qapp, lambda: vista.det is not None and not vista.ocupado())
    vista.planificar()
    assert esperar(qapp, lambda: "planificar" in fake.llamadas and not vista.ocupado())
    assert vista.tramos.rowCount() == 1 and vista.cabecera.text().startswith("ENV-")


def test_filtros_del_listado_se_leen_en_el_hilo_grafico(qapp):
    from logistica.ui.envios_view import EnviosView

    fake = FakeEnvios(EnvioDetalleDTO(envio()))
    vista = EnviosView(fake, object(), object(), uuid.uuid4(), frozenset({"envios.consultar"}))
    assert vista.boton_nuevo.isHidden()  # sin envios.crear
    vista.buscar.setText("ENV-2026")
    vista.filtro_estado.setCurrentIndex(vista.filtro_estado.findData("EN_TRANSITO"))
    vista.recargar()
    vista.buscar.setText("cambiado")
    assert esperar(qapp, lambda: not vista.ocupado())
    assert fake.llamadas[-1] == {"estado": "EN_TRANSITO", "texto": "ENV-2026"}
    assert vista.tabla.rowCount() == 1


def test_formulario_generico_valida_con_el_dominio(qapp):
    from logistica.domain.logistica import ContenedorDatos
    from logistica.ui.widgets import Campo, FormularioDialog

    campos = [Campo("codigo", "Código", obligatorio=True), Campo("kg", "Capacidad (kg)", obligatorio=True)]
    dialogo = FormularioDialog(
        None, "x", campos, lambda v: ContenedorDatos(v["codigo"], "SECO", v["kg"], "1").normalizado()
    )
    dialogo._aceptar()
    assert "Complete" in dialogo.error.text()
    dialogo.widget("codigo").setText("MSKU1")
    dialogo.widget("kg").setText("-5")
    dialogo._aceptar()
    assert "mayor que cero" in dialogo.error.text()


def test_fecha_del_formulario_tiene_zona_horaria(qapp):
    from logistica.ui.widgets import Campo, FormularioDialog

    dialogo = FormularioDialog(None, "x", [Campo("f", "Fecha", "fecha_hora", valor=T0)])
    valor = dialogo.valores()["f"]
    assert valor.tzinfo is not None and valor == T0  # se muestra en hora local, pero es el mismo instante


# --------------------------------------------------------------------------- de punta a punta (servicios
# reales)
@pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y PostgreSQL levantado)",
)
def test_de_punta_a_punta_planificar_desde_la_ui(qapp, tx_connections):
    from logistica.ui.envios_view import EnvioDetalleView
    from tests.test_fase2_logistica import Logistica

    g = Logistica(tx_connections)
    e = g.nuevo_envio()
    g.ruta(e.id)
    g.asignar(e.id, g.cont1)
    vista = EnvioDetalleView(g.env, g.cat, g.terceros, g.ops.sesion_id, e.id, g.ops.permisos)
    vista.recargar()
    assert esperar(qapp, lambda: vista.det is not None and not vista.ocupado())
    assert vista.tramos.rowCount() == 2 and vista.asignaciones.rowCount() == 1
    assert vista.botones["planificar"].isEnabled()
    vista.planificar()
    assert esperar(qapp, lambda: not vista.ocupado() and vista.det.envio.estado == "PLANIFICADO", 20)
    assert "Planificado" in vista.cabecera.text()
    assert vista.eventos.rowCount() == 4  # creado, tramos, contenedor, planificado
    vista.cerrar()


# --------------------------------------------------------------------------- regresiones de ciclo de vida
# (Qt)
def test_soltar_una_vista_justo_despues_de_usarla_no_aborta(qapp):
    """Antes, un deleteLater pendiente de la llamada + destrucción inmediata de la vista abortaba la app."""
    vista, _ = detalle_view(EnvioDetalleDTO(envio("BORRADOR"), [tramo()]), {"envios.planificar"})
    vista.recargar()
    assert esperar(qapp, lambda: vista.det is not None and not vista.ocupado())
    import shiboken6

    shiboken6.delete(vista)  # destrucción inmediata del objeto C++ (como cuando Python suelta una vista)
    assert not shiboken6.isValid(vista)
    esperar(qapp, lambda: False, 0.3)  # si abortara, el proceso de pytest moriría acá


def test_resultado_que_llega_despues_de_cerrar_la_vista_se_descarta(qapp):
    from logistica.ui.envios_view import EnvioDetalleView

    class Lento(FakeEnvios):
        def obtener(self, sesion_id, envio_id):
            time.sleep(0.3)
            return self.det

    fake = Lento(EnvioDetalleDTO(envio("BORRADOR"), [tramo()]))
    vista = EnvioDetalleView(fake, object(), object(), uuid.uuid4(), fake.det.envio.id, frozenset())
    import shiboken6

    vista.recargar()
    shiboken6.delete(vista)  # la vista desaparece antes de que llegue el resultado
    assert not shiboken6.isValid(vista)
    esperar(qapp, lambda: False, 0.8)  # el resultado llega a una vista inexistente: no debe fallar
