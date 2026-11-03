"""Fase 2 — serialización de operaciones concurrentes (locks de PostgreSQL).

Qué se prueba y qué no:
- Con DOS conexiones reales (identidad PG_APP_USER): mientras una transacción tiene el lock de un envío
  o de un contenedor, otra no puede tomarlo. Es el mecanismo que serializa las operaciones del servicio.
- En el código: cada operación de EnvioService que modifica un envío toma ese lock (`_bloquear`), y la
  asignación de contenedores bloquea además el contenedor.
- NO se prueba una carrera completa entre dos llamadas al servicio: los servicios escriben en
  `auditoria`, que es inmutable, y la prueba dejaría datos permanentes en la base de desarrollo. Los
  datos mínimos de esta prueba se crean y se borran con la identidad de administración
  (LOGISTICA_TEST_PG).

Requiere LOGISTICA_TEST_CONNECTIONS=1 y LOGISTICA_TEST_PG.
"""

from __future__ import annotations

import inspect
import os
import uuid

import psycopg
import pytest

ADMIN = os.environ.get("LOGISTICA_TEST_PG")
pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1" or not ADMIN,
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 y LOGISTICA_TEST_PG "
    "(datos confirmados, se borran al terminar)",
)

MUTANTES = (
    "definir_tramos",
    "asignar_vehiculo",
    "asignar_contenedor",
    "cancelar_asignacion",
    "planificar",
    "iniciar_tramo",
    "finalizar_tramo",
    "confirmar_entrega",
    "cancelar",
    "registrar_evento",
)


@pytest.fixture()
def datos():
    """Un envío y un contenedor confirmados (visibles para dos conexiones), borrados al terminar."""
    ids = {k: uuid.uuid4() for k in ("tercero", "usuario", "origen", "destino", "envio", "contenedor")}
    sufijo = ids["envio"].hex[:8]
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(
            "INSERT INTO tercero (id, razon_social, pais_codigo, tipo_identificacion, numero_identificacion)"
            " VALUES (%s, %s, 'AR', 'CUIT', %s)",
            (ids["tercero"], f"Conc {sufijo}", f"99-{sufijo}"),
        )
        c.execute(
            "INSERT INTO usuario (id, nombre, apellido, email, password_hash) VALUES (%s, 'C', 'C', %s, 'x')",
            (ids["usuario"], f"conc-{sufijo}@test.local"),
        )
        for clave in ("origen", "destino"):
            c.execute(
                "INSERT INTO ubicacion (id, nombre, tipo, pais_codigo, ciudad) VALUES (%s, %s, 'OTRO', "
                "'AR', 'X')",
                (ids[clave], f"{clave} {sufijo}"),
            )
        c.execute(
            "INSERT INTO envio (id, codigo, cliente_id, origen_id, destino_id, descripcion_carga, peso_kg,"
            " volumen_m3, creado_por_id) VALUES (%s, %s, %s, %s, %s, 'x', 1, 1, %s)",
            (ids["envio"], f"CONC-{sufijo}", ids["tercero"], ids["origen"], ids["destino"], ids["usuario"]),
        )
        c.execute(
            "INSERT INTO contenedor (id, codigo_internacional, tipo, capacidad_kg, capacidad_m3)"
            " VALUES (%s, %s, 'SECO', 1, 1)",
            (ids["contenedor"], f"CONC{sufijo}"),
        )
    try:
        yield ids
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute("DELETE FROM envio WHERE id = %s", (ids["envio"],))
            c.execute("DELETE FROM contenedor WHERE id = %s", (ids["contenedor"],))
            c.execute("DELETE FROM ubicacion WHERE id IN (%s, %s)", (ids["origen"], ids["destino"]))
            c.execute("DELETE FROM usuario WHERE id = %s", (ids["usuario"],))
            c.execute("DELETE FROM tercero WHERE id = %s", (ids["tercero"],))


def _segunda_espera(bloquear_t1, bloquear_t2):
    """True si T2 NO consigue el lock mientras T1 lo tiene, y sí lo consigue cuando T1 termina."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections

    conns = Connections(load_config())
    try:
        with Session(conns.engine) as t1, Session(conns.engine) as t2:
            bloquear_t1(t1)
            t2.execute(text("SET LOCAL lock_timeout = '300ms'"))
            try:
                bloquear_t2(t2)
                return False  # T2 obtuvo el lock con T1 todavía adentro: no hay serialización
            except OperationalError as exc:
                assert "lock timeout" in str(exc) or "canceling statement" in str(exc)
            t2.rollback()
            t1.rollback()  # T1 termina: libera el lock
            bloquear_t2(t2)
            t2.rollback()
            return True
    finally:
        conns.close()


def test_dos_operaciones_sobre_el_mismo_envio_se_serializan(datos):
    from logistica.repositories.postgres.envios import EnvioRepository

    repo = EnvioRepository()
    assert _segunda_espera(
        lambda tx: repo.obtener(tx, datos["envio"], bloquear=True),
        lambda tx: repo.obtener(tx, datos["envio"], bloquear=True),
    )


def test_dos_asignaciones_del_mismo_contenedor_se_serializan(datos):
    from logistica.repositories.postgres.catalogos import ContenedorRepository

    repo = ContenedorRepository()
    assert _segunda_espera(
        lambda tx: repo.obtener(tx, datos["contenedor"], bloquear=True),
        lambda tx: repo.obtener(tx, datos["contenedor"], bloquear=True),
    )


@pytest.mark.parametrize("metodo", MUTANTES)
def test_cada_operacion_que_modifica_un_envio_toma_su_lock(metodo):
    from logistica.services.envio_service import EnvioService

    fuente = inspect.getsource(getattr(EnvioService, metodo))
    assert "self._bloquear(" in fuente, f"{metodo} no bloquea el envío antes de modificarlo"


def test_asignar_contenedor_bloquea_tambien_el_contenedor():
    from logistica.services.envio_service import EnvioService

    assert "obtener(tx, d.contenedor_id, bloquear=True)" in inspect.getsource(EnvioService.asignar_contenedor)


def test_dos_trabajadores_simultaneos_se_reparten_la_outbox_sin_repetir(datos):
    """Fase 3: reclamo con FOR UPDATE SKIP LOCKED desde DOS conexiones reales a la vez."""
    from sqlalchemy.orm import Session

    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections
    from logistica.repositories.postgres.outbox import NEO4J, OutboxRepository

    eventos = [uuid.uuid4() for _ in range(10)]
    with psycopg.connect(ADMIN, autocommit=True) as c:
        for version, evento_id in enumerate(eventos, start=1):
            c.execute(
                "INSERT INTO outbox_evento (id, agregado_tipo, agregado_id, agregado_version, evento_tipo,"
                " payload) VALUES (%s, 'ENVIO', %s, %s, 'PRUEBA', '{}')",
                (evento_id, datos["envio"], version),
            )
            c.execute("INSERT INTO outbox_entrega (evento_id, destino) VALUES (%s, %s)", (evento_id, NEO4J))
    conns = Connections(load_config())
    repo = OutboxRepository()
    try:
        with Session(conns.engine) as t1, Session(conns.engine) as t2:
            a = repo.reclamar(
                t1, NEO4J, propietario_id=uuid.uuid4(), limite=5, reserva_s=60, agregado_id=datos["envio"]
            )  # T1 todavía no confirmó: mantiene los locks
            b = repo.reclamar(
                t2, NEO4J, propietario_id=uuid.uuid4(), limite=10, reserva_s=60, agregado_id=datos["envio"]
            )
            assert len(a) == 5 and len(b) == 5  # el límite se respeta y T2 no espera: salta las bloqueadas
            assert not {x.evento_id for x in a} & {x.evento_id for x in b}
            t1.rollback()
            t2.rollback()
    finally:
        conns.close()
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute("DELETE FROM outbox_entrega WHERE evento_id = ANY(%s)", (eventos,))
            c.execute("DELETE FROM outbox_evento WHERE id = ANY(%s)", (eventos,))


def test_la_lectura_para_el_grafo_espera_a_un_cambio_en_curso_del_envio(datos):
    """Fase 3, D1: el subgrafo se arma desde UN estado consistente del envío.

    Mientras una operación tiene el envío bloqueado (FOR UPDATE), la lectura para Neo4j (FOR SHARE) espera;
    así nunca mezcla tramos de antes con contenedores de después.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from logistica.core.config import load_config
    from logistica.repositories.connections import Connections
    from logistica.repositories.postgres.envios import EnvioRepository
    from logistica.services.sincronizacion_service import SincronizacionService

    conns = Connections(load_config())
    try:
        with Session(conns.engine) as operacion, Session(conns.engine) as lector:
            EnvioRepository().obtener(operacion, datos["envio"], bloquear=True)  # cambio en curso
            lector.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(OperationalError, match="lock timeout|canceling statement"):
                SincronizacionService(conns)._grafo_de(lector, datos["envio"])
            lector.rollback()
            operacion.rollback()  # el cambio termina
            assert SincronizacionService(conns)._grafo_de(lector, datos["envio"]) is not None
            lector.rollback()
    finally:
        conns.close()
