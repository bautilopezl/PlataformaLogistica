"""Circuito de monitoreo: todos los accesos a servicios corren en VistaBase/WorkerPool."""

from __future__ import annotations

import uuid
from dataclasses import fields
from datetime import datetime
from functools import partial
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from logistica.core.errors import ValidationError
from logistica.domain.acceso import Principal
from logistica.domain.monitoreo import validar_condicion
from logistica.domain.monitoreo_dtos import CondicionDatos, PaginaLecturas
from logistica.domain.simulacion_iot import ESCENARIOS
from logistica.services.envio_service import EnvioService
from logistica.services.monitoreo_service import MonitoreoService, SEVERIDADES, TIPOS_INCIDENTE_MANUAL
from logistica.ui.mapa_gps import MapaGPS
from logistica.ui.widgets import (
    Campo, VistaBase, clave_seleccionada, fecha_local, llenar_tabla, pedir, tabla,
)

ETIQUETAS_CONDICION = (
    "Temperatura mínima (°C)", "Temperatura máxima (°C)", "Humedad mínima (%)", "Humedad máxima (%)",
    "Vibración máxima (g)", "Batería mínima (%)", "Máximo sin comunicación (segundos)",
)
LECTURAS_COLUMNAS = (
    "Medición", "Sensor", "Temperatura °C", "Humedad %", "Vibración g", "Batería %", "Origen", "Evaluación",
)


class MonitoreoView(VistaBase):
    def __init__(self, monitoreo: MonitoreoService, envios: EnvioService, principal: Principal):
        super().__init__(principal.sesion_id)
        self._mon, self._envios, self._principal = monitoreo, envios, principal
        self._envios_datos: dict = {}
        self._alertas: dict = {}
        self._incidentes: dict = {}
        self._cursor: tuple[datetime, uuid.UUID] | None = None
        self._cursores: list[tuple[datetime, uuid.UUID] | None] = [None]
        self._cursores_solicitados = list(self._cursores)
        self._filtro_actual: dict[str, Any] | None = None
        self._demo_id: uuid.UUID | None = None
        self.envio = QComboBox()
        self.envio.addItem("Todos (estado, alertas e incidentes)", None)
        self.envio.currentIndexChanged.connect(self._cambiar_envio)
        self.boton_refrescar = QPushButton("Actualizar")
        self.boton_refrescar.clicked.connect(self.recargar)
        self.boton_condiciones = QPushButton("Condiciones antes de salir")
        self.boton_condiciones.clicked.connect(self.condiciones)
        self.boton_demo = QPushButton("Crear demo independiente")
        self.boton_demo.setVisible("ADMINISTRADOR" in principal.roles)
        self.boton_demo.clicked.connect(self.preparar_demo)
        cabecera = QHBoxLayout()
        for w in (self.envio, self.boton_refrescar, self.boton_condiciones, self.boton_demo):
            cabecera.addWidget(w)
        self.paneles = QTabWidget()
        self.sensores = tabla(("Sensor", "Estado administrativo", "Contenedor", "Envío", "Última medición",
                               "Seg. sin contacto", "Sin comunicación", "Alertas activas"))
        self.ultimas = tabla(LECTURAS_COLUMNAS)
        resumen = QWidget()
        layout_resumen = QVBoxLayout(resumen)
        layout_resumen.addWidget(QLabel("Estado actual · hasta 500 sensores"))
        layout_resumen.addWidget(self.sensores)
        layout_resumen.addWidget(QLabel("Últimas 20 mediciones del envío seleccionado"))
        layout_resumen.addWidget(self.ultimas)
        self.paneles.addTab(resumen, "Sensores y últimas mediciones")
        self._crear_historial()
        self._crear_alertas()
        self._crear_incidentes()
        self.mapa = MapaGPS()
        pagina_mapa = QWidget()
        mapa_layout = QVBoxLayout(pagina_mapa)
        mapa_layout.addWidget(QLabel(
            "Últimas 2000 posiciones del alcance. Una línea por sensor; no representa una ruta vial.\n"
            "Natural Earth 1:110m · dominio público · base local sin calles, sin red."))
        mapa_layout.addWidget(self.mapa)
        self.error_mapa = QLabel(self.mapa.error_recurso)
        mapa_layout.addWidget(self.error_mapa)
        self.paneles.addTab(pagina_mapa, "Mapa GPS")
        layout = QVBoxLayout(self)
        layout.addLayout(cabecera)
        layout.addWidget(QLabel(
            "El monitoreo detecta exposición, no daño. Normalizar una alerta no resuelve su incidente.\n"
            "Seleccioná un envío para ver lecturas, GPS o definir condiciones en BORRADOR/PLANIFICADO."))
        self.condiciones_actuales = QLabel("")
        self.condiciones_actuales.setWordWrap(True)
        layout.addWidget(self.condiciones_actuales)
        layout.addWidget(self.paneles)
        layout.addWidget(self.estado)
        self.actualizar_botones()

    def _crear_historial(self) -> None:
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        filtros = QHBoxLayout()
        self.campo = QComboBox()
        self.campo.addItem("Envío seleccionado", "envio_id")
        if self._principal.es_interno:
            self.campo.addItem("Sensor (UUID)", "sensor_id")
            self.campo.addItem("Contenedor (UUID)", "contenedor_id")
        self.valor = QLineEdit()
        self.valor.setPlaceholderText("UUID si el filtro es sensor/contenedor")
        self.desde, self.hasta = QLineEdit(), QLineEdit()
        self.desde.setPlaceholderText("Desde ISO con zona; opcional")
        self.hasta.setPlaceholderText("Hasta ISO con zona (exclusivo)")
        self.boton_filtrar = QPushButton("Filtrar")
        self.boton_filtrar.clicked.connect(self.filtrar)
        for w in (self.campo, self.valor, self.desde, self.hasta, self.boton_filtrar):
            filtros.addWidget(w)
        self.historial = tabla(LECTURAS_COLUMNAS)
        self.anterior, self.siguiente = QPushButton("Anterior"), QPushButton("Siguiente")
        self.anterior.clicked.connect(self.pagina_anterior)
        self.siguiente.clicked.connect(self.pagina_siguiente)
        self.pagina = QLabel("Página 1 · 50 lecturas por página")
        botones = QHBoxLayout()
        for w in (self.anterior, self.pagina, self.siguiente):
            botones.addWidget(w)
        layout.addLayout(filtros)
        layout.addWidget(self.historial)
        layout.addLayout(botones)
        self.paneles.addTab(pagina, "Historial")

    def _crear_alertas(self) -> None:
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        self.solo_activas = QCheckBox("Solo activas (desmarcar para ver cierres y recuperación)")
        self.solo_activas.setChecked(True)
        self.solo_activas.toggled.connect(self.refrescar_datos)
        self.alertas = tabla(("Envío", "Sensor", "Tipo", "Severidad", "Estado", "Primera detección",
                              "Última detección", "Cierre", "Motivo", "Incidente"))
        self.alertas.itemSelectionChanged.connect(self.actualizar_botones)
        self.reconocer = QPushButton("Reconocer")
        self.cerrar_alerta = QPushButton("Cerrar con motivo")
        self.incidente_alerta = QPushButton("Crear incidente desde alerta")
        self.reconocer.clicked.connect(self._reconocer)
        self.cerrar_alerta.clicked.connect(self._cerrar_alerta)
        self.incidente_alerta.clicked.connect(self._incidente_desde_alerta)
        botones = QHBoxLayout()
        for w in (self.reconocer, self.cerrar_alerta, self.incidente_alerta):
            botones.addWidget(w)
        layout.addWidget(self.solo_activas)
        layout.addWidget(QLabel("Hasta 500 alertas · filtros por envío y estado"))
        layout.addWidget(self.alertas)
        layout.addLayout(botones)
        self.paneles.addTab(pagina, "Alertas")

    def _crear_incidentes(self) -> None:
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        self.solo_abiertos = QCheckBox("Solo abiertos / en gestión")
        self.solo_abiertos.setChecked(True)
        self.solo_abiertos.toggled.connect(self.refrescar_datos)
        self.incidentes = tabla(("Envío", "Tipo", "Severidad", "Estado", "Descripción", "Responsable",
                                 "Ocurrido", "Resolución"))
        self.incidentes.itemSelectionChanged.connect(self.actualizar_botones)
        self.nuevo_incidente = QPushButton("Nuevo incidente del envío")
        self.tomar = QPushButton("Tomar a mi cargo")
        self.resolver = QPushButton("Resolver con explicación")
        self.nuevo_incidente.clicked.connect(self._nuevo_incidente)
        self.tomar.clicked.connect(self._tomar)
        self.resolver.clicked.connect(self._resolver)
        botones = QHBoxLayout()
        for w in (self.nuevo_incidente, self.tomar, self.resolver):
            botones.addWidget(w)
        layout.addWidget(self.solo_abiertos)
        layout.addWidget(QLabel("Hasta 500 incidentes · filtros por envío y estado"))
        layout.addWidget(self.incidentes)
        layout.addLayout(botones)
        self.paneles.addTab(pagina, "Incidentes")

    def recargar(self) -> None:
        if not self.ocupado():
            self.ejecutar(self._envios.listar, self._cargar_envios, self.sesion_id)

    def _cargar_envios(self, envios: list) -> None:
        seleccionado = self._demo_id or self.envio.currentData()
        self._demo_id = None
        self._envios_datos = {e.id: e for e in envios}
        self.envio.blockSignals(True)
        self.envio.clear()
        self.envio.addItem("Todos (estado, alertas e incidentes)", None)
        for e in envios:
            self.envio.addItem(f"{e.codigo} · {e.estado} · {e.descripcion_carga}", e.id)
        self.seleccionar_envio(seleccionado)
        self.envio.blockSignals(False)
        self._cambiar_envio()

    def seleccionar_envio(self, envio_id: uuid.UUID | None) -> None:
        # QVariant no compara por valor dos objetos Python UUID distintos.
        indice = next((i for i in range(self.envio.count()) if self.envio.itemData(i) == envio_id), 0)
        self.envio.setCurrentIndex(indice)

    def _cambiar_envio(self) -> None:
        self._filtro_actual = None
        self._cursor = None
        self._cursores = [None]
        llenar_tabla(self.historial, [], [])
        for widget in (self.sensores, self.ultimas, self.alertas, self.incidentes):
            llenar_tabla(widget, [], [])
        self.mapa.mostrar([])
        self.condiciones_actuales.clear()
        self.pagina.setText("Página 1 · 50 lecturas por página")
        self.refrescar_datos()

    def refrescar_datos(self) -> None:
        if self.ocupado():
            return
        eid, activas, abiertos = (
            self.envio.currentData(), self.solo_activas.isChecked(), self.solo_abiertos.isChecked())

        def consultar():
            return (
                self._mon.estado_sensores(self.sesion_id, envio_id=eid),
                self._mon.alertas(self.sesion_id, envio_id=eid, solo_activas=activas),
                self._mon.incidentes(self.sesion_id, envio_id=eid, solo_abiertos=abiertos),
                self._mon.ultimas_lecturas(self.sesion_id, eid) if eid else [],
                self._mon.posiciones(self.sesion_id, eid) if eid else [],
                self._mon.condiciones(self.sesion_id, eid) if eid else None,
            )

        self.ejecutar(consultar, self._mostrar)

    def _mostrar(self, datos: tuple) -> None:
        sensores, alertas, incidentes, ultimas, posiciones, condiciones = datos
        llenar_tabla(self.sensores, [
            (s.sensor_codigo, s.sensor_estado, s.contenedor_codigo, s.envio_codigo,
             fecha_local(s.ultima_medicion_en), s.sin_comunicacion_s, "Sí" if s.sin_comunicacion else "No",
             s.alertas_activas) for s in sensores], [s.sensor_id for s in sensores])
        self._alertas = {a.id: a for a in alertas}
        llenar_tabla(self.alertas, [
            (a.envio_codigo, a.sensor_codigo, a.tipo, a.severidad, a.estado,
             fecha_local(a.primera_deteccion_en), fecha_local(a.ultima_deteccion_en),
             a.cierre_tipo, a.motivo_cierre, a.incidente_id)
            for a in alertas], [a.id for a in alertas])
        self._incidentes = {i.id: i for i in incidentes}
        llenar_tabla(self.incidentes, [
            (i.envio_codigo, i.tipo, i.severidad, i.estado, i.descripcion, i.responsable_email,
             fecha_local(i.ocurrido_en), i.resolucion) for i in incidentes], [i.id for i in incidentes])
        self._mostrar_lecturas(self.ultimas, ultimas)
        self.mapa.mostrar(posiciones)
        if self.envio.currentData() is None:
            texto = ""
        elif condiciones is None:
            texto = "Sin condiciones cargadas: solo se controla comunicación (180 s)."
        else:
            texto = "Condiciones: " + " · ".join(
                f"{etiqueta}: {getattr(condiciones, f.name)}"
                for f, etiqueta in zip(fields(CondicionDatos), ETIQUETAS_CONDICION)
                if getattr(condiciones, f.name) is not None)
        self.condiciones_actuales.setText(texto)
        self.estado.setText("Sin registros." if not any(datos) else "Datos actualizados.")

    @staticmethod
    def _mostrar_lecturas(widget, lecturas: list) -> None:
        llenar_tabla(widget, [
            (fecha_local(l.fecha_medicion), l.sensor_id, l.temperatura_c, l.humedad_pct, l.vibracion_g,
             l.bateria_pct, l.origen, l.evaluacion) for l in lecturas], [l.id for l in lecturas])

    def filtrar(self) -> None:
        try:
            campo = self.campo.currentData()
            valor = self.envio.currentData() if campo == "envio_id" else uuid.UUID(self.valor.text().strip())
            if valor is None:
                raise ValueError("Seleccione un envío.")
            fechas = [datetime.fromisoformat(w.text().strip()) if w.text().strip() else None
                      for w in (self.desde, self.hasta)]
            if any(f is not None and f.tzinfo is None for f in fechas):
                raise ValueError("Incluya zona horaria, por ejemplo 2026-11-03T18:00:00-03:00.")
            if all(f is not None for f in fechas) and fechas[0] >= fechas[1]:
                raise ValueError("Desde debe ser anterior a hasta.")
        except ValueError as exc:
            self._set_estado(f"Filtro inválido: {exc}", "error")
            return
        self._filtro_actual = dict(campo=campo, valor=valor, desde=fechas[0], hasta=fechas[1])
        self._cursores = [None]
        self._cursor = None
        llenar_tabla(self.historial, [], [])
        self._cargar_pagina([None])

    def _cargar_pagina(self, cursores: list[tuple[datetime, uuid.UUID] | None]) -> None:
        self._cursores_solicitados = cursores
        self.ejecutar(partial(self._mon.lecturas, self.sesion_id, **self._filtro_actual,
                             limite=50, despues=cursores[-1]), self._mostrar_pagina)

    def _mostrar_pagina(self, pagina: PaginaLecturas) -> None:
        self._cursores = self._cursores_solicitados
        self._cursor = pagina.siguiente
        self._mostrar_lecturas(self.historial, pagina.lecturas)
        self.pagina.setText(f"Página {len(self._cursores)} · {len(pagina.lecturas)} lecturas")

    def pagina_siguiente(self) -> None:
        if self._cursor is not None and not self.ocupado():
            self._cargar_pagina([*self._cursores, self._cursor])

    def pagina_anterior(self) -> None:
        if len(self._cursores) > 1 and not self.ocupado():
            self._cargar_pagina(self._cursores[:-1])

    def condiciones(self) -> None:
        self.ejecutar(
            self._mon.condiciones, self._editar_condiciones, self.sesion_id, self.envio.currentData())

    def _editar_condiciones(self, datos: CondicionDatos | None) -> None:
        datos = datos or CondicionDatos()
        campos = [Campo(f.name, etiqueta, valor=getattr(datos, f.name))
                  for f, etiqueta in zip(fields(CondicionDatos), ETIQUETAS_CONDICION)]
        valores = pedir(self, "Condiciones · vacío = variable no controlada", campos,
                        lambda v: validar_condicion(CondicionDatos(**v)))
        if valores is not None:
            self.ejecutar(self._mon.definir_condiciones, self._accion_ok,
                          self.sesion_id, self.envio.currentData(), CondicionDatos(**valores))

    def _reconocer(self) -> None:
        self.ejecutar(self._mon.reconocer_alerta, self._accion_ok,
                      self.sesion_id, clave_seleccionada(self.alertas))

    def _cerrar_alerta(self) -> None:
        v = pedir(self, "Cerrar alerta", [Campo("motivo", "Motivo", obligatorio=True)])
        if v:
            self.ejecutar(self._mon.cerrar_alerta, self._accion_ok, self.sesion_id,
                          clave_seleccionada(self.alertas), v["motivo"])

    def _incidente_desde_alerta(self) -> None:
        alerta = self._alertas[clave_seleccionada(self.alertas)]
        self._crear_incidente(alerta.envio_id, alerta.id)

    def _nuevo_incidente(self) -> None:
        self._crear_incidente(self.envio.currentData(), None)

    def _crear_incidente(self, envio_id: uuid.UUID, alerta_id: uuid.UUID | None) -> None:
        v = pedir(self, "Nuevo incidente", [
            Campo("tipo", "Tipo", "opciones", opciones=[(s, s) for s in TIPOS_INCIDENTE_MANUAL]),
            Campo("severidad", "Severidad", "opciones",
                  opciones=[(s, s) for s in SEVERIDADES], valor="MEDIA"),
            Campo("descripcion", "Descripción", obligatorio=True)])
        if v:
            self.ejecutar(partial(self._mon.crear_incidente, self.sesion_id, envio_id,
                                 alerta_id=alerta_id, **v), self._accion_ok)

    def _tomar(self) -> None:
        self.ejecutar(self._mon.tomar_incidente, self._accion_ok,
                      self.sesion_id, clave_seleccionada(self.incidentes))

    def _resolver(self) -> None:
        v = pedir(self, "Resolver incidente", [Campo("resolucion", "Resolución", obligatorio=True)])
        if v:
            self.ejecutar(self._mon.resolver_incidente, self._accion_ok, self.sesion_id,
                          clave_seleccionada(self.incidentes), v["resolucion"])

    def _accion_ok(self, _resultado: Any) -> None:
        self.refrescar_datos()

    def preparar_demo(self) -> None:
        v = pedir(self, "Demo independiente (crea datos nuevos, no altera datos existentes)", [
            Campo("confirmacion", "Escribí DEMO para crear viaje, recursos y lecturas SIMULADOR",
                  obligatorio=True),
            Campo("escenario", "Escenario (reloj SIMULADOR acelerado)", "opciones",
                  opciones=[(s.title(), s) for s in ESCENARIOS], valor="completo")],
            self._validar_demo)
        if v:
            self.ejecutar(self._mon.preparar_demo, self._demo_creada, self.sesion_id, v["escenario"])

    @staticmethod
    def _validar_demo(valores: dict) -> None:
        if valores["confirmacion"] != "DEMO":
            raise ValidationError("confirmación", user_message="Escribí DEMO para confirmar.")

    def _demo_creada(self, envio_id: uuid.UUID) -> None:
        self._demo_id = envio_id
        self.solo_activas.blockSignals(True)
        self.solo_activas.setChecked(False)
        self.solo_activas.blockSignals(False)
        self.paneles.setCurrentIndex(2)
        self.recargar()

    def actualizar_botones(self) -> None:
        if not hasattr(self, "resolver"):
            return
        libre, permisos = not self.ocupado(), self._principal.permisos
        envio = self._envios_datos.get(self.envio.currentData())
        alerta = self._alertas.get(clave_seleccionada(self.alertas))
        incidente = self._incidentes.get(clave_seleccionada(self.incidentes))
        self.envio.setEnabled(libre)
        self.solo_activas.setEnabled(libre)
        self.solo_abiertos.setEnabled(libre)
        for b in (self.boton_refrescar, self.boton_filtrar, self.boton_demo):
            b.setEnabled(libre)
        self.boton_condiciones.setVisible("envios.planificar" in permisos)
        self.boton_condiciones.setEnabled(
            libre and envio is not None and envio.estado in ("BORRADOR", "PLANIFICADO"))
        self.reconocer.setVisible("alertas.reconocer" in permisos)
        self.reconocer.setEnabled(libre and alerta is not None and alerta.estado == "ABIERTA")
        self.cerrar_alerta.setVisible("alertas.cerrar" in permisos)
        self.cerrar_alerta.setEnabled(libre and alerta is not None and alerta.estado != "CERRADA")
        self.incidente_alerta.setVisible("incidentes.crear" in permisos)
        self.incidente_alerta.setEnabled(libre and alerta is not None and alerta.incidente_id is None)
        self.nuevo_incidente.setVisible("incidentes.crear" in permisos)
        self.nuevo_incidente.setEnabled(libre and envio is not None
                                        and envio.estado in ("PLANIFICADO", "EN_TRANSITO", "ENTREGADO"))
        for b in (self.tomar, self.resolver):
            b.setVisible("incidentes.gestionar" in permisos)
            b.setEnabled(libre and incidente is not None and incidente.estado != "RESUELTO")
        self.anterior.setEnabled(libre and len(self._cursores) > 1)
        self.siguiente.setEnabled(libre and self._cursor is not None)
