"""Chat nativo: una tarea por vista, polling sin bloquear y borradores reintentables."""

from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import Any, Callable
from uuid import UUID, uuid4

from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from logistica.domain.acceso import Principal
from logistica.domain.comunicacion import (
    ConversacionDTO, CursorMensaje, DestinatarioDTO, MensajeDTO, PaginaMensajesDTO,
    ParticipanteDTO, VistaChatDTO,
)
from logistica.domain.comunicacion import texto_validado
from logistica.core.errors import ValidationError
from logistica.services.comunicacion_service import ComunicacionService
from logistica.ui.widgets import (
    Campo, FormularioDialog, clave_seleccionada, fecha_local, llenar_tabla, tabla,
)
from logistica.ui.worker import WorkerPool

POLLING_MS = 3000  # Valor inicial para medir en la demostración; no es el scheduler general.


def indice_dato(combo: QComboBox, dato: object) -> int:
    # QVariant compara UUID Python por identidad, no por valor (relecturas crean objetos nuevos).
    return next((i for i in range(combo.count()) if combo.itemData(i) == dato), -1)


class ChatView(QWidget):
    sesion_vencida = Signal(str)

    def __init__(self, servicio: ComunicacionService, principal: Principal):
        super().__init__()
        self._servicio, self.principal = servicio, principal
        self._pool = WorkerPool(self)
        self._ocupado = False
        self._cerrando = False
        self._en_dialogo = False
        self._accion = ""
        self._pendiente: tuple[UUID, UUID, str] | None = None
        self._pagina: PaginaMensajesDTO | None = None
        self._hasta: datetime | None = None
        self._cursores: list[CursorMensaje | None] = [None]
        self._indice = 0
        self._conversaciones: dict[UUID, ConversacionDTO] = {}
        self._miembros: tuple[ParticipanteDTO, ...] = ()
        self._callback: Callable[[Any], None] | None = None

        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(self._cambiar)
        self.publicas = QComboBox()
        self.publicas.currentIndexChanged.connect(self._botones)
        self.tipo = QComboBox()
        self.tipo.addItem("PRIVADA")
        if principal.es_interno and principal.tiene_permiso("conversaciones.crear"):
            self.tipo.addItems(["GRUPAL", "PUBLICA"])
        self.actualizar = QPushButton("Actualizar")
        self.actualizar.clicked.connect(self.recargar)
        self.crear = QPushButton("Crear conversación")
        self.crear.clicked.connect(self._preparar_creacion)
        self.unirse = QPushButton("Incorporarme al canal interno")
        self.unirse.clicked.connect(self._unirse)
        self.agregar = QPushButton("Incorporar participante")
        self.agregar.clicked.connect(self._preparar_alta)
        self.retirar = QPushButton("Revocar participante")
        self.retirar.clicked.connect(self._retirar)
        self.archivar = QPushButton("Archivar")
        self.archivar.clicked.connect(self._archivar)
        self.participantes = tabla(("Participante", "Rol", "Ingreso", "Salida", "Vigente"))
        self.participantes.itemSelectionChanged.connect(self._botones)
        self.mensajes = tabla(("Fecha", "Remitente", "Texto", "Identificador"))
        self.texto = QPlainTextEdit()
        self.texto.setPlaceholderText("Mensaje de texto (hasta 4000 caracteres)")
        self.texto.setMaximumHeight(90)
        self.enviar = QPushButton("Enviar / reintentar")
        self.enviar.clicked.connect(self._enviar)
        self.descartar = QPushButton("Descartar borrador / confirmación pendiente")
        self.descartar.clicked.connect(self._descartar)
        self.anterior = QPushButton("Más recientes")
        self.anterior.clicked.connect(self._recientes)
        self.siguiente = QPushButton("Más antiguos")
        self.siguiente.clicked.connect(self._antiguos)
        self.estado = QLabel("")
        self.estado.setWordWrap(True)
        self.estado_envio = QLabel("Sin envío pendiente.")
        self.estado_envio.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Público = canal interno con incorporación explícita. "
            "El historial sólo incluye sus períodos de membresía."
        ))
        for widgets in (
            (self.selector, self.actualizar),
            (self.tipo, self.crear, self.publicas, self.unirse),
            (self.agregar, self.retirar, self.archivar),
        ):
            fila = QHBoxLayout()
            for widget in widgets:
                fila.addWidget(widget)
            layout.addLayout(fila)
        layout.addWidget(self.participantes, 1)
        layout.addWidget(self.mensajes, 3)
        fila = QHBoxLayout()
        fila.addWidget(self.anterior)
        fila.addWidget(self.siguiente)
        layout.addLayout(fila)
        layout.addWidget(self.texto)
        fila = QHBoxLayout()
        fila.addWidget(self.enviar)
        fila.addWidget(self.descartar)
        layout.addLayout(fila)
        layout.addWidget(self.estado_envio)
        layout.addWidget(self.estado)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(POLLING_MS)
        self._botones()

    def ocupado(self) -> bool:
        return self._ocupado

    def cerrar(self) -> None:
        """Cierre cooperativo: la ventana espera por timer, no en el hilo de Qt."""
        self._cerrando = True
        self._timer.stop()
        self._limpiar()
        self.texto.clear()
        self._botones()

    def trabajadores_activos(self) -> bool:
        return self._pool.active_count() > 0

    def _iniciar(self, funcion: Callable[[], Any], callback: Callable[[Any], None], accion: str) -> None:
        if self._cerrando or self._ocupado:
            return
        self._ocupado, self._callback, self._accion = True, callback, accion
        self.estado.setText("Consultando…" if accion == "leer" else "Procesando…")
        self._botones()
        self._pool.submit(
            funcion, self._ok, self._error, on_session_expired=self._vencida
        )

    @Slot(object)
    def _ok(self, resultado: Any) -> None:
        self._ocupado = False
        callback, self._callback = self._callback, None
        if self._cerrando:
            return
        self.estado.setText("Actualizado." if self._accion == "leer" else "Operación confirmada.")
        if callback:
            callback(resultado)
        self._botones()

    @Slot(str)
    def _error(self, mensaje: str) -> None:
        self._ocupado = False
        self._callback = None
        if self._cerrando:
            return
        self._limpiar()
        self._conversaciones = {}
        self.selector.blockSignals(True)
        self.selector.clear()
        self.selector.addItem("Seleccione conversación", None)
        self.selector.blockSignals(False)
        self.publicas.clear()
        self._indice, self._cursores = 0, [None]
        self.estado.setText(f"No se pudo completar. {mensaje} Se reintentará la consulta.")
        if self._accion == "enviar":
            self.estado_envio.setText(
                "Envío NO confirmado. Puede existir en MongoDB si falló la confirmación posterior. "
                "Reintente con este mismo borrador/UUID, sin duplicarlo."
            )
        self._botones()

    @Slot(str)
    def _vencida(self, mensaje: str) -> None:
        if self._cerrando:
            self._ocupado = False
            self._callback = None
            return
        self._error(mensaje)
        self.cerrar()
        self.sesion_vencida.emit(mensaje)

    def _limpiar(self) -> None:
        self.mensajes.setRowCount(0)
        self.participantes.setRowCount(0)
        self._pagina = None
        self._miembros = ()

    def _botones(self) -> None:
        libre = not self._ocupado and not self._cerrando
        conv = self._conversaciones.get(self.selector.currentData())
        es_admin = any(
            m.usuario_id == self.principal.usuario_id and m.vigente and m.rol == "ADMINISTRADOR"
            for m in self._miembros
        )
        gestionar = bool(
            conv and conv.tipo != "PRIVADA" and es_admin
            and self.principal.tiene_permiso("conversaciones.crear")
        )
        for widget in (self.selector, self.publicas, self.tipo, self.actualizar, self.crear):
            widget.setEnabled(libre)
        self.selector.setEnabled(libre and self._pendiente is None)
        self.unirse.setEnabled(libre and self.publicas.currentData() is not None)
        self.agregar.setEnabled(libre and gestionar)
        self.retirar.setEnabled(libre and gestionar and clave_seleccionada(self.participantes) is not None)
        self.archivar.setEnabled(libre and gestionar)
        self.enviar.setEnabled(libre and (conv is not None or self._pendiente is not None))
        self.descartar.setEnabled(libre and self._pendiente is not None)
        self.texto.setReadOnly(self._pendiente is not None or not libre)
        self.anterior.setEnabled(libre and self._indice > 0)
        self.siguiente.setEnabled(libre and self._pagina is not None and self._pagina.siguiente is not None)

    @Slot()
    def _poll(self) -> None:
        if self.isVisible() and not self._en_dialogo:
            self.recargar()

    def recargar(self) -> None:
        ident = self.selector.currentData()
        hasta = self._hasta if self._indice else None
        self._iniciar(
            partial(
                self._servicio.vista, self.principal.sesion_id, ident,
                antes=self._cursores[self._indice], hasta=hasta,
            ),
            self._mostrar, "leer",
        )

    def _mostrar(self, resultado: VistaChatDTO) -> None:
        seleccionado = self.selector.currentData()
        publica_seleccionada = self.publicas.currentData()
        self._conversaciones = {c.id: c for c in resultado.conversaciones}
        self.selector.blockSignals(True)
        self.selector.clear()
        self.selector.addItem("Seleccione conversación", None)
        for conv in resultado.conversaciones:
            self.selector.addItem(f"{conv.tipo} · {conv.nombre} · {str(conv.id)[:8]}", conv.id)
        self.selector.setCurrentIndex(max(0, indice_dato(self.selector, seleccionado)))
        self.selector.blockSignals(False)
        self.publicas.blockSignals(True)
        self.publicas.clear()
        for conv in resultado.publicas:
            if conv.id not in self._conversaciones:
                self.publicas.addItem(conv.nombre, conv.id)
        self.publicas.setCurrentIndex(max(0, indice_dato(self.publicas, publica_seleccionada)))
        self.publicas.blockSignals(False)
        self._limpiar()
        self._pagina = resultado.pagina
        if self._pagina:
            self._hasta = self._pagina.hasta
            self._miembros = self._pagina.membresias
            llenar_tabla(self.participantes, [
                (
                    m.nombre, m.rol, fecha_local(m.ingreso_en),
                    fecha_local(m.salida_en), "Sí" if m.vigente else "No",
                )
                for m in self._miembros
            ], [m.usuario_id for m in self._miembros])
            nombres = {m.usuario_id: m.nombre for m in self._miembros}
            llenar_tabla(self.mensajes, [
                (
                    fecha_local(m.fecha_envio), nombres.get(m.remitente_id, str(m.remitente_id)),
                    m.contenido, m.id,
                )
                for m in reversed(self._pagina.mensajes)
            ], [m.id for m in reversed(self._pagina.mensajes)])
        self._botones()

    @Slot()
    def _cambiar(self) -> None:
        self._indice, self._cursores = 0, [None]
        self._limpiar()
        self.recargar()

    @Slot()
    def _antiguos(self) -> None:
        if self._pagina and self._pagina.siguiente:
            self._cursores = self._cursores[:self._indice + 1] + [self._pagina.siguiente]
            self._indice += 1
            self.recargar()

    @Slot()
    def _recientes(self) -> None:
        if self._indice:
            self._indice -= 1
            self.recargar()

    @Slot()
    def _enviar(self) -> None:
        if self._pendiente is None:
            try:
                texto_validado(self.texto.toPlainText())
            except ValidationError as exc:
                self.estado_envio.setText(exc.user_message)
                return
            self._pendiente = (self.selector.currentData(), uuid4(), self.texto.toPlainText())
        ident, mensaje_id, contenido = self._pendiente
        self.estado_envio.setText(f"Enviando {mensaje_id}; todavía no confirmado.")
        self._iniciar(
            partial(self._servicio.enviar, self.principal.sesion_id, ident, mensaje_id, contenido),
            self._enviado, "enviar",
        )

    def _enviado(self, mensaje: MensajeDTO) -> None:
        self.estado_envio.setText(f"Persistencia confirmada: {mensaje.id}")
        self._pendiente = None
        self.texto.clear()
        self._indice, self._cursores = 0, [None]
        self.recargar()

    @Slot()
    def _descartar(self) -> None:
        self._pendiente = None
        self.texto.clear()
        self.estado_envio.setText(
            "Borrador descartado. Esto NO borra un mensaje que ya pudiera estar persistido."
        )
        self._botones()

    @Slot()
    def _preparar_creacion(self) -> None:
        self._iniciar(
            partial(
                self._servicio.destinatarios, self.principal.sesion_id,
                grupo=self.tipo.currentText() != "PRIVADA",
            ),
            self._form_creacion, "directorio",
        )

    def _form_creacion(self, usuarios: tuple[DestinatarioDTO, ...]) -> None:
        tipo = self.tipo.currentText()
        if tipo == "PRIVADA" and not usuarios:
            self.estado.setText("No hay otros usuarios habilitados en su alcance. Créelos desde Usuarios.")
            return
        campos = [Campo("nombre", "Nombre (opcional en privado)", obligatorio=tipo != "PRIVADA")]
        if tipo == "PRIVADA":
            campos.append(Campo(
                "usuario", "Destinatario", "opciones", opciones=[(u.nombre, u.id) for u in usuarios],
            ))
        dialogo = FormularioDialog(self, "Crear conversación", campos)
        self._en_dialogo = True
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        self._en_dialogo = False
        if aceptado and not self._cerrando:
            valores = dialogo.valores()
            participantes = (valores["usuario"],) if tipo == "PRIVADA" else ()
            self._iniciar(
                partial(
                    self._servicio.crear, self.principal.sesion_id, tipo, valores["nombre"], participantes,
                ),
                self._creada, "crear",
            )
        dialogo.deleteLater()

    def _creada(self, _ident: UUID) -> None:
        self.recargar()

    @Slot()
    def _preparar_alta(self) -> None:
        self._iniciar(
            partial(self._servicio.destinatarios, self.principal.sesion_id, grupo=True),
            self._form_alta, "directorio",
        )

    def _form_alta(self, usuarios: tuple[DestinatarioDTO, ...]) -> None:
        vigentes = {m.usuario_id for m in self._miembros if m.vigente}
        opciones = [(u.nombre, u.id) for u in usuarios if u.id not in vigentes]
        if not opciones:
            self.estado.setText("No hay participantes disponibles para incorporar.")
            return
        dialogo = FormularioDialog(self, "Incorporar participante", [
            Campo("usuario", "Usuario interno", "opciones", opciones=opciones),
            Campo("rol", "Rol", "opciones", opciones=[(r, r) for r in ("MIEMBRO", "ADMINISTRADOR")]),
        ])
        self._en_dialogo = True
        aceptado = dialogo.exec() == QDialog.DialogCode.Accepted
        self._en_dialogo = False
        if aceptado and not self._cerrando:
            datos = dialogo.valores()
            self._iniciar(
                partial(
                    self._servicio.incorporar, self.principal.sesion_id,
                    self.selector.currentData(), datos["usuario"], datos["rol"],
                ), self._cambiada, "incorporar",
            )
        dialogo.deleteLater()

    def _cambiada(self, _resultado: Any) -> None:
        self.recargar()

    @Slot()
    def _unirse(self) -> None:
        self._iniciar(
            partial(self._servicio.unirse, self.principal.sesion_id, self.publicas.currentData()),
            self._cambiada, "unirse",
        )

    @Slot()
    def _retirar(self) -> None:
        self._iniciar(
            partial(
                self._servicio.retirar, self.principal.sesion_id, self.selector.currentData(),
                clave_seleccionada(self.participantes),
            ), self._cambiada, "retirar",
        )

    @Slot()
    def _archivar(self) -> None:
        self._iniciar(
            partial(self._servicio.archivar, self.principal.sesion_id, self.selector.currentData()),
            self._cambiada, "archivar",
        )
