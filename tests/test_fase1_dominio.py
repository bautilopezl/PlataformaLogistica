"""Fase 1 — reglas puras de acceso (sin servidores)."""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from logistica.core.errors import BusinessRuleError, ValidationError  # noqa: E402
from logistica.domain.acceso import (  # noqa: E402
    Permiso,
    Principal,
    Rol,
    TerceroDatos,
    UsuarioDatos,
    validar_password,
    validar_roles_de_usuario,
)
from logistica.domain.passwords import Hasher  # noqa: E402

EMPRESA = uuid.uuid4()


# --------------------------------------------------------------------------- combinación de roles (§11)
@pytest.mark.parametrize("roles", [{"ADMINISTRADOR"}, {"OPERACIONES", "COMERCIAL"}])
def test_roles_internos_sin_empresa_son_validos(roles):
    validar_roles_de_usuario(roles, None, set())


def test_no_se_combinan_roles_internos_y_externos():
    with pytest.raises(BusinessRuleError, match="mezclados"):
        validar_roles_de_usuario({"OPERACIONES", "CLIENTE"}, EMPRESA, {"CLIENTE"})


def test_interno_no_puede_tener_empresa():
    with pytest.raises(BusinessRuleError):
        validar_roles_de_usuario({"COMERCIAL"}, EMPRESA, {"CLIENTE"})


def test_externo_requiere_empresa():
    with pytest.raises(BusinessRuleError):
        validar_roles_de_usuario({"CLIENTE"}, None, set())


@pytest.mark.parametrize(
    "rol, comerciales, valido",
    [
        ("CLIENTE", {"CLIENTE"}, True),
        ("CLIENTE", {"OPERADOR"}, False),
        ("OPERADOR_LOGISTICO", {"OPERADOR"}, True),
        ("OPERADOR_LOGISTICO", {"PROVEEDOR"}, False),  # los proveedores no tienen perfil externo
    ],
)
def test_externo_requiere_rol_comercial_activo_de_su_empresa(rol, comerciales, valido):
    if valido:
        validar_roles_de_usuario({rol}, EMPRESA, comerciales)
    else:
        with pytest.raises(BusinessRuleError):
            validar_roles_de_usuario({rol}, EMPRESA, comerciales)


def test_usuario_sin_roles_o_con_rol_inexistente_se_rechaza():
    with pytest.raises(BusinessRuleError):
        validar_roles_de_usuario(set(), None, set())
    with pytest.raises(ValidationError):
        validar_roles_de_usuario({"SUPERUSUARIO"}, None, set())


# --------------------------------------------------------------------------- alcance
def _principal(tercero_id=None, permisos=()):
    return Principal(
        uuid.uuid4(), uuid.uuid4(), "a@b.com", "A B", tercero_id, frozenset(), frozenset(permisos)
    )


def test_alcance_de_terceros():
    otro = uuid.uuid4()
    assert _principal().puede_ver_tercero(otro)  # interno
    externo = _principal(tercero_id=EMPRESA)
    assert externo.puede_ver_tercero(EMPRESA)
    assert not externo.puede_ver_tercero(otro)


def test_tiene_permiso_acepta_alternativas():
    p = _principal(permisos={"envios.crear"})
    assert p.tiene_permiso("terceros.gestionar", "envios.crear")
    assert not p.tiene_permiso("terceros.gestionar")


# --------------------------------------------------------------------------- validación de datos
def test_tercero_se_normaliza():
    d = TerceroDatos("  ACME SA ", "ar", " cuit ", " 30-1 ", email=" ", telefono=" 11 ").normalizado()
    assert (d.razon_social, d.pais_codigo, d.tipo_identificacion, d.numero_identificacion) == (
        "ACME SA",
        "AR",
        "CUIT",
        "30-1",
    )
    assert d.email is None and d.telefono == "11"


@pytest.mark.parametrize(
    "datos",
    [
        TerceroDatos("", "AR", "CUIT", "1"),
        TerceroDatos("X", "ARG", "CUIT", "1"),
        TerceroDatos("X", "AR", "", "1"),
        TerceroDatos("X", "AR", "CUIT", " "),
        TerceroDatos("X", "AR", "CUIT", "1", email="sin-arroba"),
    ],
)
def test_tercero_invalido_se_rechaza_con_mensaje_claro(datos):
    with pytest.raises(ValidationError) as info:
        datos.normalizado()
    assert info.value.user_message.startswith("Revise los datos")


def test_usuario_email_en_minusculas_y_obligatorios():
    assert UsuarioDatos(" Ana ", "Paz", " Ana@Mail.COM ").normalizado().email == "ana@mail.com"
    with pytest.raises(ValidationError):
        UsuarioDatos("", "Paz", "a@b.com").normalizado()


def test_politica_de_contrasena():
    validar_password("una clave larga")
    for mala in ("corta", " con espacio al inicio", "con espacio al final "):
        with pytest.raises(ValidationError):
            validar_password(mala)


# --------------------------------------------------------------------------- argon2
@pytest.fixture(scope="module")
def hasher():
    return Hasher(time_cost=1, memory_cost_kib=8 * 1024, parallelism=1)  # rápido para pruebas


def test_hash_argon2id_y_verificacion(hasher):
    h = hasher.hash("clave-segura-123")
    assert h.startswith("$argon2id$")
    assert "clave-segura-123" not in h
    assert hasher.verificar(h, "clave-segura-123")
    assert not hasher.verificar(h, "otra-clave-999")
    assert hasher.hash("clave-segura-123") != h  # sal distinta en cada hash


def test_verificar_sin_hash_o_hash_corrupto_es_falso(hasher):
    assert not hasher.verificar(None, "contraseña-inexistente")  # usuario inexistente: nunca entra
    assert not hasher.verificar("no-es-un-hash", "x")


def test_rehash_cuando_cambian_los_parametros(hasher):
    viejo = hasher.hash("clave-segura-123")
    assert not hasher.necesita_rehash(viejo)
    assert Hasher(time_cost=2, memory_cost_kib=8 * 1024, parallelism=1).necesita_rehash(viejo)


# --------------------------------------------------------------------------- coherencia con la semilla
def _seed() -> dict[str, set[str]]:
    sql = (ROOT / "db" / "postgres" / "090_seed_roles_permisos.sql").read_text(encoding="utf-8")
    pares = re.findall(r"\('([A-Z_]+)', '([a-z]+\.[a-z]+)'\)", sql)
    matriz: dict[str, set[str]] = {}
    for rol, permiso in pares:
        matriz.setdefault(rol, set()).add(permiso)
    return matriz


def test_enums_coinciden_con_la_semilla():
    sql = (ROOT / "db" / "postgres" / "090_seed_roles_permisos.sql").read_text(encoding="utf-8")
    sembrados = set(re.findall(r"gen_random_uuid\(\), '([a-z]+\.[a-z]+)'", sql))
    assert sembrados == {p.value for p in Permiso}
    roles = set(re.findall(r"gen_random_uuid\(\), '([A-Z_]+)'", sql))
    assert roles == {r.value for r in Rol}


@pytest.mark.parametrize(
    "rol, permitidos, prohibidos",
    [
        # Celdas de la matriz de AGENTS.md §11
        (
            "OPERACIONES",
            {"envios.crear", "alertas.cerrar", "conversaciones.crear"},
            {"usuarios.administrar", "terceros.gestionar", "facturas.emitir"},
        ),
        (
            "COMERCIAL",
            {"terceros.gestionar", "facturas.emitir", "cuentas.cerrar"},
            {"envios.crear", "usuarios.administrar", "tramos.actualizar"},
        ),
        (
            "CLIENTE",
            {"envios.consultar", "monitoreo.consultar"},
            {"terceros.gestionar", "usuarios.administrar", "sesiones.administrar", "tramos.actualizar"},
        ),
        (
            "OPERADOR_LOGISTICO",
            {"tramos.actualizar", "alertas.reconocer"},
            {"terceros.gestionar", "usuarios.administrar", "alertas.cerrar", "envios.crear"},
        ),
    ],
)
def test_semilla_respeta_la_matriz_de_permisos(rol, permitidos, prohibidos):
    matriz = _seed()
    assert permitidos <= matriz[rol]
    assert not (prohibidos & matriz[rol])
