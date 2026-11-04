"""Configuración de la aplicación, leída de variables de entorno (.env).

Las credenciales nunca se guardan en el código. La aplicación usa la identidad de
ejecución (PG_APP_USER); las migraciones y el setup usan la de administración.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Falta la variable de entorno obligatoria {name}")
    return value or ""


@dataclass(frozen=True)
class PostgresConfig:
    host: str
    port: int
    database: str
    user: str
    password: str

    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    def conninfo(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.database} "
            f"user={self.user} password={self.password}"
        )


@dataclass(frozen=True)
class MongoConfig:
    host: str
    port: int
    database: str
    user: str
    password: str

    def uri(self) -> str:
        return (
            f"mongodb://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/?authSource=admin"
        )


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    user: str
    password: str


@dataclass(frozen=True)
class AppConfig:
    postgres_admin: PostgresConfig
    postgres_app: PostgresConfig
    mongo: MongoConfig
    neo4j: Neo4jConfig
    log_level: str
    log_dir: Path
    # Tiempo máximo (segundos) para conectar o elegir servidor en cada motor. SUPUESTO (AGENTS.md §16.6):
    # 5 s hasta medirlo en el entorno de demostración.
    db_timeout_s: float = 5.0
    security: SecurityConfig = field(default_factory=lambda: SecurityConfig())
    # Carpeta local de los archivos de reportes (fase 7). SUPUESTO R1 (AGENTS.md §16.7): cada equipo guarda
    # los suyos; en otro equipo un reporte se vuelve a generar desde sus parámetros registrados.
    reportes_dir: Path = PROJECT_ROOT / "reportes"


@dataclass(frozen=True)
class SecurityConfig:
    """Parámetros de sesión y de argon2. SUPUESTOS (AGENTS.md §16.6) hasta medirlos en la demostración."""

    session_hours: float = 8.0
    # None = valor por defecto de argon2-cffi (perfil RFC 9106 de baja memoria).
    argon2_time_cost: int | None = None
    argon2_memory_kib: int | None = None
    argon2_parallelism: int | None = None


def _optional_int(name: str) -> int | None:
    value = _env(name)
    return int(value) if value else None


def load_config(env_file: str | os.PathLike | None = None) -> AppConfig:
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    pg_host = _env("PG_HOST", "localhost")
    pg_port = int(_env("PG_PORT", "5432"))
    pg_db = _env("PG_DB", "logistica")
    admin = PostgresConfig(
        pg_host,
        pg_port,
        pg_db,
        _env("PG_ADMIN_USER", "logistica_admin"),
        _env("PG_ADMIN_PASSWORD", required=True),
    )
    # Si no hay identidad de aplicación configurada se cae a la de administración
    # (solo aceptable en desarrollo).
    app_user = _env("PG_APP_USER") or admin.user
    app_password = _env("PG_APP_PASSWORD") or admin.password
    app = PostgresConfig(pg_host, pg_port, pg_db, app_user, app_password)
    mongo = MongoConfig(
        _env("MONGO_HOST", "localhost"),
        int(_env("MONGO_PORT", "27017")),
        _env("MONGO_DB", "logistica"),
        _env("MONGO_ADMIN_USER", "logistica_admin"),
        _env("MONGO_ADMIN_PASSWORD", required=True),
    )
    neo = Neo4jConfig(
        _env("NEO4J_URI", "bolt://localhost:7687"),
        _env("NEO4J_USER", "neo4j"),
        _env("NEO4J_PASSWORD", required=True),
    )
    log_dir = Path(_env("LOG_DIR", "logs"))
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir
    timeout = float(_env("DB_TIMEOUT_S", "5"))
    if not timeout > 0:  # también rechaza NaN
        raise RuntimeError("DB_TIMEOUT_S debe ser mayor que cero")
    security = SecurityConfig(
        session_hours=float(_env("SESSION_HOURS", "8")),
        argon2_time_cost=_optional_int("ARGON2_TIME_COST"),
        argon2_memory_kib=_optional_int("ARGON2_MEMORY_KIB"),
        argon2_parallelism=_optional_int("ARGON2_PARALLELISM"),
    )
    if not security.session_hours > 0:  # también rechaza NaN
        raise RuntimeError("SESSION_HOURS debe ser mayor que cero")
    reportes_dir = Path(_env("REPORTES_DIR", "reportes"))
    if not reportes_dir.is_absolute():
        reportes_dir = PROJECT_ROOT / reportes_dir
    return AppConfig(admin, app, mongo, neo, _env("LOG_LEVEL", "INFO").upper(), log_dir, timeout, security,
                     reportes_dir)
