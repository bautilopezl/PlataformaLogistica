"""Fase 6 — pantalla Comercial (Qt offscreen) con servicios reales sobre PostgreSQL (transacción revertida).

`tx_connections` usa UNA conexión para poder revertir todo al final; la vista lanza varios trabajadores a la
vez y, sobre una sola conexión, sus savepoints se entrelazarían. `Serializado` hace que esas llamadas reales
se ejecuten de a una (en la app cada trabajador tiene su propia sesión del pool, así que no hace falta).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from logistica.ui import comercial_view
from logistica.ui.comercial_view import ComercialView
from tests.test_fase1_ui import _permisos_sembrados, esperar, principal, qapp  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1", reason="requiere PostgreSQL local"
)


@pytest.fixture()
def c(tx_connections):
    from tests.test_fase6_comercial import Comercial

    return Comercial(tx_connections)


class Serializado:
    _lock = threading.Lock()

    def __init__(self, servicio):
        self._s = servicio

    def __getattr__(self, nombre):
        metodo = getattr(self._s, nombre)

        def llamar(*args, **kwargs):
            with Serializado._lock:
                return metodo(*args, **kwargs)

        return llamar


def abrir(qapp, c, email):
    p = c.login(c.emails[email])
    vista = ComercialView(Serializado(c.com), Serializado(c.env), Serializado(c.terceros), p)
    vista.recargar()
    assert esperar(qapp, lambda: not vista.ocupado()), vista.estado.text()
    return vista


def fila(tabla, columna, texto):
    for i in range(tabla.rowCount()):
        if tabla.item(i, columna).text() == texto:
            return i
    raise AssertionError(f"{texto!r} no está en la tabla")


def test_la_factura_automatica_se_ve_y_se_anula_con_refacturacion(qapp, c, monkeypatch):
    p = c.entregado()
    vista = abrir(qapp, c, "COMERCIAL")
    assert vista.tabla_facturas.rowCount() == 1, vista.estado.text()
    assert (
        vista.tabla_facturas.item(0, 6).text() == "12,000.00"
        and vista.tabla_facturas.item(0, 9).text() == "EMITIDA"
    )
    vista.tabla_facturas.selectRow(0)
    assert vista.b_anular.isEnabled()
    monkeypatch.setattr(
        comercial_view, "pedir", lambda *a, **k: {"motivo": "Precio mal cargado", "refacturar": True}
    )
    vista.anular_factura()
    assert esperar(qapp, lambda: not vista.ocupado()), vista.estado.text()
    assert vista.tabla_facturas.item(0, 9).text() == "ANULADA"
    vista.seleccionar_envio(p.id)
    assert esperar(qapp, lambda: not vista.ocupado())
    estados = sorted(vista.tabla_cargos.item(i, 5).text() for i in range(vista.tabla_cargos.rowCount()))
    assert estados == ["CANCELADO", "PENDIENTE"]  # original atado a la anulada + reemplazo
    vista.facturar_pendientes()
    assert esperar(qapp, lambda: not vista.ocupado()), vista.estado.text()
    assert vista.tabla_facturas.rowCount() == 2
    vista.cerrar()


def test_registrar_y_aplicar_un_cobro_desde_la_pantalla(qapp, c, monkeypatch):
    c.entregado()
    vista = abrir(qapp, c, "COMERCIAL")
    datos = {
        "tercero": c.cliente_sa.id,
        "sentido": "COBRO",
        "fecha": datetime.now(timezone.utc),
        "monto": "5000",
        "moneda": "USD",
        "medio": "TRANSFERENCIA",
        "referencia": "TRF-1",
    }
    monkeypatch.setattr(comercial_view, "pedir", lambda *a, **k: datos)
    vista.registrar_pago()
    assert esperar(qapp, lambda: not vista.ocupado() and vista.tabla_pagos.rowCount() == 1), (
        vista.estado.text()
    )
    assert vista._clave_pago is None  # se registró: la próxima carga usa otra clave
    vista.tabla_pagos.selectRow(0)
    [factura_id] = list(vista._facturas)
    monkeypatch.setattr(comercial_view, "pedir", lambda *a, **k: {"factura": factura_id, "monto": "5000"})
    vista.aplicar_pago()
    assert esperar(qapp, lambda: not vista.ocupado()), vista.estado.text()
    assert vista.tabla_facturas.item(0, 8).text() == "7,000.00"  # pendiente
    vista.tabla_cuentas.selectRow(fila(vista.tabla_cuentas, 2, "USD"))
    assert esperar(qapp, lambda: not vista.ocupado() and vista.tabla_movimientos.rowCount() == 2)
    assert vista.tabla_cuentas.item(fila(vista.tabla_cuentas, 2, "USD"), 3).text() == "7,000.00"
    vista.cerrar()


def test_un_pago_que_falla_conserva_su_clave_para_reintentar_sin_duplicar(qapp, c, monkeypatch):
    vista = abrir(qapp, c, "COMERCIAL")
    datos = {
        "tercero": c.operador_sa.id,
        "sentido": "COBRO",
        "fecha": datetime.now(timezone.utc),
        "monto": "10",
        "moneda": "USD",
        "medio": "OTRO",
        "referencia": "",
    }  # un operador no puede hacer un cobro
    monkeypatch.setattr(comercial_view, "pedir", lambda *a, **k: datos)
    vista.registrar_pago()
    assert esperar(qapp, lambda: not vista.ocupado())
    clave = vista._clave_pago
    assert clave is not None and vista.estado.property("estado") == "error"
    datos["tercero"] = c.cliente_sa.id  # corrige y reintenta: misma clave
    vista.registrar_pago()
    assert esperar(qapp, lambda: not vista.ocupado() and vista.tabla_pagos.rowCount() == 1)
    assert vista._clave_pago is None
    vista.cerrar()


def test_el_cliente_solo_consulta_su_lado_sin_botones_de_gestion(qapp, c):
    c.entregado()
    vista = abrir(qapp, c, "CLIENTE")
    titulos = [vista.pestanias.tabText(i) for i in range(vista.pestanias.count())]
    assert titulos == ["Facturas", "Pagos", "Cuentas corrientes"]  # sin Cargos
    assert vista.tabla_facturas.rowCount() == 1 and vista.tabla_cuentas.item(0, 1).text() == "Por cobrar"
    assert not any(
        b.isVisible()
        for b in (
            vista.b_anular,
            vista.b_compra,
            vista.b_pago,
            vista.b_aplicar,
            vista.b_anular_pago,
            vista.b_cerrar,
        )
    )
    vista.cerrar()


def test_las_consultas_no_corren_en_el_hilo_grafico(qapp, c):
    hilos = []
    original = c.com.facturas

    def espiar(*a, **k):
        hilos.append(QThread.currentThread() == QApplication.instance().thread())
        return original(*a, **k)

    c.com.facturas = espiar
    vista = abrir(qapp, c, "COMERCIAL")
    assert hilos and not any(hilos)
    vista.cerrar()


def test_meses_cerrables_no_incluyen_el_mes_en_curso():
    meses = comercial_view._meses_cerrables(3)
    hoy = datetime.now(timezone.utc).date().replace(day=1)
    assert len(meses) == 3 and all(m < hoy for _, m in meses)


def test_una_respuesta_vieja_de_otra_cuenta_no_se_mezcla(qapp, c):
    """Elegir la cuenta A y enseguida la B: si los movimientos de A llegan tarde, se descartan."""
    c.cobro("10")
    c.cobro("20", moneda="ARS")
    vista = abrir(qapp, c, "COMERCIAL")
    usd = fila(vista.tabla_cuentas, 2, "USD")
    ars = fila(vista.tabla_cuentas, 2, "ARS")
    vista.tabla_cuentas.selectRow(usd)
    vista.tabla_cuentas.selectRow(ars)  # antes de que vuelvan los movimientos de USD
    assert esperar(qapp, lambda: not vista.ocupado())
    assert [vista.tabla_movimientos.item(i, 2).text() for i in range(vista.tabla_movimientos.rowCount())] == [
        "−20.00"
    ]
    vista.cerrar()
