"""Registro de logs: consola + archivo rotativo, sin credenciales.

Los mensajes de error de los drivers pueden incluir cadenas de conexión. Por eso todos los handlers pasan
por `RedactSecretsFilter`, que reemplaza contraseñas por `***` antes de escribir.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(threadName)s] %(message)s"

# Formas en que una contraseña puede aparecer en un mensaje de driver, una conninfo, un .env o un dict:
#   password=xxx · password: xxx · 'password': 'xxx' · XXX_PASSWORD=xxx · PASSWORD 'xxx' (SQL)
#   usuario:clave@host en URIs (la clave puede contener '@': se toma hasta el último '@' antes del host).
_VALUE = r"""("[^"]*"|'[^']*'|\S+)"""
_PATTERNS = (
    (re.compile(r"""(\w*password\w*["']?\s*[=:]\s*)""" + _VALUE, re.IGNORECASE), r"\1***"),
    (re.compile(r"(\bPASSWORD\s+)'[^']*'", re.IGNORECASE), r"\1'***'"),
    (re.compile(r"(\w+://[^:/\s@]+:)[^/\s]+(@[^@/\s]+)"), r"\1***\2"),
    # Hashes de contraseñas (argon2): no son la clave, pero permiten atacarla por fuerza bruta.
    (re.compile(r"\$argon2[^\s'\"]*"), "$argon2***"),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Oculta contraseñas en el mensaje y en la traza de la excepción del registro."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # Argumentos que no coinciden con el formato: no se rompe a quien llamó al log.
            message = f"{record.msg} {record.args}"
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            # Se formatea la traza acá para poder limpiarla; el Formatter usa exc_text si ya existe.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_logistica", False) for h in root.handlers):
        return  # ya configurado
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT)
    redactor = RedactSecretsFilter()

    console = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        log_dir / "logistica.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    for handler in (console, file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
        handler._logistica = True  # type: ignore[attr-defined]
        root.addHandler(handler)
