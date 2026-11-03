"""Dominio de acceso: roles, permisos, sesión del usuario y reglas puras (sin I/O).

Las reglas vienen de AGENTS.md §11:
- Roles internos (ADMINISTRADOR, OPERACIONES, COMERCIAL) y externos (CLIENTE, OPERADOR_LOGISTICO) no se
  combinan en una misma cuenta.
- Un usuario interno no tiene tercero (`usuario.tercero_id IS NULL`); uno externo sí, y su empresa debe
  tener activo el rol comercial que corresponde (CLIENTE → CLIENTE, OPERADOR_LOGISTICO → OPERADOR).
- Los proveedores no tienen perfil externo.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from logistica.core.errors import BusinessRuleError, ValidationError


class Rol(StrEnum):
    ADMINISTRADOR = "ADMINISTRADOR"
    OPERACIONES = "OPERACIONES"
    COMERCIAL = "COMERCIAL"
    CLIENTE = "CLIENTE"
    OPERADOR_LOGISTICO = "OPERADOR_LOGISTICO"


class RolComercial(StrEnum):
    CLIENTE = "CLIENTE"
    PROVEEDOR = "PROVEEDOR"
    OPERADOR = "OPERADOR"


ROLES_INTERNOS = frozenset({Rol.ADMINISTRADOR, Rol.OPERACIONES, Rol.COMERCIAL})
ROLES_EXTERNOS = frozenset({Rol.CLIENTE, Rol.OPERADOR_LOGISTICO})
# Rol comercial que la empresa del usuario externo debe tener activo.
ROL_COMERCIAL_REQUERIDO = {Rol.CLIENTE: RolComercial.CLIENTE, Rol.OPERADOR_LOGISTICO: RolComercial.OPERADOR}


class Permiso(StrEnum):
    """Códigos sembrados en db/postgres/090_seed_roles_permisos.sql."""

    USUARIOS_ADMINISTRAR = "usuarios.administrar"
    SESIONES_ADMINISTRAR = "sesiones.administrar"
    TERCEROS_GESTIONAR = "terceros.gestionar"
    ENVIOS_CREAR = "envios.crear"
    ENVIOS_PLANIFICAR = "envios.planificar"
    ENVIOS_CONSULTAR = "envios.consultar"
    TRAMOS_ACTUALIZAR = "tramos.actualizar"
    MONITOREO_CONSULTAR = "monitoreo.consultar"
    ALERTAS_RECONOCER = "alertas.reconocer"
    ALERTAS_CERRAR = "alertas.cerrar"
    INCIDENTES_CREAR = "incidentes.crear"
    INCIDENTES_GESTIONAR = "incidentes.gestionar"
    CARGOS_GESTIONAR = "cargos.gestionar"
    FACTURAS_EMITIR = "facturas.emitir"
    FACTURAS_ANULAR = "facturas.anular"
    PAGOS_REGISTRAR = "pagos.registrar"
    PAGOS_APLICAR = "pagos.aplicar"
    PAGOS_ANULAR = "pagos.anular"
    CUENTAS_CONSULTAR = "cuentas.consultar"
    CUENTAS_CERRAR = "cuentas.cerrar"
    CONVERSACIONES_CREAR = "conversaciones.crear"
    REPORTES_OPERATIVOS = "reportes.operativos"
    REPORTES_COMERCIALES = "reportes.comerciales"
    PREDICCIONES_CONSULTAR = "predicciones.consultar"
    AUDITORIA_CONSULTAR = "auditoria.consultar"


# DECISIÓN DEL AGENTE (revisar): la matriz dice "Operaciones: consulta de terceros", pero no hay un permiso
# `terceros.consultar` sembrado. Consultar terceros se habilita a quien los gestiona o a quien crea y
# planifica envíos (necesita elegir cliente y operador).
PERMISOS_CONSULTA_TERCEROS = (Permiso.TERCEROS_GESTIONAR, Permiso.ENVIOS_CREAR, Permiso.ENVIOS_PLANIFICAR)


@dataclass(frozen=True)
class Principal:
    """Quién está operando. Se reconstruye desde PostgreSQL en cada operación (nunca se confía en caché)."""

    sesion_id: uuid.UUID
    usuario_id: uuid.UUID
    email: str
    nombre_completo: str
    tercero_id: uuid.UUID | None
    roles: frozenset[str]
    permisos: frozenset[str]

    @property
    def es_interno(self) -> bool:
        return self.tercero_id is None

    def tiene_permiso(self, *codigos: str) -> bool:
        """True si tiene al menos uno de los permisos indicados."""
        return any(c in self.permisos for c in codigos)

    def puede_ver_tercero(self, tercero_id: uuid.UUID) -> bool:
        """Alcance: un interno ve cualquier tercero; un externo solo su propia empresa."""
        return self.es_interno or self.tercero_id == tercero_id


# --------------------------------------------------------------------------- DTO de presentación
@dataclass(frozen=True)
class TerceroDTO:
    id: uuid.UUID
    razon_social: str
    pais_codigo: str
    tipo_identificacion: str
    numero_identificacion: str
    email: str | None
    telefono: str | None
    direccion: str | None
    activo: bool
    roles_comerciales: frozenset[str] = field(default_factory=frozenset)  # solo los activos


@dataclass(frozen=True)
class UsuarioDTO:
    id: uuid.UUID
    nombre: str
    apellido: str
    email: str
    tercero_id: uuid.UUID | None
    tercero_razon_social: str | None
    activo: bool
    roles: frozenset[str]


@dataclass(frozen=True)
class SesionDTO:
    id: uuid.UUID
    usuario_id: uuid.UUID
    usuario_email: str
    inicio_en: datetime
    vence_en: datetime
    fin_en: datetime | None
    estado: str
    cliente_descripcion: str | None
    es_actual: bool = False


# --------------------------------------------------------------------------- datos de entrada
@dataclass(frozen=True)
class TerceroDatos:
    razon_social: str
    pais_codigo: str
    tipo_identificacion: str
    numero_identificacion: str
    email: str | None = None
    telefono: str | None = None
    direccion: str | None = None

    def normalizado(self) -> TerceroDatos:
        """Valida y normaliza (recorta espacios, país en mayúsculas). Lanza ValidationError."""
        razon = self.razon_social.strip()
        pais = self.pais_codigo.strip().upper()
        tipo = self.tipo_identificacion.strip().upper()
        numero = self.numero_identificacion.strip()
        errores = []
        if not razon:
            errores.append("la razón social es obligatoria")
        elif len(razon) > 200:
            errores.append("la razón social admite hasta 200 caracteres")
        if not re.fullmatch(r"[A-Z]{2}", pais):
            errores.append("el país debe ser un código ISO de 2 letras (por ejemplo AR)")
        if not tipo or len(tipo) > 30:
            errores.append("el tipo de identificación es obligatorio (hasta 30 caracteres)")
        if not numero or len(numero) > 40:
            errores.append("el número de identificación es obligatorio (hasta 40 caracteres)")
        email = _opcional(self.email)
        if email is not None and not _email_valido(email):
            errores.append("el email no tiene un formato válido")
        telefono = _opcional(self.telefono)
        if telefono is not None and len(telefono) > 40:  # varchar(40) en el DDL
            errores.append("el teléfono admite hasta 40 caracteres")
        if errores:
            raise ValidationError(
                "; ".join(errores), user_message="Revise los datos: " + "; ".join(errores) + "."
            )
        return TerceroDatos(razon, pais, tipo, numero, email, telefono, _opcional(self.direccion))


@dataclass(frozen=True)
class UsuarioDatos:
    nombre: str
    apellido: str
    email: str
    tercero_id: uuid.UUID | None = None

    def normalizado(self) -> UsuarioDatos:
        nombre, apellido, email = self.nombre.strip(), self.apellido.strip(), self.email.strip().lower()
        errores = []
        if not nombre or not apellido:
            errores.append("nombre y apellido son obligatorios")
        elif len(nombre) > 100 or len(apellido) > 100:  # varchar(100) en el DDL
            errores.append("nombre y apellido admiten hasta 100 caracteres")
        if not _email_valido(email):
            errores.append("el email no tiene un formato válido")
        if errores:
            raise ValidationError(
                "; ".join(errores), user_message="Revise los datos: " + "; ".join(errores) + "."
            )
        return UsuarioDatos(nombre, apellido, email, self.tercero_id)


# --------------------------------------------------------------------------- reglas
# SUPUESTO (AGENTS.md §16): no hay política de contraseñas definida; se exige un largo mínimo.
LARGO_MINIMO_PASSWORD = 10


def validar_password(password: str) -> None:
    if len(password) < LARGO_MINIMO_PASSWORD:
        raise ValidationError(
            "contraseña corta",
            user_message=f"La contraseña debe tener al menos {LARGO_MINIMO_PASSWORD} caracteres.",
        )
    if password.strip() != password:
        raise ValidationError(
            "espacios", user_message="La contraseña no puede empezar ni terminar con espacios."
        )


def validar_roles_de_usuario(
    roles: set[str] | frozenset[str],
    tercero_id: uuid.UUID | None,
    roles_comerciales_activos: set[str] | frozenset[str],
) -> None:
    """Reglas de §11 sobre la combinación de roles de una cuenta. Lanza BusinessRuleError.

    `roles_comerciales_activos` son los del tercero del usuario (vacío si es interno).
    """
    desconocidos = set(roles) - {r.value for r in Rol}
    if desconocidos:
        raise ValidationError(f"roles desconocidos: {sorted(desconocidos)}")
    if not roles:
        raise BusinessRuleError("sin roles", user_message="El usuario debe tener al menos un rol.")
    internos = set(roles) & ROLES_INTERNOS
    externos = set(roles) & ROLES_EXTERNOS
    if internos and externos:
        raise BusinessRuleError(
            "roles mezclados", user_message="Un usuario no puede combinar roles internos con roles externos."
        )
    if internos and tercero_id is not None:
        raise BusinessRuleError(
            "interno con tercero", user_message="Un usuario interno no debe estar asociado a una empresa."
        )
    if externos:
        if tercero_id is None:
            raise BusinessRuleError(
                "externo sin tercero", user_message="Un usuario externo debe estar asociado a una empresa."
            )
        for rol in externos:
            requerido = ROL_COMERCIAL_REQUERIDO[Rol(rol)]
            if requerido not in roles_comerciales_activos:
                raise BusinessRuleError(
                    f"falta rol comercial {requerido}",
                    user_message=(
                        f"La empresa debe tener activo el rol comercial {requerido} para el rol {rol}."
                    ),
                )


def validar_roles_comerciales(roles: set[str] | frozenset[str]) -> None:
    """Un tercero tiene al menos un rol comercial y solo de los conocidos."""
    desconocidos = set(roles) - {r.value for r in RolComercial}
    if desconocidos:
        raise ValidationError(
            f"roles comerciales desconocidos: {sorted(desconocidos)}", user_message="Rol comercial inválido."
        )
    if not roles:
        raise BusinessRuleError(
            "sin roles comerciales", user_message="El tercero debe tener al menos un rol comercial."
        )


def roles_efectivos(
    roles: frozenset[str], tercero_id: uuid.UUID | None, roles_comerciales_activos: frozenset[str]
) -> frozenset[str]:
    """Roles que realmente habilitan permisos en este momento.

    Un interno usa solo roles internos. Un externo conserva cada rol externo mientras su empresa tenga activo
    el rol comercial requerido: si la empresa deja de ser cliente, el usuario cliente pierde ese rol al
    instante, sin que nadie edite su cuenta.
    """
    if tercero_id is None:
        return roles & ROLES_INTERNOS
    return frozenset(
        r for r in roles & ROLES_EXTERNOS if ROL_COMERCIAL_REQUERIDO[Rol(r)] in roles_comerciales_activos
    )


def _opcional(valor: str | None) -> str | None:
    if valor is None:
        return None
    valor = valor.strip()
    return valor or None


def _email_valido(email: str) -> bool:
    return len(email) <= 254 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None
