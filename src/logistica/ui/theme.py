"""Tema visual común de la aplicación (hoja de estilos Qt).

Se aplica una vez sobre la `QApplication`. Los widgets marcan su estado con la propiedad dinámica
`estado` ("ok", "error", "pendiente") y la hoja de estilos les da color.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

PRIMARY = "#1f5f8b"
PRIMARY_DARK = "#174a6c"
OK = "#2e7d32"
ERROR = "#c62828"
PENDING = "#8a8a8a"

STYLESHEET = f"""
QWidget {{
    font-size: 10pt;
}}
QLabel#titulo {{
    font-size: 14pt;
    font-weight: 600;
    color: {PRIMARY};
}}
QPushButton {{
    background-color: {PRIMARY};
    color: white;
    border: none;
    border-radius: 4px;
    padding: 6px 14px;
}}
QPushButton:hover {{
    background-color: {PRIMARY_DARK};
}}
QPushButton:disabled {{
    background-color: {PENDING};
}}
QLabel[estado="ok"] {{
    color: {OK};
    font-weight: 600;
}}
QLabel[estado="error"] {{
    color: {ERROR};
    font-weight: 600;
}}
QLabel[estado="pendiente"] {{
    color: {PENDING};
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
