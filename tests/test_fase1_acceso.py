"""Fase 1 — acceso contra PostgreSQL real (identidad PG_APP_USER), sin dejar datos.

Cada prueba corre en una transacción que se revierte (ver `tx_connections` en conftest.py).
Requiere LOGISTICA_TEST_CONNECTIONS=1.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest

from logistica.core.config import SecurityConfig
from logistica.core.errors import (
    AuthenticationError,
    BusinessRuleError,
    NotFoundError,
    PermissionDeniedError,
    SessionExpiredError,
    ValidationError,
)
from logistica.domain.acceso import TerceroDatos, UsuarioDatos
from logistica.domain.passwords import Hasher
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.usuarios import UsuarioRepository
from logistica.services.acceso_service import AccesoService
from logistica.services.autorizacion import Autorizador
from logistica.services.tercero_service import TerceroService
from logistica.services.usuario_service import UsuarioService

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y PostgreSQL levantado)",
)

CLAVE = "clave-de-prueba-123"


class Entorno:
    """Servicios reales + datos mínimos: un admin, tres empresas y un usuario por perfil."""

    def __init__(self, conns):
        self.conns = conns
        self.hasher = Hasher(time_cost=1, memory_cost_kib=8 * 1024, parallelism=1)
        self.instancia = uuid.uuid4()
        self.aut = Autorizador(conns)
        self.acceso = AccesoService(conns, self.hasher, SecurityConfig(), self.instancia, self.aut)
        self.usuarios = UsuarioService(conns, self.hasher, self.aut)
        self.terceros = TerceroService(conns, self.aut)
        self.sufijo = uuid.uuid4().hex[:8]

        # Administrador creado directo en la base (como haría scripts/crear_admin.py).
        self.admin_email = f"admin-{self.sufijo}@test.local"
        with unit_of_work(conns) as tx:
            repo = UsuarioRepository()
            admin_id = uuid.uuid4()
            repo.crear(tx, admin_id, UsuarioDatos("Ada", "Admin", self.admin_email), self.hasher.hash(CLAVE))
            repo.reemplazar_roles(tx, admin_id, {"ADMINISTRADOR"})
        self.admin = self.login(self.admin_email)

        self.cliente_sa = self.terceros.crear(self.admin.sesion_id, self._tercero("Cliente SA"), {"CLIENTE"})
        self.operador_sa = self.terceros.crear(
            self.admin.sesion_id, self._tercero("Transportes SA"), {"OPERADOR"}
        )
        self.proveedor_sa = self.terceros.crear(
            self.admin.sesion_id, self._tercero("Proveedor SA"), {"PROVEEDOR"}
        )

        self.emails = {"ADMINISTRADOR": self.admin_email}
        for rol, tercero in (
            ("OPERACIONES", None),
            ("COMERCIAL", None),
            ("CLIENTE", self.cliente_sa.id),
            ("OPERADOR_LOGISTICO", self.operador_sa.id),
        ):
            email = f"{rol.lower()}-{self.sufijo}@test.local"
            self.usuarios.crear(
                self.admin.sesion_id, UsuarioDatos("Nombre", rol.title(), email, tercero), {rol}, CLAVE
            )
            self.emails[rol] = email

    def _tercero(self, razon: str) -> TerceroDatos:
        return TerceroDatos(f"{razon} {self.sufijo}", "AR", "CUIT", f"30-{uuid.uuid4().int % 10**8:08d}-1")

    def login(self, email: str, clave: str = CLAVE):
        return self.acceso.login(email, clave, "pruebas")


@pytest.fixture()
def e(tx_connections):
    return Entorno(tx_connections)


# --------------------------------------------------------------------------- login y sesiones
def test_login_crea_sesion_y_carga_roles_y_permisos_desde_la_base(e):
    p = e.login(e.emails["COMERCIAL"].upper())  # el email no distingue mayúsculas
    assert p.roles == frozenset({"COMERCIAL"})
    assert "terceros.gestionar" in p.permisos and "usuarios.administrar" not in p.permisos
    sesiones = e.acceso.listar_sesiones(p.sesion_id)
    assert [(s.id, s.estado, s.es_actual) for s in sesiones] == [(p.sesion_id, "ABIERTA", True)]


@pytest.mark.parametrize("caso", ["clave_mala", "email_inexistente", "usuario_inactivo"])
def test_login_fallido_da_el_mismo_mensaje_en_todos_los_casos(e, caso):
    email, clave = e.emails["OPERACIONES"], CLAVE
    if caso == "clave_mala":
        clave = "otra-clave-999"
    elif caso == "email_inexistente":
        email = "nadie@test.local"
    else:
        usuario = next(u for u in e.usuarios.listar(e.admin.sesion_id) if u.email == email)
        e.usuarios.set_activo(e.admin.sesion_id, usuario.id, False)
    with pytest.raises(AuthenticationError) as info:
        e.login(email, clave)
    assert info.value.user_message == "Email o contraseña incorrectos."


def test_login_actualiza_el_hash_si_cambiaron_los_parametros(e):
    with unit_of_work(e.conns) as tx:
        antes = UsuarioRepository().buscar_credenciales(tx, e.emails["OPERACIONES"]).password_hash
    fuerte = Hasher(time_cost=2, memory_cost_kib=8 * 1024, parallelism=1)
    AccesoService(e.conns, fuerte, SecurityConfig(), e.instancia, e.aut).login(e.emails["OPERACIONES"], CLAVE)
    with unit_of_work(e.conns) as tx:
        despues = UsuarioRepository().buscar_credenciales(tx, e.emails["OPERACIONES"]).password_hash
    assert antes != despues and not fuerte.necesita_rehash(despues)


def test_logout_corta_la_operacion_siguiente(e):
    p = e.login(e.emails["COMERCIAL"])
    e.acceso.logout(p.sesion_id)
    with pytest.raises(SessionExpiredError):
        e.terceros.listar(p.sesion_id)


def test_revocar_sesion_ajena_corta_la_operacion_siguiente(e):
    victima = e.login(e.emails["COMERCIAL"])
    e.terceros.listar(victima.sesion_id)  # funciona
    e.acceso.revocar_sesion(e.admin.sesion_id, victima.sesion_id)
    with pytest.raises(SessionExpiredError):
        e.terceros.listar(victima.sesion_id)
    estados = {
        s.id: s.estado for s in e.acceso.listar_sesiones(e.admin.sesion_id, todas=True, solo_abiertas=False)
    }
    assert estados[victima.sesion_id] == "REVOCADA"


def test_sin_permiso_no_se_revocan_ni_listan_sesiones_ajenas(e):
    comercial = e.login(e.emails["COMERCIAL"])
    with pytest.raises(PermissionDeniedError):
        e.acceso.revocar_sesion(comercial.sesion_id, e.admin.sesion_id)
    with pytest.raises(PermissionDeniedError):
        e.acceso.listar_sesiones(comercial.sesion_id, todas=True)
    # Pero cualquiera puede revocar una sesión propia (por ejemplo, la de otro equipo).
    otra = e.login(e.emails["COMERCIAL"])
    e.acceso.revocar_sesion(comercial.sesion_id, otra.sesion_id)
    with pytest.raises(SessionExpiredError):
        e.acceso.principal(otra.sesion_id)


def test_sesion_vencida_no_sirve(e):
    from sqlalchemy import text

    p = e.login(e.emails["COMERCIAL"])
    with e.conns.pg_session() as s:
        s.execute(
            text(
                "UPDATE sesion SET inicio_en = clock_timestamp() - interval '9 hours', "
                "vence_en = clock_timestamp() - interval '1 hour' WHERE id = :id"
            ),
            {"id": p.sesion_id},
        )
    with pytest.raises(SessionExpiredError):
        e.terceros.listar(p.sesion_id)


def test_sesion_inexistente_no_sirve(e):
    with pytest.raises(SessionExpiredError):
        e.terceros.listar(uuid.uuid4())


def test_desactivar_usuario_revoca_sus_sesiones(e):
    p = e.login(e.emails["OPERACIONES"])
    usuario = next(u for u in e.usuarios.listar(e.admin.sesion_id) if u.email == e.emails["OPERACIONES"])
    e.usuarios.set_activo(e.admin.sesion_id, usuario.id, False)
    with pytest.raises(SessionExpiredError):
        e.terceros.listar(p.sesion_id)
    abiertas = e.acceso.listar_sesiones(e.admin.sesion_id, todas=True)
    assert all(s.usuario_id != usuario.id for s in abiertas)


def test_quitar_un_rol_cambia_los_permisos_en_la_operacion_siguiente(e):
    p = e.login(e.emails["COMERCIAL"])
    e.terceros.crear(p.sesion_id, e._tercero("Nuevo"), {"CLIENTE"})
    usuario = next(u for u in e.usuarios.listar(e.admin.sesion_id) if u.email == e.emails["COMERCIAL"])
    e.usuarios.asignar_roles(e.admin.sesion_id, usuario.id, {"OPERACIONES"})
    with pytest.raises(PermissionDeniedError):
        e.terceros.crear(p.sesion_id, e._tercero("Otro"), {"CLIENTE"})  # misma sesión, permisos nuevos


def test_externo_pierde_acceso_si_su_empresa_se_desactiva(e):
    p = e.login(e.emails["CLIENTE"])
    assert e.terceros.obtener(p.sesion_id, e.cliente_sa.id).id == e.cliente_sa.id
    e.terceros.set_activo(e.admin.sesion_id, e.cliente_sa.id, False)
    with pytest.raises(SessionExpiredError):
        e.terceros.obtener(p.sesion_id, e.cliente_sa.id)
    with pytest.raises(AuthenticationError):
        e.login(e.emails["CLIENTE"])


def test_externo_pierde_acceso_si_su_empresa_pierde_el_rol_comercial(e):
    p = e.login(e.emails["OPERADOR_LOGISTICO"])
    e.terceros.set_roles_comerciales(e.admin.sesion_id, e.operador_sa.id, {"PROVEEDOR"})
    with pytest.raises(SessionExpiredError):
        e.acceso.principal(p.sesion_id)


def test_cambiar_la_propia_contrasena(e):
    p = e.login(e.emails["OPERACIONES"])
    with pytest.raises(AuthenticationError):
        e.acceso.cambiar_password(p.sesion_id, "incorrecta-000", "nueva-clave-456")
    with pytest.raises(ValidationError):
        e.acceso.cambiar_password(p.sesion_id, CLAVE, "corta")
    e.acceso.cambiar_password(p.sesion_id, CLAVE, "nueva-clave-456")
    e.login(e.emails["OPERACIONES"], "nueva-clave-456")
    with pytest.raises(AuthenticationError):
        e.login(e.emails["OPERACIONES"], CLAVE)


# --------------------------------------------------------------------------- matriz por perfil (§11)
def _crear_tercero(e, p):
    e.terceros.crear(p.sesion_id, e._tercero("Matriz"), {"CLIENTE"})


def _listar_terceros(e, p):
    e.terceros.listar(p.sesion_id)


def _listar_usuarios(e, p):
    e.usuarios.listar(p.sesion_id)


def _sesiones_de_todos(e, p):
    e.acceso.listar_sesiones(p.sesion_id, todas=True)


def _crear_usuario(e, p):
    e.usuarios.crear(
        p.sesion_id, UsuarioDatos("X", "Y", f"x-{uuid.uuid4().hex[:6]}@test.local"), {"OPERACIONES"}, CLAVE
    )


OPERACIONES_MATRIZ = {
    "crear tercero": _crear_tercero,
    "listar terceros": _listar_terceros,
    "listar usuarios": _listar_usuarios,
    "crear usuario": _crear_usuario,
    "ver sesiones de todos": _sesiones_de_todos,
}
MATRIZ = {
    #                     crear ter, listar ter, listar usu, crear usu, sesiones todos
    "ADMINISTRADOR": (True, True, True, True, True),
    "OPERACIONES": (False, True, False, False, False),
    "COMERCIAL": (True, True, False, False, False),
    "CLIENTE": (False, False, False, False, False),
    "OPERADOR_LOGISTICO": (False, False, False, False, False),
}


@pytest.mark.parametrize("rol", list(MATRIZ))
def test_cada_perfil_solo_hace_lo_que_su_matriz_permite(e, rol):
    p = e.login(e.emails[rol])
    for (nombre, operacion), permitido in zip(OPERACIONES_MATRIZ.items(), MATRIZ[rol]):
        if permitido:
            operacion(e, p)
        else:
            with pytest.raises(PermissionDeniedError):
                operacion(e, p)
            print(f"{rol}: {nombre} denegado")  # visible con -s


def test_alcance_externo_solo_ve_su_empresa(e):
    cliente = e.login(e.emails["CLIENTE"])
    assert e.terceros.obtener(cliente.sesion_id, e.cliente_sa.id).razon_social.startswith("Cliente SA")
    with pytest.raises(PermissionDeniedError):
        e.terceros.obtener(cliente.sesion_id, e.operador_sa.id)
    operador = e.login(e.emails["OPERADOR_LOGISTICO"])
    with pytest.raises(PermissionDeniedError):
        e.terceros.obtener(operador.sesion_id, e.cliente_sa.id)


def test_externo_solo_ve_y_revoca_sus_propias_sesiones(e):
    cliente = e.login(e.emails["CLIENTE"])
    assert {s.usuario_id for s in e.acceso.listar_sesiones(cliente.sesion_id)} == {
        e.acceso.principal(cliente.sesion_id).usuario_id
    }


# --------------------------------------------------------------------------- reglas de usuarios
@pytest.mark.parametrize(
    "roles, tercero",
    [
        ({"OPERACIONES", "CLIENTE"}, "cliente"),  # internos y externos mezclados
        ({"CLIENTE"}, "operador"),  # empresa sin rol comercial CLIENTE
        ({"OPERADOR_LOGISTICO"}, "proveedor"),  # proveedor sin perfil externo
        ({"CLIENTE"}, None),  # externo sin empresa
        ({"COMERCIAL"}, "cliente"),  # interno con empresa
    ],
)
def test_reglas_de_combinacion_de_roles_al_crear_usuarios(e, roles, tercero):
    ids = {
        "cliente": e.cliente_sa.id,
        "operador": e.operador_sa.id,
        "proveedor": e.proveedor_sa.id,
        None: None,
    }
    with pytest.raises(BusinessRuleError):
        e.usuarios.crear(
            e.admin.sesion_id,
            UsuarioDatos("X", "Y", f"r-{uuid.uuid4().hex[:6]}@test.local", ids[tercero]),
            roles,
            CLAVE,
        )


def test_email_duplicado_se_rechaza_sin_importar_mayusculas(e):
    with pytest.raises(ValidationError, match="email"):
        e.usuarios.crear(
            e.admin.sesion_id, UsuarioDatos("X", "Y", e.emails["COMERCIAL"].upper()), {"COMERCIAL"}, CLAVE
        )


def _dejar_un_solo_admin(e):
    """Precondición determinista: dentro de la transacción de la prueba (se revierte), el admin del entorno
    queda como único administrador activo aunque la base de desarrollo tenga otros."""
    from sqlalchemy import text

    with e.conns.pg_session() as s:
        s.execute(
            text(
                "UPDATE usuario SET activo = false WHERE lower(email) <> lower(:email) AND id IN ("
                "SELECT ur.usuario_id FROM usuario_rol ur JOIN rol r ON r.id = ur.rol_id "
                "WHERE r.codigo = 'ADMINISTRADOR')"
            ),
            {"email": e.admin_email},
        )
    assert e.usuarios.contar_administradores_activos(e.admin.sesion_id) == 1


def test_no_se_puede_dejar_el_sistema_sin_administradores(e):
    _dejar_un_solo_admin(e)
    admin = next(u for u in e.usuarios.listar(e.admin.sesion_id) if u.email == e.admin_email)
    with pytest.raises(BusinessRuleError):
        e.usuarios.asignar_roles(e.admin.sesion_id, admin.id, {"OPERACIONES"})
    # Otro admin puede quitarle el rol solo si queda al menos uno.
    otro_email = f"admin2-{e.sufijo}@test.local"
    otro = e.usuarios.crear(
        e.admin.sesion_id, UsuarioDatos("Otro", "Admin", otro_email), {"ADMINISTRADOR"}, CLAVE
    )
    e.usuarios.asignar_roles(e.admin.sesion_id, otro.id, {"OPERACIONES"})
    with pytest.raises(BusinessRuleError):
        e.usuarios.asignar_roles(e.admin.sesion_id, admin.id, {"OPERACIONES"})


def test_nadie_se_desactiva_a_si_mismo(e):
    admin = next(u for u in e.usuarios.listar(e.admin.sesion_id) if u.email == e.admin_email)
    with pytest.raises(BusinessRuleError):
        e.usuarios.set_activo(e.admin.sesion_id, admin.id, False)


def test_los_cambios_sobre_administradores_se_serializan():
    """Dos transacciones que cambian administradores no pueden contar a la vez (lock de PostgreSQL).

    Prueba el mecanismo con dos conexiones reales y sin escribir datos: mientras T1 tiene el lock, T2 no lo
    obtiene. Así la segunda transacción cuenta recién después de que la primera confirmó (foto nueva).
    """
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections
    from logistica.repositories.postgres.usuarios import UsuarioRepository

    conns = Connections(load_config())
    repo = UsuarioRepository()
    try:
        with Session(conns.engine) as t1, Session(conns.engine) as t2:
            repo.bloquear_cambios_de_administradores(t1)
            t2.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(Exception, match="lock timeout|canceling statement"):
                repo.bloquear_cambios_de_administradores(t2)
            t2.rollback()
            t1.rollback()  # libera el lock
            repo.bloquear_cambios_de_administradores(t2)  # ahora sí
            t2.rollback()
    finally:
        conns.close()


def test_usuario_de_empresa_desactivada_se_puede_editar_sin_cambiar_roles(e):
    cliente = next(u for u in e.usuarios.listar(e.admin.sesion_id) if u.email == e.emails["CLIENTE"])
    e.terceros.set_activo(e.admin.sesion_id, e.cliente_sa.id, False)
    editado = e.usuarios.actualizar(
        e.admin.sesion_id, cliente.id, UsuarioDatos("Nuevo", "Nombre", cliente.email), set(cliente.roles)
    )
    assert editado.nombre == "Nuevo"


def test_la_app_no_puede_modificar_el_catalogo_de_permisos(e):
    # La identidad de ejecución solo lee rol_permiso (900_grants.sql):
    # cambiar la matriz de permisos no es posible desde la app.
    from sqlalchemy import text

    from logistica.core.errors import LogisticaError

    with pytest.raises((LogisticaError, Exception), match="permission denied|permiso"):
        with e.conns.pg_session() as s:
            s.execute(
                text(
                    "INSERT INTO rol_permiso (rol_id, permiso_id) SELECT r.id, p.id FROM rol r, permiso p "
                    "WHERE r.codigo = 'CLIENTE' AND p.codigo = 'envios.crear' ON CONFLICT DO NOTHING"
                )
            )


def test_externo_no_lista_terceros_aunque_tuviera_el_permiso(e, monkeypatch):
    # Si una semilla futura le diera a CLIENTE un permiso de consulta, el alcance igual lo frena.
    from dataclasses import replace

    cliente = e.login(e.emails["CLIENTE"])
    original = e.aut.principal
    monkeypatch.setattr(
        e.aut, "principal", lambda tx, sid: replace(original(tx, sid), permisos=frozenset({"envios.crear"}))
    )
    with pytest.raises(PermissionDeniedError):
        e.terceros.listar(cliente.sesion_id)


def test_datos_demasiado_largos_se_rechazan_antes_de_la_base(e):
    with pytest.raises(ValidationError):
        e.usuarios.crear(
            e.admin.sesion_id,
            UsuarioDatos("N" * 101, "Y", f"l-{e.sufijo}@test.local"),
            {"OPERACIONES"},
            CLAVE,
        )
    with pytest.raises(ValidationError):
        e.terceros.crear(
            e.admin.sesion_id, TerceroDatos("X", "AR", "CUIT", "1", telefono="9" * 41), {"CLIENTE"}
        )


def test_error_de_base_no_deja_hashes_ni_parametros_en_el_log(e, caplog):
    from sqlalchemy import text

    from logistica.core.logging_setup import RedactSecretsFilter
    from logistica.repositories.postgres import unit_of_work

    hash_falso = e.hasher.hash("clave-que-no-debe-verse")
    with pytest.raises(ValidationError) as info:  # DataError (varchar 100 excedido) traducido
        with unit_of_work(e.conns) as tx:
            tx.execute(
                text(
                    "INSERT INTO usuario (id, nombre, apellido, email, password_hash) "
                    "VALUES (gen_random_uuid(), :n, 'x', 'x@x.x', :h)"
                ),
                {"n": "N" * 150, "h": hash_falso},
            )
    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "fallo: %s", (str(info.value.__cause__),), None
    )
    RedactSecretsFilter().filter(record)
    assert hash_falso not in record.getMessage()
    assert hash_falso not in str(info.value.__cause__)  # hide_parameters=True: el driver ni lo incluye


def test_administrador_inicial_solo_si_no_hay_ninguno(e):
    with pytest.raises(BusinessRuleError):
        e.usuarios.crear_administrador_inicial(UsuarioDatos("Otro", "Admin", "otro@test.local"), CLAVE)
    # Sin administradores activos (cambio dentro de la transacción de la prueba: se revierte).
    from sqlalchemy import text

    with e.conns.pg_session() as s:
        s.execute(
            text(
                "UPDATE usuario SET activo = false WHERE id IN (SELECT ur.usuario_id FROM usuario_rol ur "
                "JOIN rol r ON r.id = ur.rol_id WHERE r.codigo = 'ADMINISTRADOR')"
            )
        )
    nuevo = e.usuarios.crear_administrador_inicial(
        UsuarioDatos("Nuevo", "Admin", f"n-{e.sufijo}@test.local"), CLAVE
    )
    assert nuevo.roles == frozenset({"ADMINISTRADOR"})
    assert "ADMINISTRADOR" in e.login(nuevo.email).roles


def test_resetear_contrasena_ajena_exige_permiso_y_revoca_sesiones(e):
    victima = e.login(e.emails["COMERCIAL"])
    usuario = next(u for u in e.usuarios.listar(e.admin.sesion_id) if u.email == e.emails["COMERCIAL"])
    e.usuarios.resetear_password(e.admin.sesion_id, usuario.id, "reseteada-789")
    with pytest.raises(SessionExpiredError):
        e.acceso.principal(victima.sesion_id)
    e.login(e.emails["COMERCIAL"], "reseteada-789")


# --------------------------------------------------------------------------- ABM de terceros
def test_abm_de_terceros_con_baja_logica(e):
    s = e.admin.sesion_id
    t = e.terceros.crear(s, e._tercero("Abm"), {"CLIENTE", "PROVEEDOR"})
    assert t.roles_comerciales == frozenset({"CLIENTE", "PROVEEDOR"})
    editado = e.terceros.actualizar(
        s, t.id, TerceroDatos(f"Abm Editado {e.sufijo}", "UY", "RUT", "21-1", email="contacto@abm.uy")
    )
    assert (editado.razon_social, editado.pais_codigo, editado.email) == (
        f"Abm Editado {e.sufijo}",
        "UY",
        "contacto@abm.uy",
    )
    e.terceros.set_roles_comerciales(s, t.id, {"CLIENTE"})
    assert e.terceros.obtener(s, t.id).roles_comerciales == frozenset({"CLIENTE"})
    e.terceros.set_activo(s, t.id, False)
    assert t.id not in {x.id for x in e.terceros.listar(s, texto="Abm Editado")}
    inactivo = e.terceros.obtener(s, t.id)  # baja lógica: sigue existiendo
    assert not inactivo.activo
    assert t.id in {x.id for x in e.terceros.listar(s, texto="Abm Editado", incluir_inactivos=True)}
    e.terceros.set_activo(s, t.id, True)
    assert e.terceros.obtener(s, t.id).activo


def test_tercero_con_identificacion_repetida_se_rechaza(e):
    d = e._tercero("Duplicado")
    e.terceros.crear(e.admin.sesion_id, d, {"CLIENTE"})
    with pytest.raises(ValidationError, match="identificación"):
        e.terceros.crear(
            e.admin.sesion_id,
            TerceroDatos("Otra razón", d.pais_codigo, d.tipo_identificacion.lower(), d.numero_identificacion),
            {"CLIENTE"},
        )


def test_tercero_requiere_al_menos_un_rol_comercial_valido(e):
    with pytest.raises(BusinessRuleError):
        e.terceros.crear(e.admin.sesion_id, e._tercero("Sin rol"), set())
    with pytest.raises(ValidationError):
        e.terceros.crear(e.admin.sesion_id, e._tercero("Rol malo"), {"DISTRIBUIDOR"})


def test_filtrar_terceros_por_rol_comercial(e):
    ids = {t.id for t in e.terceros.listar(e.admin.sesion_id, rol="OPERADOR", texto=e.sufijo)}
    assert ids == {e.operador_sa.id}


def test_tercero_inexistente(e):
    with pytest.raises(NotFoundError):
        e.terceros.obtener(e.admin.sesion_id, uuid.uuid4())


# --------------------------------------------------------------------------- auditoría
def test_auditoria_registra_ids_y_nunca_la_contrasena(e, caplog):
    caplog.set_level(logging.INFO, logger="logistica.auditoria")
    p = e.login(e.emails["COMERCIAL"])
    e.acceso.cambiar_password(p.sesion_id, CLAVE, "nueva-clave-456")
    registros = [r.getMessage() for r in caplog.records if r.name == "logistica.auditoria"]
    assert any("login" in m and str(p.usuario_id) in m for m in registros)
    assert any("cambiar_password" in m for m in registros)
    texto = "\n".join(r.getMessage() for r in caplog.records)
    assert CLAVE not in texto and "nueva-clave-456" not in texto


# --------------------------------------------------------------------------- tabla de auditoría
def _auditoria_de(e, usuario_id, **filtros):
    from logistica.services.auditoria_service import AuditoriaService

    return AuditoriaService(e.conns, e.aut).listar(e.admin.sesion_id, usuario_id=usuario_id, **filtros)


def test_la_app_no_puede_modificar_la_auditoria_ni_el_catalogo_de_roles(e):
    from sqlalchemy import text

    with e.conns.pg_session() as s:
        privilegios = s.execute(
            text(
                "SELECT has_table_privilege(current_user, 'auditoria', 'INSERT'),"
                " has_table_privilege(current_user, 'auditoria', 'SELECT'),"
                " has_table_privilege(current_user, 'auditoria', 'UPDATE'),"
                " has_table_privilege(current_user, 'auditoria', 'DELETE'),"
                " has_table_privilege(current_user, 'rol', 'SELECT'),"
                " has_table_privilege(current_user, 'rol', 'INSERT'),"
                " has_table_privilege(current_user, 'rol', 'UPDATE'),"
                " has_table_privilege(current_user, 'rol', 'DELETE')"
            )
        ).one()
    assert tuple(privilegios) == (True, True, False, False, True, False, False, False)


def test_cada_operacion_deja_su_registro_en_la_base(e):
    t = e.terceros.crear(e.admin.sesion_id, e._tercero("Auditado"), {"CLIENTE"})
    registros = _auditoria_de(e, e.admin.usuario_id, accion="crear_tercero").registros
    propio = [r for r in registros if r.entidad_id == t.id]
    assert len(propio) == 1
    r = propio[0]
    assert (r.resultado, r.entidad, r.usuario_email) == ("EXITO", "tercero", e.admin_email)
    assert r.detalle["roles"] == ["CLIENTE"]


def test_una_operacion_rechazada_no_deja_registro_de_exito(e):
    d = e._tercero("Repetido")
    e.terceros.crear(e.admin.sesion_id, d, {"CLIENTE"})
    antes = len(_auditoria_de(e, e.admin.usuario_id, accion="crear_tercero").registros)
    with pytest.raises(ValidationError):
        e.terceros.crear(e.admin.sesion_id, d, {"CLIENTE"})
    # La fila de auditoría iba en la misma transacción que el alta: al revertirse, no queda ninguna.
    assert len(_auditoria_de(e, e.admin.usuario_id, accion="crear_tercero").registros) == antes


def test_el_login_fallido_queda_registrado_aunque_el_login_se_revierta(e):
    usuario = next(u for u in e.usuarios.listar(e.admin.sesion_id) if u.email == e.emails["COMERCIAL"])
    with pytest.raises(AuthenticationError):
        e.login(e.emails["COMERCIAL"], "clave-incorrecta-1")
    fallidos = _auditoria_de(e, usuario.id, accion="login_fallido").registros
    assert len(fallidos) == 1 and fallidos[0].resultado == "RECHAZO"
    with pytest.raises(AuthenticationError):
        e.login("nadie-existe@test.local", "x")
    from logistica.services.auditoria_service import AuditoriaService

    anonimos = AuditoriaService(e.conns, e.aut).listar(e.admin.sesion_id, accion="login_fallido").registros
    assert any(r.usuario_id is None for r in anonimos)
    assert all("nadie-existe" not in str(r.detalle) for r in anonimos)  # no se guardan emails ajenos


def test_login_y_logout_quedan_registrados(e):
    p = e.login(e.emails["OPERACIONES"])
    e.acceso.logout(p.sesion_id)
    acciones = [r.accion for r in _auditoria_de(e, p.usuario_id).registros]
    assert "login" in acciones and "logout" in acciones


def test_solo_quien_tiene_permiso_consulta_la_auditoria(e):
    from logistica.services.auditoria_service import AuditoriaService

    comercial = e.login(e.emails["COMERCIAL"])
    with pytest.raises(PermissionDeniedError):
        AuditoriaService(e.conns, e.aut).listar(comercial.sesion_id)


def test_la_auditoria_se_pagina(e):
    for i in range(3):
        e.terceros.crear(e.admin.sesion_id, e._tercero(f"Pagina {i}"), {"CLIENTE"})
    primera = _auditoria_de(e, e.admin.usuario_id, accion="crear_tercero", limite=2)
    assert len(primera.registros) == 2 and primera.hay_mas
    resto = _auditoria_de(e, e.admin.usuario_id, accion="crear_tercero", limite=2, offset=2)
    todas = {r.id for r in primera.registros} | {r.id for r in resto.registros}
    assert len(todas) == len(primera.registros) + len(resto.registros)  # sin repetidos entre páginas
    fechas = [r.fecha for r in primera.registros + resto.registros]
    assert fechas == sorted(fechas, reverse=True)  # más recientes primero


def test_la_auditoria_nunca_guarda_contrasenas_ni_hashes(e):
    from sqlalchemy import text

    p = e.login(e.emails["OPERACIONES"])
    e.acceso.cambiar_password(p.sesion_id, CLAVE, "nueva-clave-456")
    usuario = next(u for u in e.usuarios.listar(e.admin.sesion_id) if u.email == e.emails["OPERACIONES"])
    e.usuarios.resetear_password(e.admin.sesion_id, usuario.id, "reseteada-789")
    with e.conns.pg_session() as s:
        volcado = " ".join(s.execute(text("SELECT accion || ' ' || detalle::text FROM auditoria")).scalars())
    for secreto in (CLAVE, "nueva-clave-456", "reseteada-789", "$argon2"):
        assert secreto not in volcado
