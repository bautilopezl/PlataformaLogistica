"""Fase 7 — pantalla Reportes (Qt offscreen) con servicios reales sobre PostgreSQL (transacción revertida)."""

from __future__ import annotations

import os

import pytest

from logistica.ui import reportes_view
from logistica.ui.reportes_view import ReportesView
from logistica.ui.widgets import indice_dato
from tests.test_fase1_ui import esperar, qapp  # noqa: F401
from tests.test_fase6_ui import Serializado

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1", reason="requiere PostgreSQL local"
)


@pytest.fixture()
def r(tx_connections, tmp_path):
    from tests.test_fase7_reportes import Reportes

    return Reportes(tx_connections, tmp_path)


def abrir(qapp, r, rol):
    p = r.login(r.emails[rol])
    vista = ReportesView(Serializado(r.rep), Serializado(r.pred), Serializado(r.terceros), p)
    vista.recargar()
    assert esperar(qapp, lambda: not vista.ocupado()), vista.estado.text()
    return vista


def elegir(qapp, vista, codigo):
    vista.tipo.setCurrentIndex(vista.tipo.findData(codigo))
    assert vista.tipo.currentData() == codigo


def test_ver_exportar_y_abrir_un_reporte(qapp, r, monkeypatch):
    r.entregado(cargos=())
    vista = abrir(qapp, r, "OPERACIONES")
    elegir(qapp, vista, "ENVIOS_POR_ESTADO")
    vista.sin_rango.setChecked(True)
    vista.cliente.setCurrentIndex(indice_dato(vista.cliente, r.cliente_sa.id))
    vista.ver()
    assert esperar(qapp, lambda: not vista.ocupado()), vista.estado.text()
    assert vista.resultado.rowCount() == 1 and vista.resultado.item(0, 0).text() == "ENTREGADO"
    assert vista.grafico.isVisibleTo(vista)  # tiene columna numérica para graficar
    vista.exportar("CSV")
    assert esperar(qapp, lambda: not vista.ocupado() and vista.historial.rowCount() == 1), vista.estado.text()
    assert vista.historial.item(0, 3).text() == "COMPLETADO"
    abiertos = []
    monkeypatch.setattr(
        reportes_view.QDesktopServices, "openUrl", lambda url: abiertos.append(url.toLocalFile())
    )
    vista.historial.selectRow(0)
    vista.abrir()
    assert esperar(qapp, lambda: not vista.ocupado()) and abiertos and abiertos[0].endswith(".csv")
    vista.cerrar()


def test_el_cliente_no_ve_filtro_de_cliente_ni_reportes_internos(qapp, r):
    vista = abrir(qapp, r, "CLIENTE")
    codigos = [vista.tipo.itemData(i) for i in range(vista.tipo.count())]
    assert "COSTOS_POR_TRANSPORTE" not in codigos and "ENVIOS_POR_CLIENTE" not in codigos
    elegir(qapp, vista, "ENVIOS_POR_ESTADO")
    assert not vista.cliente.isVisibleTo(vista) and not vista.b_predecir.isVisibleTo(vista)
    vista.cerrar()


def test_rango_invertido_se_avisa_sin_llamar_al_servicio(qapp, r):
    vista = abrir(qapp, r, "OPERACIONES")
    elegir(qapp, vista, "ENVIOS_POR_ESTADO")
    vista.hasta.setDate(vista.desde.date().addDays(-1))
    vista.ver()
    assert not vista.ocupado() and vista.estado.property("estado") == "error"
    vista.cerrar()


def test_calcular_predicciones_desde_la_pantalla(qapp, r):
    r.planificado()
    vista = abrir(qapp, r, "OPERACIONES")
    assert vista.b_predecir.isVisibleTo(vista)
    vista.calcular_predicciones()
    assert esperar(qapp, lambda: not vista.ocupado()), vista.estado.text()
    assert "Predicciones de hoy" in vista.estado.text()
    elegir(qapp, vista, "PREDICCION_ATRASOS")
    vista.ver()
    assert esperar(qapp, lambda: not vista.ocupado()) and vista.resultado.rowCount() >= 1
    vista.cerrar()
