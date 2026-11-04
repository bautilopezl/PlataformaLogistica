"""Piezas comunes de la UI: vista base con trabajadores y tablas de solo lectura."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable, Sequence

import shiboken6
from PySide6.QtCore import QDateTime, QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from logistica.core.errors import LogisticaError
from logistica.ui.worker import WorkerPool

# Espera máxima al cerrar una vista con operaciones en curso (los servicios tienen sus propios timeouts).
SHUTDOWN_WAIT_MS = 10_000


class _Llamada(QObject):
    """Una operación en curso de una vista. Es un QObject del hilo gráfico: Qt le entrega ahí el resultado,
    y cada llamada conoce su propio callback (dos operaciones simultáneas no se cruzan).

    Vida del objeto: NO es hija de la vista ni usa deleteLater. La retiene el `WorkerPool` hasta que entregó
    su resultado y la suelta en el hilo gráfico. (Con deleteLater pendiente, si Python destruía la vista
    en ese
    momento, Qt abortaba la aplicación: se reprodujo en tests/test_fase2_ui.py.) Si la vista ya no existe
    cuando llega el resultado, se descarta.
    """

    def __init__(self, vista: VistaBase, on_ok: Callable[[Any], None]):
        super().__init__()
        self._vista, self._on_ok = vista, on_ok

    def _viva(self) -> bool:
        return shiboken6.isValid(self._vista) and not self._vista._cerrando

    @Slot(object)
    def ok(self, resultado: Any) -> None:
        if not self._viva():
            return
        self._vista._terminar("", "ok")
        self._on_ok(resultado)
        self._vista.actualizar_botones()

    @Slot(str)
    def error(self, mensaje: str) -> None:
        if not self._viva():
            return
        self._vista._terminar(mensaje, "error")
        if self._vista.isVisible():
            QMessageBox.warning(self._vista, "No se pudo completar la operación", mensaje)
        self._vista.actualizar_botones()

    @Slot(str)
    def vencida(self, mensaje: str) -> None:
        if not self._viva():
            return
        self._vista._terminar(mensaje, "error")
        self._vista.sesion_vencida.emit(mensaje)


class VistaBase(QWidget):
    """Base de las pantallas: ejecuta servicios fuera del hilo gráfico y centraliza el manejo de errores.

    `ejecutar(fn, on_ok, *args)` corre `fn(*args)` en un trabajador. Si falla, muestra el mensaje en
    `self.estado` y en un diálogo; si la sesión venció, emite `sesion_vencida` para que la ventana principal
    vuelva al login.
    """

    sesion_vencida = Signal(str)

    def __init__(self, sesion_id: uuid.UUID, parent: QWidget | None = None):
        super().__init__(parent)
        self.sesion_id = sesion_id
        self.estado = QLabel("")
        self.estado.setWordWrap(True)
        self._pool = WorkerPool(self)
        self._en_curso = 0
        self._cerrando = False

    def ejecutar(self, fn: Callable[..., Any], on_ok: Callable[[Any], None], *args: Any) -> None:
        if self._cerrando:
            return
        self._en_curso += 1
        self._set_estado("Procesando…", "pendiente")
        self.actualizar_botones()
        llamada = _Llamada(self, on_ok)
        self._pool.submit(
            fn, llamada.ok, llamada.error, *args, on_session_expired=llamada.vencida, retener=llamada
        )

    def ocupado(self) -> bool:
        return self._en_curso > 0

    def cerrar(self) -> None:
        """No acepta trabajo nuevo y espera a lo que está en curso."""
        self._cerrando = True
        self._pool.wait_for_done(SHUTDOWN_WAIT_MS)

    def actualizar_botones(self) -> None:
        """Las subclases habilitan o deshabilitan sus botones según `ocupado()` y la selección."""

    def _terminar(self, texto: str, estado: str) -> None:
        self._en_curso -= 1
        self._set_estado(texto, estado)

    def _set_estado(self, texto: str, estado: str) -> None:
        self.estado.setText(texto)
        self.estado.setProperty("estado", estado)
        self.estado.style().unpolish(self.estado)
        self.estado.style().polish(self.estado)


def tabla(columnas: Sequence[str]) -> QTableWidget:
    t = QTableWidget(0, len(columnas))
    t.setHorizontalHeaderLabels(list(columnas))
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def llenar_tabla(t: QTableWidget, filas: Sequence[Sequence[Any]], claves: Sequence[Any]) -> None:
    """Carga filas de texto y guarda en cada fila su clave (por ejemplo el id) para recuperarla al elegir."""
    t.clearSelection()
    t.setRowCount(len(filas))
    for i, (fila, clave) in enumerate(zip(filas, claves)):
        for j, valor in enumerate(fila):
            item = QTableWidgetItem("" if valor is None else str(valor))
            if j == 0:
                item.setData(Qt.ItemDataRole.UserRole, clave)
            t.setItem(i, j, item)


def clave_seleccionada(t: QTableWidget) -> Any | None:
    """Clave de la fila SELECCIONADA (no la fila 'actual', que puede quedar puesta tras recargar)."""
    filas = t.selectionModel().selectedRows()
    fila = filas[0].row() if filas else -1
    if fila < 0 or t.item(fila, 0) is None:
        return None
    return t.item(fila, 0).data(Qt.ItemDataRole.UserRole)


def indice_dato(combo: QComboBox, dato: object) -> int:
    """Índice del elemento cuyo dato es `dato`, o -1. QVariant compara los UUID de Python por identidad, no por
    valor (cada relectura crea objetos nuevos), por eso no se usa `findData`."""
    return next((i for i in range(combo.count()) if combo.itemData(i) == dato), -1)


def si_no(valor: bool) -> str:
    return "Sí" if valor else "No"


# --------------------------------------------------------------------------- formularios
class Campo:
    """Un campo de `FormularioDialog`: texto, opciones (combo), número, fecha y hora, o casillas."""

    def __init__(
        self,
        clave: str,
        etiqueta: str,
        tipo: str = "texto",
        *,
        opciones: Sequence[tuple[str, Any]] = (),
        valor: Any = None,
        obligatorio: bool = False,
    ):
        self.clave, self.etiqueta, self.tipo = clave, etiqueta, tipo
        self.opciones, self.valor, self.obligatorio = list(opciones), valor, obligatorio


class FormularioDialog(QDialog):
    """Diálogo genérico de alta/edición.

    `validar(valores)` recibe un dict con los valores y debe lanzar `ValidationError`/`BusinessRuleError` con
    un mensaje claro si algo está mal (normalmente llama al `normalizado()` del dominio: mismas reglas que el
    servicio, para avisar antes de enviar). El servicio vuelve a validar igual.
    """

    def __init__(
        self,
        parent: QWidget | None,
        titulo: str,
        campos: Sequence[Campo],
        validar: Callable[[dict[str, Any]], None] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(titulo)
        self._campos = list(campos)
        self._validar = validar
        self._widgets: dict[str, QWidget] = {}
        form = QFormLayout()
        for c in self._campos:
            w = _crear_widget(c)
            self._widgets[c.clave] = w
            form.addRow(c.etiqueta + (" *" if c.obligatorio else ""), w)
        self.error = QLabel("")
        self.error.setProperty("estado", "error")
        self.error.setWordWrap(True)
        botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        botones.accepted.connect(self._aceptar)
        botones.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(botones)

    def valores(self) -> dict[str, Any]:
        return {c.clave: _leer_widget(c, self._widgets[c.clave]) for c in self._campos}

    def widget(self, clave: str) -> QWidget:
        return self._widgets[clave]

    def _aceptar(self) -> None:
        valores = self.valores()
        faltan = [c.etiqueta for c in self._campos if c.obligatorio and valores[c.clave] in (None, "", set())]
        if faltan:
            self.error.setText("Complete: " + ", ".join(faltan) + ".")
            return
        if self._validar is not None:
            try:
                self._validar(valores)
            except LogisticaError as exc:
                self.error.setText(exc.user_message)
                return
        self.accept()


def pedir(
    parent: QWidget | None,
    titulo: str,
    campos: Sequence[Campo],
    validar: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """Muestra un `FormularioDialog` modal; devuelve los valores o None si se canceló."""
    dialogo = FormularioDialog(parent, titulo, campos, validar)
    aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
    valores = dialogo.valores() if aceptado else None
    dialogo.deleteLater()  # se destruye en el hilo gráfico (ver ui/gc_guard.py)
    return valores


def _crear_widget(c: Campo) -> QWidget:
    if c.tipo == "opciones":
        w = QComboBox()
        for texto, dato in c.opciones:
            w.addItem(texto, dato)
        if c.valor is not None:
            w.setCurrentIndex(max(0, w.findData(c.valor)))
        return w
    if c.tipo == "fecha_hora":
        w = QDateTimeEdit()
        w.setCalendarPopup(True)
        w.setDisplayFormat("dd/MM/yyyy HH:mm")
        valor = c.valor or datetime.now().astimezone()
        w.setDateTime(QDateTime(valor.astimezone()))  # se muestra en hora local
        return w
    if c.tipo == "casillas":
        w = QWidget()
        fila = QHBoxLayout(w)
        fila.setContentsMargins(0, 0, 0, 0)
        for texto, dato in c.opciones:
            check = QCheckBox(texto)
            check.setProperty("dato", dato)
            check.setChecked(dato in (c.valor or ()))
            fila.addWidget(check)
        return w
    w = QLineEdit("" if c.valor is None else str(c.valor))
    return w


def _leer_widget(c: Campo, w: QWidget) -> Any:
    if c.tipo == "opciones":
        return w.currentData()
    if c.tipo == "fecha_hora":
        return w.dateTime().toPython().astimezone()  # hora local con zona → el servicio la guarda en UTC
    if c.tipo == "casillas":
        return {ch.property("dato") for ch in w.findChildren(QCheckBox) if ch.isChecked()}
    return w.text().strip()


def fecha_local(dt: datetime | None) -> str:
    """Se guarda en UTC; se muestra en la hora local del equipo."""
    return dt.astimezone().strftime("%d/%m/%Y %H:%M") if dt else ""
