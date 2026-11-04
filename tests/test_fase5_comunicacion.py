"""Chat con motores reales y datos aislados; no representa pruebas manuales del grupo."""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from logistica.core.errors import (
    DatabaseUnavailableError, IntegrityError, PermissionDeniedError, SessionExpiredError, ValidationError,
)
from logistica.domain.acceso import UsuarioDatos
from logistica.domain.comunicacion import MensajeDTO, techo_milisegundo
from logistica.services import auditoria
from logistica.services.comunicacion_service import ComunicacionService
from tests.test_fase1_acceso import CLAVE, Entorno

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="requiere LOGISTICA_TEST_CONNECTIONS=1 y motores reales",
)


@pytest.fixture()
def chat(tx_connections):
    e = Entorno(tx_connections)
    e.chat = ComunicacionService(tx_connections, e.aut)
    e.ops = e.login(e.emails["OPERACIONES"])
    e.com = e.login(e.emails["COMERCIAL"])
    e.cli = e.login(e.emails["CLIENTE"])
    e.conversaciones = []
    yield e
    tx_connections.mongo_db["mensajes"].delete_many({"conversacion_id": {"$in": e.conversaciones}})


def crear(e, tipo="GRUPAL", participantes=None):
    ident = e.chat.crear(
        e.admin.sesion_id, tipo, "DEMO-CHAT prueba aislada",
        tuple(participantes if participantes is not None else (e.ops.usuario_id,)),
    )
    e.conversaciones.append(ident)
    return ident


def enviar(e, ident, p=None, contenido="Hola"):
    return e.chat.enviar((p or e.admin).sesion_id, ident, uuid.uuid4(), contenido)


@pytest.mark.parametrize("tipo", ["PRIVADA", "GRUPAL", "PUBLICA"])
def test_dos_usuarios_distintos_con_remitente_de_sesion_y_utc(chat, tipo):
    ident = crear(chat, tipo)
    a = enviar(chat, ident, contenido="Mensaje de A")
    b = enviar(chat, ident, chat.ops, "Respuesta de B")
    time.sleep(0.003)
    pagina = chat.chat.historial(chat.ops.sesion_id, ident)
    assert {m.id for m in pagina.mensajes} == {a.id, b.id}
    assert a.remitente_id == chat.admin.usuario_id
    assert b.remitente_id == chat.ops.usuario_id
    assert a.fecha_envio.utcoffset() == timedelta(0)
    assert a.fecha_envio.microsecond % 1000 == 0
    assert len({m.id for m in pagina.mensajes}) == 2
    with chat.conns.pg_session() as tx:
        acciones = tx.execute(text(
            "SELECT accion FROM auditoria WHERE entidad_id IN (:conv,:a,:b)"
        ), {"conv": ident, "a": a.id, "b": b.id}).scalars().all()
    assert acciones.count("conversacion.crear") == 1
    assert acciones.count("mensaje.confirmado_mongo") == 2


def test_publico_solo_interno_y_lectura_exige_incorporacion_explicita(chat):
    ident = crear(chat, "PUBLICA", participantes=())
    assert ident in {c.id for c in chat.chat.vista(chat.com.sesion_id).publicas}
    assert chat.chat.vista(chat.cli.sesion_id).publicas == ()
    for p in (chat.com, chat.cli):
        with pytest.raises(PermissionDeniedError):
            chat.chat.historial(p.sesion_id, ident)
    with pytest.raises(PermissionDeniedError):
        chat.chat.unirse(chat.cli.sesion_id, ident)
    anterior = enviar(chat, ident, contenido="Anterior a incorporación")
    chat.chat.unirse(chat.com.sesion_id, ident)
    posterior = enviar(chat, ident, chat.com)
    time.sleep(0.003)
    ids = {m.id for m in chat.chat.historial(chat.com.sesion_id, ident).mensajes}
    assert posterior.id in ids and anterior.id not in ids


def test_publico_revocado_no_puede_reincorporarse_sin_administrador_miembro(chat):
    ident = crear(chat, "PUBLICA", participantes=())
    chat.chat.unirse(chat.com.sesion_id, ident)
    chat.chat.retirar(chat.admin.sesion_id, ident, chat.com.usuario_id)
    with pytest.raises(PermissionDeniedError):
        chat.chat.unirse(chat.com.sesion_id, ident)
    with pytest.raises(PermissionDeniedError):
        chat.chat.historial(chat.com.sesion_id, ident)
    chat.chat.incorporar(chat.admin.sesion_id, ident, chat.com.usuario_id)
    enviar(chat, ident, chat.com)


def test_privados_misma_empresa_sin_acceso_global_del_administrador(chat):
    otro = chat.usuarios.crear(
        chat.admin.sesion_id,
        UsuarioDatos("Cliente", "Dos", f"cli2-{chat.sufijo}@test.local", chat.cliente_sa.id),
        {"CLIENTE"}, CLAVE,
    )
    ident = chat.chat.crear(chat.cli.sesion_id, "PRIVADA", "", (otro.id,))
    chat.conversaciones.append(ident)
    enviar(chat, ident, chat.cli)
    with pytest.raises(PermissionDeniedError):
        chat.chat.historial(chat.admin.sesion_id, ident)
    assert ident not in {c.id for c in chat.chat.vista(chat.admin.sesion_id).conversaciones}
    with pytest.raises(PermissionDeniedError):
        chat.chat.crear(chat.cli.sesion_id, "PRIVADA", "", (chat.admin.usuario_id,))
    externo = chat.login(chat.emails["OPERADOR_LOGISTICO"])
    with pytest.raises(PermissionDeniedError):
        chat.chat.crear(chat.cli.sesion_id, "PRIVADA", "", (externo.usuario_id,))
    assert [u.id for u in chat.chat.destinatarios(chat.cli.sesion_id)] == [otro.id]


def test_creacion_y_gestion_exigen_permiso_y_rol_de_miembro(chat):
    for tipo in ("GRUPAL", "PUBLICA"):
        with pytest.raises(PermissionDeniedError):
            chat.chat.crear(chat.com.sesion_id, tipo, "No permitido", ())
    ident = crear(chat, participantes=(chat.ops.usuario_id, chat.com.usuario_id))
    with pytest.raises(PermissionDeniedError):
        chat.chat.incorporar(chat.ops.sesion_id, ident, chat.cli.usuario_id)
    with pytest.raises(PermissionDeniedError):
        chat.chat.incorporar(chat.admin.sesion_id, ident, chat.cli.usuario_id)
    with pytest.raises(PermissionDeniedError):
        chat.chat.retirar(chat.com.sesion_id, ident, chat.ops.usuario_id)
    chat.chat.retirar(chat.admin.sesion_id, ident, chat.ops.usuario_id)
    chat.chat.incorporar(chat.admin.sesion_id, ident, chat.ops.usuario_id, "ADMINISTRADOR")
    chat.chat.retirar(chat.ops.sesion_id, ident, chat.com.usuario_id)
    chat.chat.retirar(chat.ops.sesion_id, ident, chat.admin.usuario_id)
    with pytest.raises(PermissionDeniedError):
        chat.chat.historial(chat.admin.sesion_id, ident)
    with pytest.raises(ValidationError):
        chat.chat.retirar(chat.ops.sesion_id, ident, chat.ops.usuario_id)
    chat.chat.archivar(chat.ops.sesion_id, ident)
    with pytest.raises(PermissionDeniedError):
        enviar(chat, ident, chat.ops)


def test_permiso_de_gestion_se_revalida_sin_quitar_la_membresia(chat):
    ident = crear(chat, participantes=())
    chat.chat.incorporar(chat.admin.sesion_id, ident, chat.ops.usuario_id, "ADMINISTRADOR")
    chat.usuarios.asignar_roles(chat.admin.sesion_id, chat.ops.usuario_id, {"COMERCIAL"})
    # La sesión conserva un rol efectivo y la membresía, pero perdió conversaciones.crear.
    enviar(chat, ident, chat.ops)
    with pytest.raises(PermissionDeniedError):
        chat.chat.incorporar(chat.ops.sesion_id, ident, chat.com.usuario_id)


@pytest.mark.parametrize("caso", ["ajeno", "membresia", "sesion", "vencida", "usuario", "permiso_gestion"])
def test_revalidacion_de_acceso(chat, caso):
    ident = crear(chat)
    p = chat.ops
    if caso == "ajeno":
        p = chat.com
    elif caso == "membresia":
        chat.chat.retirar(chat.admin.sesion_id, ident, p.usuario_id)
    elif caso == "sesion":
        chat.acceso.logout(p.sesion_id)
    elif caso == "vencida":
        with chat.conns.pg_session() as tx:
            tx.execute(text(
                "UPDATE sesion SET inicio_en=clock_timestamp()-interval '2 hours', "
                "vence_en=clock_timestamp()-interval '1 second' WHERE id=:id"
            ), {"id": p.sesion_id})
    elif caso == "usuario":
        chat.usuarios.set_activo(chat.admin.sesion_id, p.usuario_id, False)
    else:
        chat.chat.retirar(chat.admin.sesion_id, ident, p.usuario_id)
        chat.chat.incorporar(chat.admin.sesion_id, ident, p.usuario_id, "ADMINISTRADOR")
        with chat.conns.pg_session() as tx:
            tx.execute(text(
                "DELETE FROM usuario_rol WHERE usuario_id=:id"
            ), {"id": p.usuario_id})
        with pytest.raises(SessionExpiredError):
            chat.chat.incorporar(p.sesion_id, ident, chat.com.usuario_id)
    for accion in (
        lambda: enviar(chat, ident, p),
        lambda: chat.chat.historial(p.sesion_id, ident),
    ):
        with pytest.raises((PermissionDeniedError, SessionExpiredError)):
            accion()
    assert chat.conns.mongo_db["mensajes"].count_documents({"conversacion_id": ident}) == 0


@pytest.mark.parametrize("texto", ["", "  \n", "x" * 4001])
def test_texto_invalido_no_persiste(chat, texto):
    ident = crear(chat)
    with pytest.raises(ValidationError):
        enviar(chat, ident, contenido=texto)
    assert chat.conns.mongo_db["mensajes"].count_documents({"conversacion_id": ident}) == 0


def test_reintento_con_uuid_estable_conserva_fecha_y_rechaza_otro_contenido(chat):
    ident = crear(chat)
    mensaje_id = uuid.uuid4()
    primero = chat.chat.enviar(chat.ops.sesion_id, ident, mensaje_id, "Prueba")
    segundo = chat.chat.enviar(chat.ops.sesion_id, ident, mensaje_id, "Prueba")
    assert primero == segundo
    with pytest.raises(IntegrityError):
        chat.chat.enviar(chat.ops.sesion_id, ident, mensaje_id, "Texto cambiado")
    with pytest.raises(IntegrityError):
        chat.chat.enviar(chat.admin.sesion_id, ident, mensaje_id, "Prueba")
    assert chat.conns.mongo_db["mensajes"].count_documents({"conversacion_id": ident}) == 1


@pytest.mark.parametrize("punto", ["antes_mongo", "despues_mongo", "auditoria"])
def test_falla_entre_motores_no_confirma_y_reintenta_sin_duplicar(chat, monkeypatch, punto):
    ident = crear(chat)
    mensaje_id = uuid.uuid4()
    guardar = chat.chat._mensajes.guardar
    registrar = auditoria.registrar

    def fallar(mensaje):
        if punto == "despues_mongo":
            guardar(mensaje)
        raise DatabaseUnavailableError("MongoDB", "falla de prueba aislada")

    def fallar_pg(*args, **kwargs):
        raise DatabaseUnavailableError("PostgreSQL", "fallo posterior a persistencia Mongo")

    if punto == "auditoria":
        monkeypatch.setattr(auditoria, "registrar", fallar_pg)
    else:
        monkeypatch.setattr(chat.chat._mensajes, "guardar", fallar)
    with pytest.raises(DatabaseUnavailableError):
        chat.chat.enviar(chat.ops.sesion_id, ident, mensaje_id, "Reintento")
    esperados = 0 if punto == "antes_mongo" else 1
    assert chat.conns.mongo_db["mensajes"].count_documents({"conversacion_id": ident}) == esperados
    monkeypatch.setattr(chat.chat._mensajes, "guardar", guardar)
    monkeypatch.setattr(auditoria, "registrar", registrar)
    resultado = chat.chat.enviar(chat.ops.sesion_id, ident, mensaje_id, "Reintento")
    assert resultado.id == mensaje_id
    assert chat.conns.mongo_db["mensajes"].count_documents({"conversacion_id": ident}) == 1


@pytest.mark.parametrize("operacion", ["historial", "enviar"])
def test_descarta_resultados_si_se_revoca_durante_mongo(chat, monkeypatch, operacion):
    ident = crear(chat)
    metodo = chat.chat._mensajes.historial if operacion == "historial" else chat.chat._mensajes.guardar

    def revocar(*args, **kwargs):
        resultado = metodo(*args, **kwargs)
        # Misma conexión revertida de prueba: simula el cambio observado al revalidar.
        with chat.conns.pg_session() as tx:
            tx.execute(text(
                "UPDATE sesion SET estado='REVOCADA',fin_en=clock_timestamp() WHERE id=:id"
            ), {"id": chat.ops.sesion_id})
        return resultado

    monkeypatch.setattr(chat.chat._mensajes, operacion if operacion == "historial" else "guardar", revocar)
    with pytest.raises(SessionExpiredError):
        if operacion == "historial":
            chat.chat.historial(chat.ops.sesion_id, ident)
        else:
            enviar(chat, ident, chat.ops)


def test_historial_periodos_excluye_antes_del_ingreso_y_las_interrupciones(chat):
    ident = crear(chat)
    ahora = datetime.now(timezone.utc)
    with chat.conns.pg_session() as tx:
        tx.execute(text("""
            UPDATE conversacion_miembro SET ingreso_en=:desde,salida_en=:hasta
            WHERE conversacion_id=:id AND usuario_id=:usuario
        """), {"desde": ahora - timedelta(days=4), "hasta": ahora - timedelta(days=3),
              "id": ident, "usuario": chat.ops.usuario_id})
        chat.chat._repo.incorporar(tx, ident, chat.ops.usuario_id, "MIEMBRO")
        tx.execute(text("""
            UPDATE conversacion_miembro SET ingreso_en=:desde
            WHERE conversacion_id=:id AND usuario_id=:usuario AND salida_en IS NULL
        """), {"desde": ahora - timedelta(days=1), "id": ident, "usuario": chat.ops.usuario_id})
    for dias in (5, 3.5, 2, 0.5):
        chat.chat._mensajes.guardar(MensajeDTO(
            uuid.uuid4(), ident, chat.admin.usuario_id, ahora - timedelta(days=dias), str(dias)
        ))
    assert {m.contenido for m in chat.chat.historial(chat.ops.sesion_id, ident).mensajes} == {"3.5", "0.5"}


def test_paginacion_fechas_iguales_con_nuevos_mensajes_y_corte_fijo(chat):
    ident = crear(chat)
    fecha = datetime.now(timezone.utc) - timedelta(seconds=1)
    ids = sorted(uuid.uuid4() for _ in range(7))
    with chat.conns.pg_session() as tx:
        tx.execute(text(
            "UPDATE conversacion_miembro SET ingreso_en=:fecha WHERE conversacion_id=:id"
        ), {"fecha": fecha - timedelta(seconds=1), "id": ident})
    for mensaje_id in ids:
        chat.chat._mensajes.guardar(MensajeDTO(
            mensaje_id, ident, chat.ops.usuario_id, fecha, "Misma fecha"
        ))
    pagina = chat.chat.historial(chat.ops.sesion_id, ident, limite=3)
    corte = pagina.hasta
    recibidos = [m.id for m in pagina.mensajes]
    nuevo = enviar(chat, ident, chat.ops)
    while pagina.siguiente:
        pagina = chat.chat.historial(
            chat.ops.sesion_id, ident, limite=3, antes=pagina.siguiente, hasta=corte
        )
        recibidos.extend(m.id for m in pagina.mensajes)
    assert recibidos == list(reversed(ids))
    assert nuevo.id not in recibidos
    assert len(recibidos) == len(set(recibidos))
    time.sleep(0.003)
    assert nuevo.id in {m.id for m in chat.chat.historial(chat.ops.sesion_id, ident).mensajes}


def test_indices_y_precision_de_limites(chat):
    indice = chat.conns.mongo_db["mensajes"].index_information()["ix_mensaje_conversacion_fecha_id"]
    assert indice["key"] == [("conversacion_id", 1), ("fecha_envio", 1), ("_id", 1)]
    f = datetime(2026, 1, 1, microsecond=123456, tzinfo=timezone.utc)
    assert techo_milisegundo(f).microsecond == 124000


def test_mongo_no_disponible_con_falla_de_conexion_real_y_recuperacion(chat):
    from dataclasses import replace

    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections
    from logistica.repositories.mongo.mensajes import MensajesRepository

    cfg = load_config()
    # Puerto reservado sin aceptar conexiones: no se detiene ningún motor compartido.
    import socket

    with socket.socket() as reserva:
        reserva.bind(("127.0.0.1", 0))
        puerto = reserva.getsockname()[1]
        caida = Connections(replace(
            cfg, mongo=replace(cfg.mongo, host="127.0.0.1", port=puerto), db_timeout_s=0.1,
        ))
        ident = crear(chat)
        original = chat.chat._mensajes
        chat.chat._mensajes = MensajesRepository(caida)
        mensaje_id = uuid.uuid4()
        try:
            with pytest.raises(DatabaseUnavailableError):
                chat.chat.enviar(chat.ops.sesion_id, ident, mensaje_id, "Recuperación")
            with pytest.raises(DatabaseUnavailableError):
                chat.chat.historial(chat.ops.sesion_id, ident)
        finally:
            chat.chat._mensajes = original
            caida.close()
    chat.chat.enviar(chat.ops.sesion_id, ident, mensaje_id, "Recuperación")
    assert chat.conns.mongo_db["mensajes"].count_documents({"conversacion_id": ident}) == 1
