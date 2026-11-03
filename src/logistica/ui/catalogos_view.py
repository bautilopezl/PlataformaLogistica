"""Pestaña "Catálogos": ubicaciones, vehículos, contenedores y sensores (con su instalación en
contenedores).
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTabWidget, QVBoxLayout, QWidget

from logistica.domain.logistica import (
    ContenedorDatos,
    EstadoRecurso,
    SensorDatos,
    TipoContenedor,
    TipoSensor,
    TipoUbicacion,
    TipoVehiculo,
    UbicacionDatos,
    VariableSensor,
    VehiculoDatos,
)
from logistica.services.catalogo_service import CatalogoService
from logistica.services.tercero_service import TerceroService
from logistica.ui.widgets import (
    Campo,
    VistaBase,
    clave_seleccionada,
    fecha_local,
    llenar_tabla,
    pedir,
    si_no,
    tabla,
)


def _opciones(enum) -> list[tuple[str, str]]:
    return [(e.value.replace("_", " ").title(), e.value) for e in enum]


ESTADOS = _opciones(EstadoRecurso)


class _TablaCatalogo(VistaBase):
    """Base de las cuatro pantallas: una tabla, un botón Recargar y botones propios."""

    COLUMNAS: Sequence[str] = ()

    def __init__(self, sesion_id: uuid.UUID, parent: QWidget | None = None):
        super().__init__(sesion_id, parent)
        self._items: dict[uuid.UUID, Any] = {}
        self.tabla = tabla(self.COLUMNAS)
        self.tabla.itemSelectionChanged.connect(self.actualizar_botones)
        self._botones: list[tuple[QPushButton, bool]] = []  # (botón, requiere selección)
        self.acciones = QHBoxLayout()
        self.boton("Recargar", self.recargar, requiere_seleccion=False)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabla)
        layout.addLayout(self.acciones)
        layout.addWidget(self.estado)

    def boton(
        self, texto: str, accion: Callable[[], None], *, requiere_seleccion: bool = True
    ) -> QPushButton:
        b = QPushButton(texto)
        b.clicked.connect(accion)
        self.acciones.addWidget(b)
        self._botones.append((b, requiere_seleccion))
        return b

    def mostrar(self, items: list[Any], fila: Callable[[Any], tuple]) -> None:
        self._items = {i.id: i for i in items}
        llenar_tabla(self.tabla, [fila(i) for i in items], [i.id for i in items])

    def seleccionado(self) -> Any | None:
        clave = clave_seleccionada(self.tabla)
        return self._items.get(clave) if clave else None

    def actualizar_botones(self) -> None:
        libre, hay = not self.ocupado(), self.seleccionado() is not None
        for b, requiere in self._botones:
            b.setEnabled(libre and (hay or not requiere))


# --------------------------------------------------------------------------- ubicaciones
class UbicacionesView(_TablaCatalogo):
    COLUMNAS = ("Nombre", "Tipo", "País", "Ciudad", "Coordenadas", "Activa")

    def __init__(self, servicio: CatalogoService, sesion_id: uuid.UUID, parent: QWidget | None = None):
        super().__init__(sesion_id, parent)
        self._s = servicio
        self.boton("Nueva", self.nueva, requiere_seleccion=False)
        self.boton("Editar", self.editar)
        self.boton("Activar / desactivar", self.alternar)
        self.acciones.addStretch()
        self.actualizar_botones()

    def recargar(self) -> None:
        self.ejecutar(
            lambda sid: self._s.listar_ubicaciones(sid, incluir_inactivas=True), self._mostrar, self.sesion_id
        )

    def _mostrar(self, items) -> None:
        self.mostrar(
            items,
            lambda u: (
                u.nombre,
                u.tipo,
                u.pais_codigo,
                u.ciudad,
                f"{u.latitud}, {u.longitud}" if u.latitud is not None else "",
                si_no(u.activo),
            ),
        )

    def _campos(self, u=None) -> list[Campo]:
        return [
            Campo("nombre", "Nombre", valor=u.nombre if u else None, obligatorio=True),
            Campo("tipo", "Tipo", "opciones", opciones=_opciones(TipoUbicacion), valor=u.tipo if u else None),
            Campo("pais", "País (ISO)", valor=u.pais_codigo if u else "AR", obligatorio=True),
            Campo("ciudad", "Ciudad", valor=u.ciudad if u else None, obligatorio=True),
            Campo("direccion", "Dirección", valor=u.direccion if u else None),
            Campo("latitud", "Latitud", valor=u.latitud if u else None),
            Campo("longitud", "Longitud", valor=u.longitud if u else None),
        ]

    @staticmethod
    def _datos(v: dict) -> UbicacionDatos:
        return UbicacionDatos(
            v["nombre"], v["tipo"], v["pais"], v["ciudad"], v["direccion"], v["latitud"], v["longitud"]
        )

    def nueva(self) -> None:
        v = pedir(self, "Nueva ubicación", self._campos(), lambda v: self._datos(v).normalizado())
        if v:
            self.ejecutar(self._s.crear_ubicacion, lambda _r: self.recargar(), self.sesion_id, self._datos(v))

    def editar(self) -> None:
        u = self.seleccionado()
        v = u and pedir(self, "Editar ubicación", self._campos(u), lambda v: self._datos(v).normalizado())
        if v:
            self.ejecutar(
                self._s.actualizar_ubicacion, lambda _r: self.recargar(), self.sesion_id, u.id, self._datos(v)
            )

    def alternar(self) -> None:
        u = self.seleccionado()
        if u:
            self.ejecutar(
                self._s.set_ubicacion_activa, lambda _r: self.recargar(), self.sesion_id, u.id, not u.activo
            )


# --------------------------------------------------------------------------- vehículos
class VehiculosView(_TablaCatalogo):
    COLUMNAS = ("Operador", "Tipo", "Identificación", "Estado")

    def __init__(
        self,
        servicio: CatalogoService,
        terceros: TerceroService,
        sesion_id: uuid.UUID,
        parent: QWidget | None = None,
    ):
        super().__init__(sesion_id, parent)
        self._s, self._terceros = servicio, terceros
        self.boton("Nuevo", self.nuevo, requiere_seleccion=False)
        self.boton("Cambiar estado", self.cambiar_estado)
        self.acciones.addStretch()
        self.actualizar_botones()

    def recargar(self) -> None:
        self.ejecutar(self._s.listar_vehiculos, self._mostrar, self.sesion_id)

    def _mostrar(self, items) -> None:
        self.mostrar(items, lambda v: (v.operador_razon_social, v.tipo, v.identificacion, v.estado))

    def nuevo(self) -> None:
        # Primero se traen los operadores (para elegir el dueño del vehículo), después se abre el formulario.
        self.ejecutar(
            lambda sid: self._terceros.listar(sid, rol="OPERADOR"), self._abrir_alta, self.sesion_id
        )

    def _abrir_alta(self, operadores) -> None:
        campos = [
            Campo("operador", "Operador", "opciones", opciones=[(t.razon_social, t.id) for t in operadores]),
            Campo("tipo", "Tipo", "opciones", opciones=_opciones(TipoVehiculo)),
            Campo("ident", "Identificación (patente, IMO, matrícula)", obligatorio=True),
        ]
        v = pedir(
            self,
            "Nuevo vehículo",
            campos,
            lambda v: VehiculoDatos(v["operador"], v["tipo"], v["ident"]).normalizado(),
        )
        if v:
            self.ejecutar(
                self._s.crear_vehiculo,
                lambda _r: self.recargar(),
                self.sesion_id,
                VehiculoDatos(v["operador"], v["tipo"], v["ident"]),
            )

    def cambiar_estado(self) -> None:
        x = self.seleccionado()
        v = x and pedir(
            self,
            "Estado del vehículo",
            [Campo("estado", "Estado", "opciones", opciones=ESTADOS, valor=x.estado)],
        )
        if v:
            self.ejecutar(
                self._s.set_estado_vehiculo, lambda _r: self.recargar(), self.sesion_id, x.id, v["estado"]
            )


# --------------------------------------------------------------------------- contenedores
class ContenedoresView(_TablaCatalogo):
    COLUMNAS = ("Código", "Tipo", "Capacidad (kg)", "Capacidad (m³)", "Estado")

    def __init__(self, servicio: CatalogoService, sesion_id: uuid.UUID, parent: QWidget | None = None):
        super().__init__(sesion_id, parent)
        self._s = servicio
        self.boton("Nuevo", self.nuevo, requiere_seleccion=False)
        self.boton("Cambiar estado", self.cambiar_estado)
        self.acciones.addStretch()
        self.actualizar_botones()

    def recargar(self) -> None:
        self.ejecutar(self._s.listar_contenedores, self._mostrar, self.sesion_id)

    def _mostrar(self, items) -> None:
        self.mostrar(
            items, lambda c: (c.codigo_internacional, c.tipo, c.capacidad_kg, c.capacidad_m3, c.estado)
        )

    def nuevo(self) -> None:
        campos = [
            Campo("codigo", "Código internacional", obligatorio=True),
            Campo("tipo", "Tipo", "opciones", opciones=_opciones(TipoContenedor)),
            Campo("kg", "Capacidad (kg)", obligatorio=True),
            Campo("m3", "Capacidad (m³)", obligatorio=True),
        ]
        armar = lambda v: ContenedorDatos(v["codigo"], v["tipo"], v["kg"], v["m3"])  # noqa: E731
        v = pedir(self, "Nuevo contenedor", campos, lambda v: armar(v).normalizado())
        if v:
            self.ejecutar(self._s.crear_contenedor, lambda _r: self.recargar(), self.sesion_id, armar(v))

    def cambiar_estado(self) -> None:
        x = self.seleccionado()
        v = x and pedir(
            self,
            "Estado del contenedor",
            [Campo("estado", "Estado", "opciones", opciones=ESTADOS, valor=x.estado)],
        )
        if v:
            self.ejecutar(
                self._s.set_estado_contenedor, lambda _r: self.recargar(), self.sesion_id, x.id, v["estado"]
            )


# --------------------------------------------------------------------------- sensores
class SensoresView(_TablaCatalogo):
    COLUMNAS = ("Código", "Tipo", "Fabricante", "Mide", "Estado", "Instalado en")

    def __init__(self, servicio: CatalogoService, sesion_id: uuid.UUID, parent: QWidget | None = None):
        super().__init__(sesion_id, parent)
        self._s = servicio
        self.boton("Nuevo", self.nuevo, requiere_seleccion=False)
        self.boton("Cambiar estado", self.cambiar_estado)
        self.boton("Instalar en contenedor", self.instalar)
        self.boton("Retirar", self.retirar)
        self.acciones.addStretch()
        self.actualizar_botones()

    def recargar(self) -> None:
        self.ejecutar(self._s.listar_sensores, self._mostrar, self.sesion_id)

    def _mostrar(self, items) -> None:
        self.mostrar(
            items,
            lambda s: (
                s.codigo,
                s.tipo,
                s.fabricante,
                ", ".join(sorted(s.capacidades)),
                s.estado,
                s.contenedor_actual or "(sin instalar)",
            ),
        )

    def nuevo(self) -> None:
        campos = [
            Campo("codigo", "Código", obligatorio=True),
            Campo("tipo", "Tipo", "opciones", opciones=_opciones(TipoSensor)),
            Campo("fabricante", "Fabricante", obligatorio=True),
            Campo("modelo", "Modelo"),
            Campo("intervalo", "Intervalo esperado (s)", valor=60, obligatorio=True),
            Campo("caps", "Mide", "casillas", opciones=_opciones(VariableSensor), obligatorio=True),
        ]
        armar = lambda v: SensorDatos(
            v["codigo"],
            v["tipo"],
            v["fabricante"],
            v["modelo"],
            v["intervalo"],  # noqa: E731
            frozenset(v["caps"]),
        )
        v = pedir(self, "Nuevo sensor", campos, lambda v: armar(v).normalizado())
        if v:
            self.ejecutar(self._s.crear_sensor, lambda _r: self.recargar(), self.sesion_id, armar(v))

    def cambiar_estado(self) -> None:
        x = self.seleccionado()
        v = x and pedir(
            self,
            "Estado del sensor",
            [Campo("estado", "Estado", "opciones", opciones=ESTADOS, valor=x.estado)],
        )
        if v:
            self.ejecutar(
                self._s.set_estado_sensor, lambda _r: self.recargar(), self.sesion_id, x.id, v["estado"]
            )

    def instalar(self) -> None:
        if self.seleccionado():
            self.ejecutar(self._s.listar_contenedores, self._abrir_instalacion, self.sesion_id)

    def _abrir_instalacion(self, contenedores) -> None:
        sensor = self.seleccionado()
        if sensor is None:
            return
        campos = [
            Campo(
                "contenedor",
                "Contenedor",
                "opciones",
                opciones=[(c.codigo_internacional, c.id) for c in contenedores if c.estado != "BAJA"],
            ),
            Campo("desde", "Instalado el", "fecha_hora"),
            Campo("obs", "Observaciones"),
        ]
        v = pedir(self, f"Instalar {sensor.codigo}", campos)
        if v and v["contenedor"]:
            self.ejecutar(
                self._s.instalar_sensor,
                lambda _r: self.recargar(),
                self.sesion_id,
                sensor.id,
                v["contenedor"],
                v["desde"],
                v["obs"],
            )

    def retirar(self) -> None:
        if self.seleccionado():
            self.ejecutar(
                self._s.historial_sensor, self._abrir_retiro, self.sesion_id, self.seleccionado().id
            )

    def _abrir_retiro(self, historial) -> None:
        abierta = next((i for i in historial if i.retirado_en is None), None)
        if abierta is None:
            self._set_estado("El sensor no está instalado.", "error")
            return
        v = pedir(
            self,
            f"Retirar de {abierta.contenedor_codigo} (instalado el {fecha_local(abierta.instalado_en)})",
            [Campo("hasta", "Retirado el", "fecha_hora")],
        )
        if v:
            self.ejecutar(
                self._s.retirar_sensor, lambda _r: self.recargar(), self.sesion_id, abierta.id, v["hasta"]
            )


# --------------------------------------------------------------------------- contenedor de pestañas
class CatalogosView(QWidget):
    """Agrupa las cuatro pantallas; cada una carga sus datos al mostrarse."""

    sesion_vencida = Signal(str)  # reenvía el aviso de cualquiera de las cuatro pantallas

    def __init__(
        self,
        servicio: CatalogoService,
        terceros: TerceroService,
        sesion_id: uuid.UUID,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.tabs = QTabWidget()
        self.vistas: list[_TablaCatalogo] = [
            UbicacionesView(servicio, sesion_id),
            VehiculosView(servicio, terceros, sesion_id),
            ContenedoresView(servicio, sesion_id),
            SensoresView(servicio, sesion_id),
        ]
        for vista, titulo in zip(self.vistas, ("Ubicaciones", "Vehículos", "Contenedores", "Sensores")):
            self.tabs.addTab(vista, titulo)
            vista.sesion_vencida.connect(self.sesion_vencida)
        self.tabs.currentChanged.connect(lambda i: self.vistas[i].recargar())
        QVBoxLayout(self).addWidget(self.tabs)

    def recargar(self) -> None:
        self.vistas[self.tabs.currentIndex()].recargar()

    def ocupado(self) -> bool:
        return any(v.ocupado() for v in self.vistas)

    def cerrar(self) -> None:
        for v in self.vistas:
            v.cerrar()
