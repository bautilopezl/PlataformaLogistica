"""Mapa vectorial local de Natural Earth (dominio público), dibujado por Qt, sin red."""

from __future__ import annotations

import json
import logging
import math
import uuid
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QResizeEvent, QTransform, QWheelEvent
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView

from logistica.domain.monitoreo_dtos import PosicionDTO

log = logging.getLogger(__name__)
RECURSO = Path(__file__).with_name("recursos") / "tierra_110m.geojson"
REFERENCIAS = (
    ("Buenos Aires, AR", -34.60, -58.38),
    ("Rosario, AR", -32.95, -60.64),
    ("Montevideo, UY", -34.90, -56.16),
    ("Santiago, CL", -33.45, -70.67),
    ("São Paulo, BR", -23.55, -46.63),
    ("Lima, PE", -12.05, -77.04),
    ("Bogotá, CO", 4.71, -74.07),
    ("Madrid, ES", 40.42, -3.70),
)


class MapaGPS(QGraphicsView):
    """Proyección equirectangular lon/-lat. Arrastre para mover; rueda para acercar."""

    def __init__(self) -> None:
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor("#dcecf4"))
        self.setMinimumHeight(260)
        self.trayectos: dict[uuid.UUID, list[PosicionDTO]] = {}
        self._dinamicos: list[QGraphicsItem] = []
        self._encuadre = QRectF(-8000, -1500, 5000, 4500)
        self.error_recurso = ""
        self._base()
        self.encuadrar()

    @staticmethod
    def proyectar(latitud: float, longitud: float) -> QPointF:
        return QPointF(longitud * 100, -latitud * 100)

    def _base(self) -> None:
        try:
            datos = json.loads(RECURSO.read_text(encoding="utf-8"))
            for feature in datos["features"]:
                path = QPainterPath()
                for anillo in feature["geometry"]["coordinates"]:
                    for i, (lon, lat) in enumerate(anillo):
                        punto = self.proyectar(lat, lon)
                        path.moveTo(punto) if i == 0 else path.lineTo(punto)
                    path.closeSubpath()
                self.scene().addPath(path, QPen(QColor("#7f927a"), 0), QColor("#e6e7c9"))
        except (OSError, ValueError, KeyError, TypeError):
            log.exception("No se pudo cargar la base geográfica local")
            self.error_recurso = "No se pudo cargar Natural Earth; revise el recurso local (ver logs)."
        pen = QPen(QColor("#a3b8c2"), 0)
        for lon in range(-180, 181, 10):
            self.scene().addLine(lon * 100, -9000, lon * 100, 9000, pen)
        for lat in range(-80, 81, 10):
            self.scene().addLine(-18000, -lat * 100, 18000, -lat * 100, pen)
        for nombre, lat, lon in REFERENCIAS:
            self._rotulo(nombre, self.proyectar(lat, lon), QColor("#394648"))
        self.setSceneRect(-18000, -9000, 36000, 18000)

    def _rotulo(self, texto: str, punto: QPointF, color: QColor) -> QGraphicsItem:
        item = self.scene().addSimpleText(texto)
        item.setBrush(color)
        item.setPos(punto)
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        return item

    def mostrar(self, posiciones: list[PosicionDTO]) -> None:
        for item in self._dinamicos:
            self.scene().removeItem(item)
        self._dinamicos.clear()
        self.trayectos = {}
        for p in sorted(posiciones, key=lambda p: p.fecha_medicion):
            self.trayectos.setdefault(p.sensor_id, []).append(p)
        colores = ("#b12232", "#1555a0", "#663399", "#187447")
        for i, (sensor, puntos) in enumerate(self.trayectos.items()):
            color = QColor(colores[i % len(colores)])
            path = QPainterPath()
            anterior = None
            for p in puntos:
                xy = self.proyectar(p.latitud, p.longitud)
                if anterior is None or abs(p.longitud - anterior.longitud) > 180:
                    path.moveTo(xy)  # no dibujar un cruce falso por el antimeridiano
                else:
                    path.lineTo(xy)
                marca = self.scene().addEllipse(-3, -3, 6, 6, QPen(color, 0), color)
                marca.setPos(xy)
                marca.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                marca.setToolTip(
                    f"Sensor {sensor}\n{p.fecha_medicion.astimezone():%d/%m/%Y %H:%M:%S}\n"
                    f"{p.latitud:.6f}, {p.longitud:.6f}"
                )
                self._dinamicos.append(marca)
                anterior = p
            pen = QPen(color, 2)
            pen.setCosmetic(True)
            self._dinamicos.append(self.scene().addPath(path, pen))
            rotulo = self._rotulo(f"Última · {str(sensor)[:8]}", xy, color)
            rotulo.setTransform(QTransform.fromTranslate(8, -24))
            self._dinamicos.append(rotulo)
        if posiciones:
            xs = [p.longitud * 100 for p in posiciones]
            ys = [-p.latitud * 100 for p in posiciones]
            ancho, alto = max(max(xs) - min(xs), 120), max(max(ys) - min(ys), 120)
            self._encuadre = QRectF(min(xs) - ancho * .3, min(ys) - alto * .3, ancho * 1.6, alto * 1.6)
        self.encuadrar()

    def encuadrar(self) -> None:
        self.fitInView(self._encuadre, Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.encuadrar()

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.25 if event.angleDelta().y() > 0 else .8
        escala = self.transform().m11() * factor
        if .005 < escala < 20:
            self.scale(factor, factor)
        event.accept()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """Retícula local y referencias numéricas legibles a cualquier nivel de zoom."""
        pen = QPen(QColor("#829da8"), 0)
        painter.setPen(pen)
        paso = 10 ** math.floor(math.log10(max(rect.width() / 500, .00001)))
        for eje, minimo, maximo in (("lon", rect.left() / 100, rect.right() / 100),
                                   ("lat", -rect.bottom() / 100, -rect.top() / 100)):
            for i in range(math.floor(minimo / paso), math.ceil(maximo / paso) + 1):
                valor = i * paso
                if eje == "lon":
                    painter.drawLine(QPointF(valor * 100, rect.top()), QPointF(valor * 100, rect.bottom()))
                else:
                    painter.drawLine(QPointF(rect.left(), -valor * 100), QPointF(rect.right(), -valor * 100))
        centro = self.mapToScene(self.viewport().rect().center())
        painter.save()
        painter.resetTransform()
        painter.fillRect(0, 0, self.viewport().width(), 24, QColor("#ffffff"))
        painter.setPen(QColor("#203040"))
        painter.drawText(8, 17, f"N ↑ · Centro {(-centro.y()/100):.3f}°, {(centro.x()/100):.3f}° · "
                         "Lon/lat WGS84 · Arrastrar / rueda")
        painter.restore()
