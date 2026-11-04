"""Fase 8 — la ventana ejecuta los procesos periódicos solo en instancias internas y fuera del hilo
gráfico."""

from __future__ import annotations

import uuid
from dataclasses import replace

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from logistica.domain.procesos import ResultadoTarea, Tarea
from tests.test_fase1_ui import _permisos_sembrados, esperar, principal, qapp, servicios  # noqa: F401


class Procesos:
    def __init__(self):
        self.hilos = []

    def ejecutar_con_sesion(self, sesion_id):
        self.hilos.append(QThread.currentThread() == QApplication.instance().thread())
        return [
            ResultadoTarea(Tarea.FACTURACION, "2026-11-04T12:00", "EJECUTADA", "1 entrega(s) facturadas"),
            ResultadoTarea(Tarea.CIERRE_MENSUAL, "2026-10", "ERROR", "PostgreSQL no responde"),
        ]


def ventana(qapp, rol, externo=False):
    from logistica.ui.main_window import MainWindow

    procesos = Procesos()
    p = principal({rol}, _permisos_sembrados(rol), tercero_id=uuid.uuid4() if externo else None)
    w = MainWindow(replace(servicios(), procesos=procesos), p)
    return w, procesos


def test_una_instancia_interna_procesa_al_iniciar_en_un_trabajador(qapp):
    w, procesos = ventana(qapp, "COMERCIAL")
    assert esperar(qapp, lambda: "ejecutados" in w.estado_procesos.text())
    assert procesos.hilos == [False]  # no corrió en el hilo gráfico
    assert w.estado_procesos.text() == "Procesos: 1 ejecutados · 1 con error (ver logs)"
    assert "CIERRE_MENSUAL (2026-10): ERROR" in w.estado_procesos.toolTip()
    w.close()


def test_una_instancia_externa_no_procesa(qapp):
    w, procesos = ventana(qapp, "CLIENTE", externo=True)
    qapp.processEvents()
    assert procesos.hilos == [] and not w._timer_procesos.isActive()
    w.close()
