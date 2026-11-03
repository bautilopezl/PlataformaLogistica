"""Fase 3 — sincronización por outbox: reclamo atómico, histórico en Mongo, proyección en Neo4j, recuperación.

Aceptación (AGENTS.md §17): con Neo4j caído se sigue operando; al volver, el grafo converge sin duplicados.
"Caído" se simula con un driver que apunta a un puerto donde no escucha nadie (falla real de conexión) en
lugar de detener el contenedor, para que la prueba sea automática y no afecte a otras.

PostgreSQL corre en la transacción revertida de `tx_connections`; los nodos de Neo4j y los documentos de Mongo
que crean estas pruebas se borran al terminar. Requiere LOGISTICA_TEST_CONNECTIONS=1 y los tres motores.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from logistica.domain.logistica import TramoPlan
from logistica.repositories.postgres.outbox import MONGO, NEO4J
from logistica.services.sincronizacion_service import SincronizacionService
from tests.test_fase2_logistica import Logistica, h

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y los tres motores levantados)",
)


@pytest.fixture()
def g(tx_connections):
    entorno = Logistica(tx_connections)
    entorno.envios_creados = []
    yield entorno
    _limpiar(entorno)


def _limpiar(g):
    """Borra lo que las pruebas dejaron en Neo4j y Mongo (PostgreSQL se revierte solo)."""
    ids = [str(x) for x in g.envios_creados]
    with g.conns.neo4j.session() as s:
        s.run("MATCH (t:Tramo) WHERE t.envio_id IN $ids DETACH DELETE t", ids=ids).consume()
        s.run("MATCH (e:Envio) WHERE e.id IN $ids DETACH DELETE e", ids=ids).consume()
        otros = [
            str(x.id) for x in (g.cliente_sa, g.operador_sa, g.operador2, g.A, g.B, g.C, g.cont1, g.cont2)
        ]
        s.run("MATCH (n) WHERE n.id IN $ids AND NOT (n)--() DELETE n", ids=otros).consume()
    import uuid

    g.conns.mongo_db["eventos_logisticos"].delete_many({"envio_id": {"$in": [uuid.UUID(i) for i in ids]}})


def sync(g, **kwargs) -> SincronizacionService:
    return SincronizacionService(g.conns, **kwargs)


def completo(g):
    """Un envío entregado de punta a punta (deja eventos para ambos destinos)."""
    s = g.ops.sesion_id
    p = g.planificado()
    g.envios_creados.append(p.id)
    t1, t2 = g.env.obtener(s, p.id).tramos
    g.env.iniciar_tramo(s, t1.id, h(0.5))
    g.env.registrar_evento(s, p.id, "RETRASO", h(1), {"motivo": "Paro"}, tramo_id=t1.id)
    g.env.finalizar_tramo(s, t1.id, h(5))
    g.env.iniciar_tramo(s, t2.id, h(6))
    g.env.finalizar_tramo(s, t2.id, h(80))
    g.env.confirmar_entrega(s, p.id, h(81))
    return p


def estados(g, envio_id):
    from sqlalchemy import text

    with g.conns.pg_session() as s:
        rows = s.execute(
            text(
                "SELECT x.destino, x.estado, count(*) FROM outbox_entrega x JOIN outbox_evento e"
                " ON e.id = x.evento_id WHERE e.agregado_id = :id GROUP BY 1, 2"
            ),
            {"id": envio_id},
        )
        return {(r[0], r[1]): r[2] for r in rows}


def reabrir(g, envio_id, destino):
    """Simula una reentrega: vuelve a dejar PENDIENTES las entregas ya completadas."""
    from sqlalchemy import text

    with g.conns.pg_session() as s:
        s.execute(
            text(
                "UPDATE outbox_entrega x SET estado = 'PENDIENTE', procesada_en = NULL,"
                " proximo_intento_en = clock_timestamp() FROM outbox_evento e"
                " WHERE e.id = x.evento_id AND e.agregado_id = :id AND x.destino = :d"
            ),
            {"id": envio_id, "d": destino},
        )


# --------------------------------------------------------------------------- reclamo
def test_dos_trabajadores_no_reclaman_la_misma_entrega(g):
    from logistica.repositories.postgres import unit_of_work
    from logistica.repositories.postgres.outbox import OutboxRepository

    p = completo(g)
    repo = OutboxRepository()
    with unit_of_work(g.conns) as tx:
        a = repo.reclamar(tx, NEO4J, propietario_id=_uuid(), limite=3, reserva_s=60, agregado_id=p.id)
        b = repo.reclamar(tx, NEO4J, propietario_id=_uuid(), limite=50, reserva_s=60, agregado_id=p.id)
    assert len(a) == 3
    assert not {x.evento_id for x in a} & {x.evento_id for x in b}


def test_solo_completa_quien_todavia_es_duenio_del_reclamo(g):
    from sqlalchemy import text

    from logistica.repositories.postgres import unit_of_work
    from logistica.repositories.postgres.outbox import OutboxRepository

    p = completo(g)
    repo = OutboxRepository()
    yo, otro = _uuid(), _uuid()
    with unit_of_work(g.conns) as tx:
        mio = repo.reclamar(tx, NEO4J, propietario_id=yo, limite=1, reserva_s=60, agregado_id=p.id)[0]
        # Mi reserva vence (me colgué) y otro trabajador la recupera.
        tx.execute(
            text(
                "UPDATE outbox_entrega SET bloqueada_hasta = clock_timestamp() - interval '1 second'"
                " WHERE evento_id = :e AND destino = :d"
            ),
            {"e": mio.evento_id, "d": NEO4J},
        )
        suyo = repo.reclamar(tx, NEO4J, propietario_id=otro, limite=50, reserva_s=60, agregado_id=p.id)
        assert mio.evento_id in {x.evento_id for x in suyo}
        recuperado = next(x for x in suyo if x.evento_id == mio.evento_id)
        assert recuperado.version_reclamo == mio.version_reclamo + 1
        assert not repo.completar(tx, mio.evento_id, NEO4J, yo, mio.version_reclamo)  # ya no es mío
        assert repo.completar(tx, mio.evento_id, NEO4J, otro, recuperado.version_reclamo)


# --------------------------------------------------------------------------- Mongo: histórico
def test_eventos_llegan_a_mongo_una_sola_vez_aunque_se_reentreguen(g):
    import uuid

    p = completo(g)
    r = sync(g).procesar(MONGO, agregado_id=p.id)
    assert r.fallidos == 0 and r.procesados == 6  # iniciado x2, retraso, finalizado x2, entregado
    coleccion = g.conns.mongo_db["eventos_logisticos"]
    tipos = sorted(d["tipo"] for d in coleccion.find({"envio_id": uuid.UUID(str(p.id))}))
    assert tipos == [
        "ENTREGADO",
        "RETRASO",
        "TRAMO_FINALIZADO",
        "TRAMO_FINALIZADO",
        "TRAMO_INICIADO",
        "TRAMO_INICIADO",
    ]
    antes = {d["_id"]: d for d in coleccion.find({"envio_id": uuid.UUID(str(p.id))})}
    reabrir(g, p.id, MONGO)
    assert sync(g).procesar(MONGO, agregado_id=p.id).procesados == 6
    despues = {d["_id"]: d for d in coleccion.find({"envio_id": uuid.UUID(str(p.id))})}
    assert despues == antes  # ni duplicados ni modificaciones (incluida fecha_registro)
    assert coleccion.count_documents({"envio_id": uuid.UUID(str(p.id))}) == 6  # sin duplicados
    assert estados(g, p.id)[(MONGO, "COMPLETADA")] == 6


# --------------------------------------------------------------------------- Neo4j: proyección
def test_proyeccion_del_envio_en_neo4j(g):
    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    p = completo(g)
    r = sync(g).procesar(NEO4J, agregado_id=p.id)
    assert r.fallidos == 0 and r.procesados > 0
    grafo = ProyeccionRepository(g.conns).subgrafo(p.id)
    det = g.env.obtener(g.ops.sesion_id, p.id)
    assert grafo["estado"] == "ENTREGADO" and grafo["version"] == det.envio.version
    assert grafo["cliente"] == str(g.cliente_sa.id)
    assert grafo["tramos"] == [
        (t.orden, str(t.origen_id), str(t.destino_id), str(t.operador_id), t.estado) for t in det.tramos
    ]
    assert grafo["siguientes"] == [(1, 2)]
    assert grafo["utiliza"] == [(str(a.id), str(a.contenedor_id)) for a in det.asignaciones]
    assert grafo["vehiculos"] == [t.vehiculo_identificacion for t in det.tramos]


def test_redefinir_tramos_reemplaza_los_nodos_sin_dejar_huerfanos(g):
    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    s = g.ops.sesion_id
    e = g.nuevo_envio()
    g.envios_creados.append(e.id)
    g.ruta(e.id)
    sync(g).procesar(NEO4J, agregado_id=e.id)
    directo = TramoPlan(g.A.id, g.C.id, "MARITIMO", g.operador_sa.id, h(0), h(70), g.barco.id)
    g.env.definir_tramos(s, e.id, [directo])
    sync(g).procesar(NEO4J, agregado_id=e.id)
    grafo = ProyeccionRepository(g.conns).subgrafo(e.id)
    assert [t[:3] for t in grafo["tramos"]] == [(1, str(g.A.id), str(g.C.id))]
    assert grafo["tramos_huerfanos"] == 0 and grafo["siguientes"] == []


def test_cancelar_una_asignacion_quita_la_relacion_utiliza(g):
    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    e = g.nuevo_envio()
    g.envios_creados.append(e.id)
    a = g.asignar(e.id, g.cont1)
    sync(g).procesar(NEO4J, agregado_id=e.id)
    assert len(ProyeccionRepository(g.conns).subgrafo(e.id)["utiliza"]) == 1
    g.env.cancelar_asignacion(g.ops.sesion_id, a.id)
    sync(g).procesar(NEO4J, agregado_id=e.id)
    assert ProyeccionRepository(g.conns).subgrafo(e.id)["utiliza"] == []


def test_un_evento_viejo_reentregado_no_hace_retroceder_el_grafo(g):
    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    p = completo(g)
    sync(g).procesar(NEO4J, agregado_id=p.id)
    antes = ProyeccionRepository(g.conns).subgrafo(p.id)
    reabrir(g, p.id, NEO4J)  # todos los eventos, incluidos los viejos, llegan de nuevo
    r = sync(g).procesar(NEO4J, agregado_id=p.id)
    assert r.fallidos == 0
    assert ProyeccionRepository(g.conns).subgrafo(p.id) == antes


# --------------------------------------------------------------------------- aceptación: Neo4j caído
def test_con_neo4j_caido_se_opera_y_al_volver_el_grafo_converge_sin_duplicados(g):
    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections
    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    cfg = load_config()
    caido = Connections(replace(cfg, neo4j=replace(cfg.neo4j, uri="bolt://127.0.0.1:1"), db_timeout_s=1.0))
    try:
        # 1) Neo4j no responde: la operación de negocio igual se confirma (PostgreSQL decide).
        p = completo(g)
        r = SincronizacionService(g.conns, neo4j=caido).procesar(NEO4J, agregado_id=p.id)
        assert (
            r.procesados == 0 and r.fallidos == 0 and r.pospuestos > 0
        )  # se reintentan, sin gastar intentos
        pendientes = estados(g, p.id)
        assert (NEO4J, "COMPLETADA") not in pendientes and pendientes[(NEO4J, "PENDIENTE")] > 0
        assert _ultimo_error(g, p.id) and "Clave" not in _ultimo_error(g, p.id)
        # Mongo sigue funcionando aunque Neo4j esté caído.
        assert sync(g).procesar(MONGO, agregado_id=p.id).fallidos == 0
    finally:
        caido.close()
    # 2) Neo4j vuelve: los reintentos (ya vencidos) se procesan y el grafo converge.
    _vencer_esperas(g, p.id)
    r = sync(g).procesar(NEO4J, agregado_id=p.id)
    assert r.fallidos == 0
    assert (NEO4J, "PENDIENTE") not in estados(g, p.id)
    grafo = ProyeccionRepository(g.conns).subgrafo(p.id)
    det = g.env.obtener(g.ops.sesion_id, p.id)
    assert grafo["estado"] == "ENTREGADO" and len(grafo["tramos"]) == len(det.tramos)
    assert grafo["nodos_envio"] == 1 and grafo["tramos_huerfanos"] == 0  # sin duplicados


def test_una_caida_larga_del_destino_no_agota_los_intentos(g):
    """Revisión, bug 1: que Neo4j no responda NO es culpa del evento.

    No consume intentos ni lo vuelve FALLIDO.
    """
    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections

    cfg = load_config()
    caido = Connections(replace(cfg, neo4j=replace(cfg.neo4j, uri="bolt://127.0.0.1:1"), db_timeout_s=1.0))
    try:
        e = g.nuevo_envio()
        g.envios_creados.append(e.id)
        servicio = SincronizacionService(g.conns, neo4j=caido, max_intentos=2)
        for _ in range(5):  # más vueltas que intentos permitidos
            servicio.procesar(NEO4J, agregado_id=e.id)
            _vencer_esperas(g, e.id)
    finally:
        caido.close()
    assert estados(g, e.id).get((NEO4J, "FALLIDA"), 0) == 0
    assert estados(g, e.id)[(NEO4J, "PENDIENTE")] == 1
    assert sync(g).procesar(NEO4J, agregado_id=e.id).procesados == 1  # al volver, se aplica


def _evento_invalido_para_mongo(g, envio_id):
    """Un evento cuyo documento el validador de Mongo rechaza siempre (error del evento, no del destino)."""
    import json
    import uuid

    from sqlalchemy import text

    evento_id = uuid.uuid4()
    payload = {
        "event_id": str(evento_id),
        "tipo": "TIPO_INEXISTENTE",
        "envio_id": str(envio_id),
        "fecha_ocurrencia": "2026-12-01T08:00:00+00:00",
        "detalles": {},
    }
    with g.conns.pg_session() as s:
        s.execute(
            text(
                "INSERT INTO outbox_evento (id, agregado_tipo, agregado_id, agregado_version, evento_tipo,"
                " payload) VALUES (:id, 'ENVIO', :a, 999, 'PRUEBA', CAST(:p AS jsonb))"
            ),
            {"id": evento_id, "a": envio_id, "p": json.dumps(payload)},
        )
        s.execute(
            text("INSERT INTO outbox_entrega (evento_id, destino) VALUES (:id, 'MONGO_EVENTOS')"),
            {"id": evento_id},
        )
    return evento_id


def test_un_evento_invalido_termina_fallido_sin_bloquear_a_los_demas(g):
    p = completo(g)
    _evento_invalido_para_mongo(g, p.id)
    servicio = SincronizacionService(g.conns, max_intentos=2)
    primera = servicio.procesar(MONGO, agregado_id=p.id)
    assert primera.procesados == 6 and primera.fallidos == 1  # los válidos pasan igual
    _vencer_esperas(g, p.id)
    servicio.procesar(MONGO, agregado_id=p.id)
    assert estados(g, p.id)[(MONGO, "FALLIDA")] == 1  # agotó sus intentos: queda para revisar
    assert "ValidationError" in _ultimo_error_mongo(g, p.id)


def test_las_fallidas_se_pueden_reencolar(g):
    p = completo(g)
    _evento_invalido_para_mongo(g, p.id)
    servicio = SincronizacionService(g.conns, max_intentos=1)
    servicio.procesar(MONGO, agregado_id=p.id)
    assert estados(g, p.id)[(MONGO, "FALLIDA")] == 1
    assert servicio.reintentar_fallidas(MONGO) >= 1
    assert estados(g, p.id).get((MONGO, "FALLIDA"), 0) == 0


def test_un_error_inesperado_no_traba_el_lote(g, monkeypatch):
    """Revisión, bug 2: una excepción no prevista se registra como fallo de ESA entrega y el resto sigue."""
    p = completo(g)
    servicio = sync(g)
    original = servicio._eventos.guardar
    llamadas = []

    def falla_una_vez(doc):
        llamadas.append(doc["tipo"])
        if len(llamadas) == 1:
            raise TypeError("bug inesperado")
        return original(doc)

    monkeypatch.setattr(servicio._eventos, "guardar", falla_una_vez)
    r = servicio.procesar(MONGO, agregado_id=p.id)
    assert r.fallidos == 1 and r.procesados == 5
    assert estados(g, p.id).get((MONGO, "EN_PROCESO"), 0) == 0  # nada quedó trabado


def test_procesar_todo_sigue_con_neo4j_aunque_mongo_falle(g, monkeypatch):
    p = completo(g)
    servicio = sync(g)

    def mongo_roto(*args, **kwargs):
        raise RuntimeError("Mongo roto")

    monkeypatch.setattr(
        servicio,
        "procesar",
        lambda destino, **kw: (
            mongo_roto()
            if destino == MONGO
            else SincronizacionService.procesar(servicio, destino, agregado_id=p.id)
        ),
    )
    resultado = servicio.procesar_todo()
    assert resultado[NEO4J].procesados > 0 and resultado[MONGO].fallidos == 0


def test_la_guarda_de_version_rechaza_un_estado_mas_viejo(g):
    """Revisión: probar la guarda directamente (el servicio siempre relee el estado actual)."""
    from dataclasses import replace as cambiar

    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    e = g.nuevo_envio()
    g.envios_creados.append(e.id)
    with g.conns.pg_session() as s:
        nuevo = sync(g)._grafo_de(s, e.id)
    repo = ProyeccionRepository(g.conns)
    v = nuevo.envio["version"]
    viejo = cambiar(nuevo, envio={**nuevo.envio, "version": v - 1 if v > 1 else 0, "estado": "VIEJO"})
    assert repo.aplicar(nuevo) is True
    assert repo.aplicar(viejo) is False and repo.subgrafo(e.id)["estado"] == "BORRADOR"
    assert repo.aplicar(viejo, forzar=True) is False  # forzar permite IGUAL, nunca más viejo
    assert repo.aplicar(nuevo, forzar=True) is True


def test_dos_proyecciones_simultaneas_gana_la_version_mayor(g):
    """Revisión, bug 3: dos trabajadores a la vez con versiones distintas: queda la mayor, siempre."""
    import threading
    from dataclasses import replace as cambiar

    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    e = g.nuevo_envio()
    g.envios_creados.append(e.id)
    with g.conns.pg_session() as s:
        base = sync(g)._grafo_de(s, e.id)
    repo = ProyeccionRepository(g.conns)
    for ronda in range(10):
        v = 100 + ronda * 2
        viejo = cambiar(base, envio={**base.envio, "version": v, "estado": "VIEJO"})
        nuevo = cambiar(base, envio={**base.envio, "version": v + 1, "estado": "NUEVO"})
        largada = threading.Barrier(2)

        def aplicar(grafo):
            largada.wait()
            repo.aplicar(grafo)

        hilos = [threading.Thread(target=aplicar, args=(x,)) for x in (nuevo, viejo)]
        for h_ in hilos:
            h_.start()
        for h_ in hilos:
            h_.join()
        final = repo.subgrafo(e.id)
        assert (final["version"], final["estado"]) == (v + 1, "NUEVO"), f"ronda {ronda}"


def test_reconstruccion_completa_borra_solo_lo_que_no_existe_en_postgresql(g):
    import uuid

    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    p = completo(g)
    sync(g).procesar(NEO4J, agregado_id=p.id)
    basura = str(uuid.uuid4())
    with g.conns.neo4j.session() as s:
        s.run("CREATE (:Envio {id: $id, version: 1})", id=basura).consume()
    try:
        sync(g).reconstruir_grafo()
        with g.conns.neo4j.session() as s:
            assert s.run("MATCH (e:Envio {id: $id}) RETURN count(e) AS n", id=basura).single()["n"] == 0
        assert ProyeccionRepository(g.conns).subgrafo(p.id)["estado"] == "ENTREGADO"  # el real sigue
    finally:
        with g.conns.neo4j.session() as s:
            s.run("MATCH (e:Envio {id: $id}) DETACH DELETE e", id=basura).consume()


# --------------------------------------------------------------------------- reconstrucción
def test_reconstruir_el_grafo_desde_postgresql(g):
    from logistica.repositories.neo4j.proyeccion import ProyeccionRepository

    p = completo(g)
    sync(g).procesar(NEO4J, agregado_id=p.id)
    esperado = ProyeccionRepository(g.conns).subgrafo(p.id)
    with g.conns.neo4j.session() as s:  # se pierde la proyección (por ejemplo, Neo4j recreado)
        s.run("MATCH (t:Tramo {envio_id: $id}) DETACH DELETE t", id=str(p.id)).consume()
        s.run("MATCH (e:Envio {id: $id}) DETACH DELETE e", id=str(p.id)).consume()
    proyectados = sync(g).reconstruir_grafo(envio_ids=[p.id])
    assert proyectados == 1
    assert ProyeccionRepository(g.conns).subgrafo(p.id) == esperado


def test_resumen_de_la_cola(g):
    p = completo(g)
    resumen = sync(g).resumen(agregado_id=p.id)
    assert resumen[NEO4J]["PENDIENTE"] > 0 and resumen[MONGO]["PENDIENTE"] == 6


# --------------------------------------------------------------------------- auxiliares
def _uuid():
    import uuid

    return uuid.uuid4()


def _ultimo_error(g, envio_id) -> str:
    from sqlalchemy import text

    with g.conns.pg_session() as s:
        return (
            s.execute(
                text(
                    "SELECT string_agg(x.ultimo_error, ' | ') FROM outbox_entrega x JOIN outbox_evento e"
                    " ON e.id = x.evento_id WHERE e.agregado_id = :id AND x.destino = 'NEO4J_LOGISTICA'"
                ),
                {"id": envio_id},
            ).scalar()
            or ""
        )


def _vencer_esperas(g, envio_id):
    """Adelanta el reloj de los reintentos (en vez de esperar el backoff real)."""
    from sqlalchemy import text

    with g.conns.pg_session() as s:
        s.execute(
            text(
                "UPDATE outbox_entrega x SET proximo_intento_en = clock_timestamp() - interval '1 second'"
                " FROM outbox_evento e WHERE e.id = x.evento_id AND e.agregado_id = :id"
            ),
            {"id": envio_id},
        )


def _ultimo_error_mongo(g, envio_id) -> str:
    from sqlalchemy import text

    with g.conns.pg_session() as s:
        return (
            s.execute(
                text(
                    "SELECT string_agg(x.ultimo_error, ' | ') FROM outbox_entrega x JOIN outbox_evento e"
                    " ON e.id = x.evento_id WHERE e.agregado_id = :id AND x.destino = 'MONGO_EVENTOS'"
                ),
                {"id": envio_id},
            ).scalar()
            or ""
        )
