"""Archivos de reportes en disco (CSV con `csv`, PDF con `reportlab`).

Patrón Strategy: un escritor por formato con la misma interfaz `escribir(tabla, ruta)`. El servicio no sabe
cómo se arma cada formato; solo pide "guardá esta tabla como PDF". SUPUESTO R1 (AGENTS.md §16.7): carpeta
local por equipo (`REPORTES_DIR`). El registro en `reporte` guarda solo la clave del archivo; si en otro
equipo no está, se vuelve a generar desde los parámetros registrados.
"""

from __future__ import annotations

import csv
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from logistica.domain.analitica import Tabla, celda, celda_csv
from logistica.domain.comercial import formato_dinero


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


# =========================================================================== comprobantes de factura
LEYENDA_SIN_VALIDEZ = (
    "DOCUMENTO ACADÉMICO / INTERNO SIN VALIDEZ FISCAL. No es una factura electrónica ni un comprobante "
    "autorizado por ARCA; representa los datos registrados en la plataforma."
)


def _fecha(dt: Any) -> str:
    return dt.astimezone().strftime("%d/%m/%Y %H:%M") if dt else "-"


def _cantidad(valor: Decimal) -> str:
    texto = f"{valor:,.3f}".rstrip("0").rstrip(".")
    return texto or "0"


def _precio(valor: Decimal) -> str:
    """Hasta 4 decimales (así se guarda el precio unitario), nunca menos de 2."""
    texto = f"{valor:,.4f}"
    while texto.endswith("0") and len(texto.split(".")[1]) > 2:
        texto = texto[:-1]
    return texto


class EscritorComprobantePDF:
    """Comprobante en A4 vertical con `reportlab` (ya usado por los reportes). Las tablas se parten por fila
    entre páginas y repiten su encabezado; cada página lleva la leyenda de documento sin validez fiscal."""

    def escribir(self, c: Any, ruta: Path) -> None:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        f = c.factura
        venta = f.sentido == "VENTA"
        estilos = getSampleStyleSheet()
        normal = estilos["BodyText"].clone("normal_c", fontSize=9, leading=11.5)
        chico = estilos["BodyText"].clone("chico_c", fontSize=8, leading=10)
        derecha = chico.clone("derecha_c", alignment=2)
        encabezado = chico.clone("encabezado_c", textColor=colors.white)
        encabezado_normal = normal.clone("encabezado_n", textColor=colors.white)
        titulo = estilos["Title"].clone("titulo_c", fontSize=16, leading=20, spaceAfter=4)
        seccion = estilos["Heading3"].clone("seccion_c", fontSize=10.5, spaceBefore=8, spaceAfter=4)
        azul, gris = colors.HexColor("#1f3b57"), colors.HexColor("#9aa7b4")
        p = lambda texto, estilo=normal: Paragraph(_xml(texto), estilo)  # noqa: E731
        pb = lambda texto, estilo=normal: Paragraph(f"<b>{_xml(texto)}</b>", estilo)  # noqa: E731

        def estilo_tabla(con_encabezado: bool = True) -> TableStyle:
            reglas = [
                ("GRID", (0, 0), (-1, -1), 0.25, gris),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
            if con_encabezado:
                reglas += [
                    ("BACKGROUND", (0, 0), (-1, 0), azul),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f6")]),
                ]
            return TableStyle(reglas)

        tipo = "Factura de VENTA (a cobrar al cliente)" if venta else "Factura de COMPRA (a pagar al proveedor)"
        emisor = c.emisor
        plataforma = (
            [emisor.razon_social, emisor.identificacion or "Identificación no configurada",
             emisor.domicilio or "Domicilio no configurado"]
            if emisor.configurado
            else ["Plataforma de logística internacional", "Emisor no configurado (EMISOR_RAZON_SOCIAL)", ""]
        )
        k = c.contraparte
        contraparte = [
            k.razon_social,
            f"{k.tipo_identificacion} {k.numero_identificacion} ({k.pais_codigo})",
            k.direccion or "Domicilio no registrado",
        ]
        emite, recibe = (plataforma, contraparte) if venta else (contraparte, plataforma)
        partes = Table(
            [
                [pb("Emisor", encabezado_normal), pb("Cliente" if venta else "Receptor (nuestra empresa)", encabezado_normal)],
                [
                    Paragraph("<br/>".join(_xml(x) for x in emite if x), normal),
                    Paragraph("<br/>".join(_xml(x) for x in recibe if x), normal),
                ],
            ],
            colWidths=[267, 267],
        )
        partes.setStyle(estilo_tabla())

        envios = ", ".join(sorted(set(c.envio_codigos.values()))) or "Sin envío asociado"
        datos = Table(
            [
                [pb("Tipo", chico), p(tipo, chico), pb("Estado", chico), p(f.estado, chico)],
                [pb("Número", chico), p(f.codigo, chico), pb("Moneda", chico), p(f.moneda_codigo, chico)],
                [pb("Emitida", chico), p(_fecha(f.emitida_en), chico), pb("Vence", chico),
                 p(_fecha(f.vence_en), chico)],
                [pb("Envío(s)", chico), p(envios, chico), pb("Comprobante", chico), p(f.tipo_comprobante, chico)],
            ],
            colWidths=[62, 205, 62, 205],
        )
        datos.setStyle(estilo_tabla(con_encabezado=False))

        filas = [[pb(x, encabezado) for x in ("#", "Concepto", "Envío", "Cantidad", "Precio unitario", "Importe")]]
        for r in f.renglones:
            filas.append(
                [
                    p(str(r.renglon), chico),
                    p(r.concepto, chico),
                    p(c.envio_codigos.get(r.envio_id, "-") if r.envio_id else "-", chico),
                    p(_cantidad(r.cantidad), derecha),
                    p(_precio(r.precio_unitario), derecha),
                    p(formato_dinero(r.subtotal), derecha),
                ]
            )
        conceptos = Table(filas, colWidths=[22, 190, 98, 56, 80, 88], repeatRows=1, splitByRow=1)
        conceptos.setStyle(estilo_tabla())
        total = Table(
            [[pb(f"Importe original de la factura ({f.moneda_codigo})"), pb(formato_dinero(f.importe_total),
                                                                            derecha)]],
            colWidths=[446, 88],
        )
        total.setStyle(estilo_tabla(con_encabezado=False))

        accion = "cobranza" if venta else "pago"
        vigentes = [a for a in c.aplicaciones if a.anulada_en is None]
        aplicado = sum((a.monto_aplicado for a in vigentes), Decimal("0"))
        saldo = f.importe_total - aplicado if f.estado == "EMITIDA" else Decimal("0")
        estado_cobranza = Table(
            [
                [p("Importe original"), p(formato_dinero(f.importe_total), derecha)],
                [p("Cobrado/aplicado" if venta else "Pagado/aplicado"), p(formato_dinero(aplicado), derecha)],
                [pb("Saldo pendiente" if f.estado == "EMITIDA" else "Saldo pendiente (factura anulada)"),
                 pb(formato_dinero(saldo), derecha)],
            ],
            colWidths=[446, 88],
        )
        estado_cobranza.setStyle(estilo_tabla(con_encabezado=False))

        historia = [
            p(tipo.split(" (")[0] + f" {f.codigo}", titulo),
            Table([[Paragraph(f"<b>{_xml(LEYENDA_SIN_VALIDEZ)}</b>", chico)]], colWidths=[534],
                  style=[("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#b3261e")),
                         ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fdecea"))]),
            Spacer(1, 8),
            partes,
            Spacer(1, 6),
            datos,
            p("Conceptos facturados", seccion),
            conceptos,
            Spacer(1, 4),
            total,
            KeepTogether([
                p(f"Estado de {accion} al {_fecha(c.generado_en)} (no altera el importe original)", seccion),
                estado_cobranza,
            ]),
        ]
        if c.aplicaciones:
            filas_ap = [[pb(x, encabezado) for x in ("Fecha", "Importe aplicado", "Estado")]]
            for a in c.aplicaciones:
                estado = "Vigente" if a.anulada_en is None else (
                    f"Anulada el {_fecha(a.anulada_en)}: {a.motivo_anulacion or ''}")
                filas_ap.append([p(_fecha(a.creado_en), chico), p(formato_dinero(a.monto_aplicado), derecha),
                                 p(estado, chico)])
            aplicaciones = Table(filas_ap, colWidths=[110, 100, 324], repeatRows=1, splitByRow=1)
            aplicaciones.setStyle(estilo_tabla())
            historia += [p("Aplicaciones de " + ("cobros" if venta else "pagos"), seccion), aplicaciones]
        if f.estado == "ANULADA":
            historia += [
                p("Anulación", seccion),
                p(f"Anulada el {_fecha(f.anulada_en)}. Motivo: {f.motivo_anulacion or '-'}"),
            ]
        historia += [
            Spacer(1, 10),
            p(f"Generado el {_fecha(c.generado_en)} por {c.generado_por}. Importes según lo registrado al "
              "emitir la factura; no se recalculan con precios actuales.", chico),
        ]

        def pie(canvas: Any, doc: Any) -> None:
            canvas.saveState()
            canvas.setFont("Helvetica", 7)
            canvas.setFillColor(colors.HexColor("#b3261e"))
            canvas.drawString(30, 20, "Documento académico/interno sin validez fiscal - no autorizado por ARCA")
            canvas.setFillColor(colors.black)
            canvas.drawRightString(A4[0] - 30, 20, f"{f.codigo} - página {doc.page}")
            canvas.restoreState()

        SimpleDocTemplate(
            str(ruta),
            pagesize=A4,
            title=f"{tipo.split(' (')[0]} {f.codigo}",
            leftMargin=30,
            rightMargin=30,
            topMargin=30,
            bottomMargin=36,
        ).build(historia, onFirstPage=pie, onLaterPages=pie)


class ArchivosComprobantes:
    """Carpeta local de los comprobantes PDF (`REPORTES_DIR/comprobantes`). Misma escritura atómica que los
    reportes: un archivo a medio escribir nunca queda con su nombre final."""

    def __init__(self, carpeta: Path, escritor: EscritorComprobantePDF | None = None):
        self._carpeta = carpeta
        self._escritor = escritor or EscritorComprobantePDF()

    def guardar(self, comprobante: Any) -> Path:
        f = comprobante.factura
        self._carpeta.mkdir(parents=True, exist_ok=True)
        nombre = _seguro(
            f"{f.sentido.lower()}_{f.codigo}_{comprobante.generado_en.astimezone():%Y%m%d_%H%M%S}"
        ) + ".pdf"
        temporal = self._carpeta / (nombre + ".tmp")
        try:
            self._escritor.escribir(comprobante, temporal)
            temporal.replace(self._carpeta / nombre)
        finally:
            temporal.unlink(missing_ok=True)
        return (self._carpeta / nombre).resolve()
