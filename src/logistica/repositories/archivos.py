"""Archivos de reportes en disco (CSV con `csv`, PDF con `reportlab`).

Patrón Strategy: un escritor por formato con la misma interfaz `escribir(tabla, ruta)`. El servicio no sabe
cómo se arma cada formato; solo pide "guardá esta tabla como PDF". SUPUESTO R1 (AGENTS.md §16.7): carpeta
local por equipo (`REPORTES_DIR`). El registro en `reporte` guarda solo la clave del archivo; si en otro
equipo no está, se vuelve a generar desde los parámetros registrados.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Protocol

from logistica.domain.analitica import Tabla, celda, celda_csv


class Escritor(Protocol):
    extension: str

    def escribir(self, tabla: Tabla, ruta: Path, subtitulo: str) -> None: ...


class EscritorCSV:
    extension = "csv"

    def escribir(self, tabla: Tabla, ruta: Path, subtitulo: str) -> None:
        # utf-8-sig: Excel en Windows reconoce los acentos; ";" porque en es-AR la coma es el separador
        # decimal.
        with ruta.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow([tabla.titulo])
            w.writerow([subtitulo])
            w.writerow(tabla.columnas)
            for fila in tabla.filas:
                w.writerow([celda_csv(v) for v in fila])
            for nota in tabla.notas:
                w.writerow([nota])


class EscritorPDF:
    extension = "pdf"

    def escribir(self, tabla: Tabla, ruta: Path, subtitulo: str) -> None:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        estilos = getSampleStyleSheet()
        chico = estilos["BodyText"].clone("chico", fontSize=8, leading=10)
        doc = SimpleDocTemplate(
            str(ruta),
            pagesize=landscape(A4),
            title=tabla.titulo,
            leftMargin=28,
            rightMargin=28,
            topMargin=28,
            bottomMargin=28,
        )
        datos = [[Paragraph(f"<b>{_xml(c)}</b>", chico) for c in tabla.columnas]]
        datos += [[Paragraph(_xml(celda(v)), chico) for v in fila] for fila in tabla.filas]
        if not tabla.filas:
            datos.append(
                [Paragraph("Sin datos para los filtros elegidos.", chico)] + [""] * (len(tabla.columnas) - 1)
            )
        t = Table(datos, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3b57")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9aa7b4")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f6")]),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        historia = [
            Paragraph(_xml(tabla.titulo), estilos["Title"]),
            Paragraph(_xml(subtitulo), estilos["Normal"]),
            Spacer(1, 10),
            t,
            Spacer(1, 10),
        ]
        historia += [Paragraph(_xml(n), chico) for n in tabla.notas]
        doc.build(historia)


ESCRITORES: dict[str, Escritor] = {"CSV": EscritorCSV(), "PDF": EscritorPDF()}


class ArchivosReportes:
    def __init__(self, carpeta: Path):
        self._carpeta = carpeta

    def guardar(self, nombre_base: str, tabla: Tabla, formato: str, subtitulo: str) -> str:
        """Escribe el archivo y devuelve su clave (nombre relativo a la carpeta)."""
        escritor = ESCRITORES[formato]
        self._carpeta.mkdir(parents=True, exist_ok=True)
        clave = f"{_seguro(nombre_base)}.{escritor.extension}"
        temporal = self._carpeta / (clave + ".tmp")
        try:
            escritor.escribir(tabla, temporal, subtitulo)
            temporal.replace(
                self._carpeta / clave
            )  # un archivo a medio escribir nunca queda con el nombre final
        finally:
            temporal.unlink(missing_ok=True)
        return clave

    def borrar(self, clave: str) -> None:
        """Quita el archivo de un reporte que terminó en ERROR (por ejemplo, la sesión se revocó)."""
        ruta = self.ruta(clave)
        if ruta is not None:
            ruta.unlink(missing_ok=True)

    def ruta(self, clave: str) -> Path | None:
        ruta = (self._carpeta / clave).resolve()
        if (
            ruta.parent != self._carpeta.resolve() or not ruta.is_file()
        ):  # la clave no puede salir de la carpeta
            return None
        return ruta


def _seguro(nombre: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", nombre)[:150]


def _xml(texto: str) -> str:
    return str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
