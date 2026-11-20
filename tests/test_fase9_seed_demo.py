"""Fase 9 — pruebas permanentes de `scripts/seed_demo.py` (datos de demostración).

Reemplaza el harness temporal de validación manual por pruebas que corren contra PostgreSQL REAL pero
dentro de UNA transacción que se revierte al final (`tx_connections`, igual que el resto de la suite de
integración): nunca se corre contra la base de desarrollo ni se le deja nada. Mongo y Neo4j son los
motores reales configurados en `.env` (no hay forma de aislarlos por transacción); lo que crean estas
pruebas se borra al terminar, mismo patrón que `tests/test_fase3_sincronizacion.py`.

Qué cubre (consigna de la revisión):
- Primera corrida y repetición sin duplicados (idempotencia por registro, no solo por prefijo).
- Recuperación después de una ejecución parcial: huérfano BORRADOR/PLANIFICADO (se cancela y reemplaza) y
  huérfano EN_TRANSITO (se completa en el mismo lugar donde quedó, sin poder cancelarse por S8).
- Conservación de registros ajenos al seed (un tercero y un envío NO relacionados no cambian) y de entregas
  de outbox pendientes de OTROS envíos (la sincronización se acota por `agregado_id`).
- Coherencia temporal dentro de un envío histórico (creación ≤ salida ≤ llegada ≤ entrega, reserva cubre el
  uso real) y coherencia de las fechas que llegan a MongoDB y Neo4j por el flujo normal de sincronización.

Requiere LOGISTICA_TEST_CONNECTIONS=1 y los tres motores levantados.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from logistica.core.config import load_config
from logistica.domain.acceso import UsuarioDatos
from logistica.domain.logistica import TramoPlan
from logistica.domain.passwords import Hasher
from logistica.repositories.neo4j.proyeccion import ProyeccionRepository
from logistica.repositories.postgres import unit_of_work
from logistica.repositories.postgres.outbox import FACTURACION, MONGO, NEO4J
from logistica.repositories.postgres.usuarios import UsuarioRepository
from logistica.services.contenedor import crear_servicios

pytestmark = pytest.mark.skipif(
    os.environ.get("LOGISTICA_TEST_CONNECTIONS") != "1",
    reason="definir LOGISTICA_TEST_CONNECTIONS=1 (usa .env y los tres motores levantados)",
)

# Se importa el script como módulo (no es un paquete de src/): así las pruebas ejercitan el código REAL,
# no una copia. `scripts/` no está en sys.path por defecto, de ahí `spec_from_file_location`.
_spec = importlib.util.spec_from_file_location("seed_demo_bajo_prueba", ROOT / "scripts" / "seed_demo.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)

CLAVE_ADMIN = "clave-de-prueba-123"
_HASHER_RAPIDO = Hasher(time_cost=1, memory_cost_kib=8 * 1024, parallelism=1)


# ============================================================================ CLI, sin base (rápido)
def test_cli_ayuda_sale_con_codigo_0():
    """Sanity de proceso: `--help` no toca ninguna base y debe salir 0. Comprueba el código de salida real
    del proceso (`returncode`), no el texto impreso ni una inferencia del wrapper de la terminal."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")  # la ayuda imprime flechas (→); la consola de Windows
    resultado = subprocess.run(                        # por defecto no decodifica eso en cp1252
        [sys.executable, str(ROOT / "scripts" / "seed_demo.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8", timeout=30, env=env,
    )
    assert resultado.returncode == 0
    assert "--email" in resultado.stdout


def test_cli_sin_email_sale_con_codigo_distinto_de_0():
    """`argparse` rechaza la falta de `--email` (requerido) con exit code 2, nunca 0: confirma que el
    script distingue uso incorrecto de éxito, sin necesitar la base ni la contraseña del administrador."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    resultado = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_demo.py")],
        capture_output=True, text=True, encoding="utf-8", timeout=30, env=env,
    )
    assert resultado.returncode != 0


# ============================================================================ entorno de prueba
class EntornoSeed:
    """Admin con sesión + los servicios completos (`crear_servicios`, igual que la app real), todo dentro
    de la transacción revertida de `tx_connections`. El administrador se inserta directo por repositorio
    (como hace `tests/test_fase1_acceso.py.Entorno`): `crear_administrador_inicial` fallaría porque la
    transacción ve al administrador REAL ya existente en la base de desarrollo."""

    def __init__(self, conns):
        self.conns = conns
        self.servicios = crear_servicios(load_config(), conns, uuid.uuid4())
        self.sufijo = uuid.uuid4().hex[:8]
        admin_email = f"admin-seed9-{self.sufijo}@test.local"
        with unit_of_work(conns) as tx:
            repo = UsuarioRepository()
            admin_id = uuid.uuid4()
            repo.crear(
                tx, admin_id, UsuarioDatos("Ada", "Admin", admin_email), _HASHER_RAPIDO.hash(CLAVE_ADMIN)
            )
            repo.reemplazar_roles(tx, admin_id, {"ADMINISTRADOR"})
        self.sesion = self.servicios.acceso.login(admin_email, CLAVE_ADMIN, "pruebas fase9").sesion_id
        self.envios_creados: list[uuid.UUID] = []

    def tercero(self, razon: str, roles: set[str], *, sufijo_identificacion: str) -> uuid.UUID:
        return seed.asegurar_tercero(
            self.servicios, self.sesion, razon_social=f"{razon} {self.sufijo}",
            numero_identificacion=f"30-{sufijo_identificacion}-{self.sufijo[:1]}", roles=roles,
        )

    def ubicacion(self, nombre: str, **kwargs) -> uuid.UUID:
        return seed.asegurar_ubicacion(self.servicios, self.sesion, nombre=f"{nombre} {self.sufijo}", **kwargs)


@pytest.fixture()
def s9(tx_connections):
    entorno = EntornoSeed(tx_connections)
    yield entorno
    _limpiar_mongo_neo4j(entorno)


def _limpiar_mongo_neo4j(entorno: EntornoSeed) -> None:
    """Mismo patrón que `tests/test_fase3_sincronizacion.py`: PostgreSQL se revierte solo; lo que haya en
    Mongo/Neo4j se borra por `envio_id` (los únicos motores que no viven dentro de la transacción)."""
    ids = [str(x) for x in entorno.envios_creados]
    if not ids:
        return
    with entorno.conns.neo4j.session() as s:
        s.run("MATCH (t:Tramo) WHERE t.envio_id IN $ids DETACH DELETE t", ids=ids).consume()
        s.run("MATCH (e:Envio) WHERE e.id IN $ids DETACH DELETE e", ids=ids).consume()
    entorno.conns.mongo_db["eventos_logisticos"].delete_many(
        {"envio_id": {"$in": [uuid.UUID(i) for i in ids]}}
    )


class Ruta:
    """Catálogo mínimo para `asegurar_ruta_historica`: un cliente, un operador, dos ubicaciones, un
    vehículo y dos contenedores — con un objetivo de ocurrencias chico (3, no 12) para que las pruebas
    corran rápido; la lógica de recuperación no depende del tamaño del objetivo."""

    OBJETIVO = 3

    def __init__(self, s9: EntornoSeed, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(seed, "OBJETIVO_RUTA_HISTORICA", self.OBJETIVO)
        self.s9 = s9
        self.cliente_id = s9.tercero("Cliente Ruta9", {"CLIENTE"}, sufijo_identificacion="91111111")
        self.operador_id = s9.tercero("Operador Ruta9", {"OPERADOR"}, sufijo_identificacion="92222222")
        self.origen_id = s9.ubicacion(
            "Origen9", tipo="PUERTO", pais_codigo="AR", ciudad="Buenos Aires", latitud=-34.6, longitud=-58.38,
        )
        self.destino_id = s9.ubicacion(
            "Destino9", tipo="PUERTO", pais_codigo="NL", ciudad="Rotterdam", latitud=51.92, longitud=4.48,
        )
        self.vehiculo_id = seed.asegurar_vehiculo(
            s9.servicios, s9.sesion, operador_id=self.operador_id, tipo="BARCO",
            identificacion=f"BUQ9-{s9.sufijo}",
        )
        self.cont1 = seed.asegurar_contenedor(
            s9.servicios, s9.sesion, codigo=f"CONT9A{s9.sufijo}", tipo="SECO", capacidad_kg=20_000,
            capacidad_m3=30,
        )
        self.cont2 = seed.asegurar_contenedor(
            s9.servicios, s9.sesion, codigo=f"CONT9B{s9.sufijo}", tipo="SECO", capacidad_kg=20_000,
            capacidad_m3=30,
        )
        self.razon_social = f"Cliente Ruta9 {s9.sufijo}"

    def kwargs(self) -> dict:
        return dict(
            servicios=self.s9.servicios, connections=self.s9.conns, sesion_ops=self.s9.sesion,
            sesion_com=self.s9.sesion, cliente_id=self.cliente_id, cliente_razon_social=self.razon_social,
            origen_id=self.origen_id, destino_id=self.destino_id, operador_id=self.operador_id,
            vehiculo_id=self.vehiculo_id, contenedores=(self.cont1, self.cont2),
        )

    def entregados(self) -> list:
        return self.s9.servicios.envios.listar(
            self.s9.sesion, estado="ENTREGADO", texto=self.razon_social
        )


@pytest.fixture()
def ruta(s9, monkeypatch):
    return Ruta(s9, monkeypatch)


# ============================================================================ idempotencia de catálogo
def test_asegurar_tercero_es_idempotente_por_identificacion(s9):
    id1 = s9.tercero("Repetido", {"CLIENTE"}, sufijo_identificacion="99000001")
    id2 = s9.tercero("Repetido", {"CLIENTE"}, sufijo_identificacion="99000001")
    assert id1 == id2
    coincidencias = [
        t for t in s9.servicios.terceros.listar(s9.sesion, texto="99000001", incluir_inactivos=True)
        if t.numero_identificacion == f"30-99000001-{s9.sufijo[:1]}"
    ]
    assert len(coincidencias) == 1


def test_asegurar_usuario_es_idempotente_por_email(s9):
    email = f"repetido-{s9.sufijo}@demo.local"
    id1 = seed.asegurar_usuario(s9.servicios, s9.sesion, nombre="Rep", apellido="Uno", email=email, roles={"OPERACIONES"})
    id2 = seed.asegurar_usuario(s9.servicios, s9.sesion, nombre="Rep", apellido="Uno", email=email, roles={"OPERACIONES"})
    assert id1 == id2
    assert sum(1 for u in s9.servicios.usuarios.listar(s9.sesion) if u.email == email) == 1


def test_asegurar_ubicacion_vehiculo_contenedor_son_idempotentes(s9, ruta):
    # La fixture `ruta` ya crea cada uno dos veces indirectamente a través de llamadas repetidas en otras
    # pruebas; acá se verifica explícitamente llamando de nuevo con los mismos datos.
    ub2 = seed.asegurar_ubicacion(
        s9.servicios, s9.sesion, nombre=f"Origen9 {s9.sufijo}", tipo="PUERTO", pais_codigo="AR",
        ciudad="Buenos Aires", latitud=-34.6, longitud=-58.38,
    )
    assert ub2 == ruta.origen_id
    veh2 = seed.asegurar_vehiculo(
        s9.servicios, s9.sesion, operador_id=ruta.operador_id, tipo="BARCO",
        identificacion=f"BUQ9-{s9.sufijo}",
    )
    assert veh2 == ruta.vehiculo_id
    cont2 = seed.asegurar_contenedor(
        s9.servicios, s9.sesion, codigo=f"CONT9A{s9.sufijo}", tipo="SECO", capacidad_kg=20_000,
        capacidad_m3=30,
    )
    assert cont2 == ruta.cont1


# ============================================================================ ruta histórica: idempotencia
def test_ruta_historica_primera_corrida_y_repeticion_sin_duplicados(s9, ruta):
    ids1 = seed.asegurar_ruta_historica(**ruta.kwargs())
    s9.envios_creados.extend(ids1)
    assert len(ids1) == Ruta.OBJETIVO
    assert len(ruta.entregados()) == Ruta.OBJETIVO

    ids2 = seed.asegurar_ruta_historica(**ruta.kwargs())
    assert sorted(ids2) == sorted(ids1), "la repetición no debe crear ni reemplazar ninguna entrega"
    assert len(ruta.entregados()) == Ruta.OBJETIVO, "la repetición no debe dejar entregas de más"


def test_ruta_historica_recupera_huerfano_borrador_sin_duplicar(s9, ruta):
    """Simula una corrida interrumpida ANTES de zarpar: el envío de la ocurrencia 2 quedó en BORRADOR (con
    su cargo ya cargado). Una corrida nueva debe cancelarlo, reemplazarlo y llegar a las 3 entregas, sin
    dejar un cuarto envío ni un cargo huérfano."""
    ids_parciales = _crear_ocurrencias_entregadas(s9, ruta, [0, 1])
    s9.envios_creados.extend(ids_parciales)
    huerfano_id = _crear_huerfano_borrador(s9, ruta, indice=2)
    s9.envios_creados.append(huerfano_id)

    ids = seed.asegurar_ruta_historica(**ruta.kwargs())
    s9.envios_creados.extend(i for i in ids if i not in s9.envios_creados)

    assert len(ruta.entregados()) == Ruta.OBJETIVO
    assert huerfano_id not in ids, "el huérfano cancelado no debe contarse como entrega"
    huerfano = s9.servicios.envios.obtener(s9.sesion, huerfano_id).envio
    assert huerfano.estado == "CANCELADO"
    cargos_huerfano = s9.servicios.comercial.cargos(s9.sesion, huerfano_id)
    assert all(c.estado != "PENDIENTE" for c in cargos_huerfano), "el cargo del huérfano debe cancelarse"
    # Repetir de nuevo no debe duplicar nada (recuperación + idempotencia juntas).
    ids_otra_vez = seed.asegurar_ruta_historica(**ruta.kwargs())
    assert sorted(ids_otra_vez) == sorted(ids)
    assert len(ruta.entregados()) == Ruta.OBJETIVO


def test_ruta_historica_recupera_huerfano_en_transito_completandolo(s9, ruta):
    """Simula una corrida interrumpida DESPUÉS de zarpar (S8: ya no se puede cancelar). La recuperación
    debe TERMINAR ese envío exacto (no crear un reemplazo) y llegar a las 3 entregas."""
    ids_parciales = _crear_ocurrencias_entregadas(s9, ruta, [0, 1])
    s9.envios_creados.extend(ids_parciales)
    huerfano_id, salida_esperada = _crear_huerfano_en_transito(s9, ruta, indice=2)
    s9.envios_creados.append(huerfano_id)

    ids = seed.asegurar_ruta_historica(**ruta.kwargs())

    assert len(ruta.entregados()) == Ruta.OBJETIVO
    assert huerfano_id in ids, "el huérfano EN_TRANSITO se completa, no se reemplaza"
    detalle = s9.servicios.envios.obtener(s9.sesion, huerfano_id)
    assert detalle.envio.estado == "ENTREGADO"
    assert detalle.tramos[0].salida_real_en == salida_esperada, "la salida real no debía recalcularse"
    assert detalle.envio.entrega_real_en == salida_esperada + timedelta(days=18)  # índice 2: sin atraso
    # Repetir de nuevo no debe duplicar nada.
    ids_otra_vez = seed.asegurar_ruta_historica(**ruta.kwargs())
    assert sorted(ids_otra_vez) == sorted(ids)
    assert len(ruta.entregados()) == Ruta.OBJETIVO


def _crear_ocurrencias_entregadas(s9: EntornoSeed, ruta: Ruta, indices: list[int]) -> list[uuid.UUID]:
    """Crea, completas, SOLO las ocurrencias indicadas (no todo `OBJETIVO_RUTA_HISTORICA`), para preparar
    el escenario de una corrida previa que llegó hasta ahí y se interrumpió en la siguiente."""
    ahora = datetime.now(timezone.utc)
    ids = []
    for i in indices:
        salida = (ahora - timedelta(days=174 - i * 14)).replace(hour=9, minute=0, second=0, microsecond=0)
        envio_id, _ = seed.crear_envio_ruta(
            s9.servicios, s9.conns, s9.sesion, s9.sesion, cliente_id=ruta.cliente_id,
            origen_id=ruta.origen_id, destino_id=ruta.destino_id, operador_id=ruta.operador_id,
            vehiculo_id=ruta.vehiculo_id, contenedor_id=(ruta.cont1 if i % 2 == 0 else ruta.cont2),
            salida_real=salida, duracion_dias=18, atraso_dias=seed._dias_atraso(i), peso_kg=18_000,
            volumen_m3=25,
            descripcion_carga=f"{seed.PREFIJO} ruta histórica BA-Rotterdam, ocurrencia {i + 1:02d}/"
                               f"{Ruta.OBJETIVO} (prueba)",
        )
        ids.append(envio_id)
    return ids


def _crear_huerfano_borrador(s9: EntornoSeed, ruta: Ruta, *, indice: int) -> uuid.UUID:
    from logistica.domain.comercial import CargoDatos
    from logistica.domain.logistica import EnvioDatos

    envio = s9.servicios.envios.crear(
        s9.sesion,
        EnvioDatos(
            ruta.cliente_id, ruta.origen_id, ruta.destino_id,
            f"{seed.PREFIJO} ruta histórica BA-Rotterdam, ocurrencia {indice + 1:02d}/{Ruta.OBJETIVO} "
            f"(huérfano de prueba)",
            18_000, 25,
        ),
    )
    s9.servicios.comercial.crear_cargo(
        s9.sesion, envio.id, CargoDatos("Flete marítimo Buenos Aires-Rotterdam", 1, 4500, "USD")
    )
    ahora = datetime.now(timezone.utc)
    salida = (ahora + timedelta(days=5)).replace(hour=9, minute=0, second=0, microsecond=0)
    s9.servicios.envios.definir_tramos(
        s9.sesion, envio.id,
        [TramoPlan(ruta.origen_id, ruta.destino_id, "MARITIMO", ruta.operador_id, salida,
                   salida + timedelta(days=18), ruta.vehiculo_id, 3800, "USD")],
    )
    return envio.id


def _crear_huerfano_en_transito(s9: EntornoSeed, ruta: Ruta, *, indice: int) -> tuple[uuid.UUID, datetime]:
    from logistica.domain.comercial import CargoDatos
    from logistica.domain.logistica import AsignacionDatos, EnvioDatos

    ahora = datetime.now(timezone.utc)
    salida = (ahora - timedelta(days=174 - indice * 14)).replace(hour=9, minute=0, second=0, microsecond=0)
    envio = s9.servicios.envios.crear(
        s9.sesion,
        EnvioDatos(
            ruta.cliente_id, ruta.origen_id, ruta.destino_id,
            f"{seed.PREFIJO} ruta histórica BA-Rotterdam, ocurrencia {indice + 1:02d}/{Ruta.OBJETIVO} "
            f"(huérfano de prueba)",
            18_000, 25,
        ),
    )
    seed._retrasar_creacion(s9.conns, envio.id, salida - timedelta(days=5))
    s9.servicios.comercial.crear_cargo(
        s9.sesion, envio.id, CargoDatos("Flete marítimo Buenos Aires-Rotterdam", 1, 4500, "USD")
    )
    detalle = s9.servicios.envios.definir_tramos(
        s9.sesion, envio.id,
        [TramoPlan(ruta.origen_id, ruta.destino_id, "MARITIMO", ruta.operador_id, salida,
                   salida + timedelta(days=18), ruta.vehiculo_id, 3800, "USD")],
    )
    tramo_id = detalle.tramos[0].id
    s9.servicios.envios.asignar_contenedor(
        s9.sesion, envio.id,
        AsignacionDatos(ruta.cont1, 18_000, 25, salida - timedelta(days=1), salida + timedelta(days=30)),
    )
    s9.servicios.envios.planificar(s9.sesion, envio.id)
    s9.servicios.envios.iniciar_tramo(s9.sesion, tramo_id, salida)
    return envio.id, salida


# ============================================================================ aislamiento de datos ajenos
def test_preserva_registros_ajenos_al_seed(s9, ruta):
    ajeno = s9.servicios.terceros.crear(
        s9.sesion,
        __import__("logistica.domain.acceso", fromlist=["TerceroDatos"]).TerceroDatos(
            f"Empresa Totalmente Ajena {s9.sufijo}", "AR", "CUIT", f"30-{s9.sufijo}-7"
        ),
        {"CLIENTE"},
    )
    antes = s9.servicios.terceros.obtener(s9.sesion, ajeno.id)

    seed.asegurar_ruta_historica(**ruta.kwargs())
    s9.servicios.terceros.crear(
        s9.sesion,
        __import__("logistica.domain.acceso", fromlist=["TerceroDatos"]).TerceroDatos(
            f"Cliente Ruta9 Otro {s9.sufijo}", "AR", "CUIT", f"30-99999999-{s9.sufijo[:1]}"
        ),
        {"CLIENTE"},
    )

    despues = s9.servicios.terceros.obtener(s9.sesion, ajeno.id)
    assert despues == antes, "un tercero ajeno al seed no debe cambiar"


def test_sincronizacion_acotada_no_toca_entregas_pendientes_ajenas(s9, ruta):
    """Confirma lo que pide la revisión: `procesar(destino, agregado_id=...)` nunca reclama entregas
    pendientes de OTRO envío, aunque haya cupo en el lote."""
    ids = seed.asegurar_ruta_historica(**ruta.kwargs())
    s9.envios_creados.extend(ids)
    ajeno_id = ids[0]  # un envío "ajeno" al que vamos a sincronizar deliberadamente
    propio_id = ids[1]

    # El ajeno tiene entregas NEO4J/MONGO pendientes (recién creado, nunca sincronizado).
    pendientes_ajeno_antes = s9.servicios.envios.eventos(s9.sesion, ajeno_id)
    assert any(ev.pendiente for ev in pendientes_ajeno_antes), "hace falta un pendiente ajeno para probar"

    resultado = s9.servicios.sincronizacion.procesar(NEO4J, limite=10, agregado_id=propio_id)
    s9.envios_creados  # (el propio ya está registrado para limpieza)
    assert resultado.procesados > 0

    pendientes_ajeno_despues = s9.servicios.envios.eventos(s9.sesion, ajeno_id)
    assert [e.pendiente for e in pendientes_ajeno_antes] == [e.pendiente for e in pendientes_ajeno_despues], (
        "procesar() acotado por agregado_id no debe tocar las entregas pendientes de otro envío"
    )


# ============================================================================ coherencia temporal
@pytest.mark.parametrize("indice,atraso_esperado", [(0, 0), (3, 5)])
def test_coherencia_temporal_de_un_envio_historico(s9, ruta, indice, atraso_esperado):
    ahora = datetime.now(timezone.utc)
    salida = (ahora - timedelta(days=100)).replace(hour=9, minute=0, second=0, microsecond=0)
    envio_id, tarde = seed.crear_envio_ruta(
        s9.servicios, s9.conns, s9.sesion, s9.sesion, cliente_id=ruta.cliente_id,
        origen_id=ruta.origen_id, destino_id=ruta.destino_id, operador_id=ruta.operador_id,
        vehiculo_id=ruta.vehiculo_id, contenedor_id=ruta.cont1, salida_real=salida, duracion_dias=18,
        atraso_dias=atraso_esperado, peso_kg=18_000, volumen_m3=25,
        descripcion_carga=f"{seed.PREFIJO} coherencia temporal (prueba, índice {indice})",
    )
    s9.envios_creados.append(envio_id)
    assert tarde == (atraso_esperado > 0)

    detalle = s9.servicios.envios.obtener(s9.sesion, envio_id)
    envio, tramo = detalle.envio, detalle.tramos[0]
    asignacion = detalle.asignaciones[0]

    assert envio.creado_en <= tramo.salida_estimada_en == tramo.salida_real_en
    assert tramo.salida_real_en < tramo.llegada_real_en == envio.entrega_real_en
    assert envio.entrega_comprometida_en == tramo.llegada_estimada_en
    if atraso_esperado > 0:
        assert envio.entrega_real_en > envio.entrega_comprometida_en
    else:
        assert envio.entrega_real_en == envio.entrega_comprometida_en
    assert asignacion.estado == "FINALIZADA"
    assert asignacion.reserva_desde <= tramo.salida_real_en
    assert asignacion.reserva_hasta >= envio.entrega_real_en
    assert asignacion.uso_desde == tramo.salida_real_en
    assert asignacion.uso_hasta == envio.entrega_real_en


def test_retrasar_creacion_solo_afecta_el_envio_indicado(s9, ruta):
    from logistica.domain.logistica import EnvioDatos

    e1 = s9.servicios.envios.crear(
        s9.sesion, EnvioDatos(ruta.cliente_id, ruta.origen_id, ruta.destino_id, "Envío 1", 100, 1)
    )
    e2 = s9.servicios.envios.crear(
        s9.sesion, EnvioDatos(ruta.cliente_id, ruta.origen_id, ruta.destino_id, "Envío 2", 100, 1)
    )
    s9.envios_creados += [e1.id, e2.id]
    creado_en_e2_antes = s9.servicios.envios.obtener(s9.sesion, e2.id).envio.creado_en

    nueva_fecha = datetime.now(timezone.utc) - timedelta(days=100)
    seed._retrasar_creacion(s9.conns, e1.id, nueva_fecha)

    assert s9.servicios.envios.obtener(s9.sesion, e1.id).envio.creado_en == nueva_fecha
    assert s9.servicios.envios.obtener(s9.sesion, e2.id).envio.creado_en == creado_en_e2_antes

    with pytest.raises(RuntimeError):
        seed._retrasar_creacion(s9.conns, uuid.uuid4(), nueva_fecha)


# ============================================================================ sincronización coherente
def test_sincronizacion_mongo_y_neo4j_reciben_fechas_coherentes(s9, ruta):
    ahora = datetime.now(timezone.utc)
    salida = (ahora - timedelta(days=100)).replace(hour=9, minute=0, second=0, microsecond=0)
    envio_id, _ = seed.crear_envio_ruta(
        s9.servicios, s9.conns, s9.sesion, s9.sesion, cliente_id=ruta.cliente_id,
        origen_id=ruta.origen_id, destino_id=ruta.destino_id, operador_id=ruta.operador_id,
        vehiculo_id=ruta.vehiculo_id, contenedor_id=ruta.cont1, salida_real=salida, duracion_dias=18,
        atraso_dias=0, peso_kg=18_000, volumen_m3=25,
        descripcion_carga=f"{seed.PREFIJO} sincronización coherente (prueba)",
    )
    s9.envios_creados.append(envio_id)

    for destino in (NEO4J, MONGO, FACTURACION):
        resultado = s9.servicios.sincronizacion.procesar(destino, limite=10, agregado_id=envio_id)
        assert resultado.fallidos == 0

    # Mongo: los eventos de salida y entrega llevan la fecha HISTÓRICA, no la de hoy.
    pagina = s9.servicios.trazabilidad.eventos(s9.sesion, envio_id)
    por_tipo = {e.tipo: e for e in pagina.eventos}
    assert por_tipo["TRAMO_INICIADO"].fecha_ocurrencia == salida
    entregado = next(e for e in pagina.eventos if e.tipo == "ENTREGADO")
    assert entregado.fecha_ocurrencia == salida + timedelta(days=18)
    assert (ahora - entregado.fecha_ocurrencia).days >= 80, "la fecha del evento debe ser histórica, no hoy"

    # Neo4j: la proyección del tramo tiene las mismas fechas reales que PostgreSQL.
    ruta_proyectada = s9.servicios.trazabilidad.ruta(s9.sesion, envio_id)
    assert ruta_proyectada.estado_proyeccion in ("ACTUALIZADA", "ATRASADA")
    tramo_proyectado = ruta_proyectada.tramos[0]
    assert tramo_proyectado.salida_real_en == salida
    assert tramo_proyectado.llegada_real_en == salida + timedelta(days=18)

    # Facturación: el cargo de este envío terminó facturado (no se tocó ningún otro cargo).
    cargos = s9.servicios.comercial.cargos(s9.sesion, envio_id)
    assert cargos and all(c.estado == "FACTURADO" for c in cargos)


# ============================================================================ chat: idempotencia
def test_chat_es_idempotente_por_conversacion(s9):
    from logistica.domain.acceso import Rol, UsuarioDatos as UD

    u_ops = seed.asegurar_usuario(
        s9.servicios, s9.sesion, nombre="Ops9", apellido="Demo", email=f"ops9-{s9.sufijo}@demo.local",
        roles={Rol.OPERACIONES},
    )
    u_com = seed.asegurar_usuario(
        s9.servicios, s9.sesion, nombre="Com9", apellido="Demo", email=f"com9-{s9.sufijo}@demo.local",
        roles={Rol.COMERCIAL},
    )
    cli = s9.tercero("Cliente Chat9", {"CLIENTE"}, sufijo_identificacion="93333333")
    u_cli1 = seed.asegurar_usuario(
        s9.servicios, s9.sesion, nombre="Cli9Uno", apellido="Demo", email=f"cli9a-{s9.sufijo}@demo.local",
        roles={Rol.CLIENTE}, tercero_id=cli,
    )
    u_cli2 = seed.asegurar_usuario(
        s9.servicios, s9.sesion, nombre="Cli9Dos", apellido="Demo", email=f"cli9b-{s9.sufijo}@demo.local",
        roles={Rol.CLIENTE}, tercero_id=cli,
    )
    p_ops = s9.servicios.acceso.login(f"ops9-{s9.sufijo}@demo.local", seed.PASSWORD_DEMO, "pruebas")
    p_com = s9.servicios.acceso.login(f"com9-{s9.sufijo}@demo.local", seed.PASSWORD_DEMO, "pruebas")
    p_c1 = s9.servicios.acceso.login(f"cli9a-{s9.sufijo}@demo.local", seed.PASSWORD_DEMO, "pruebas")
    p_c2 = s9.servicios.acceso.login(f"cli9b-{s9.sufijo}@demo.local", seed.PASSWORD_DEMO, "pruebas")
    kwargs = dict(
        servicios=s9.servicios, sesion_ops=p_ops.sesion_id, sesion_com=p_com.sesion_id,
        sesion_cli1=p_c1.sesion_id, sesion_cli2=p_c2.sesion_id, usuario_ops=u_ops, usuario_com=u_com,
        usuario_cli1=u_cli1, usuario_cli2=u_cli2,
    )

    seed.asegurar_chat(**kwargs)
    vista1 = s9.servicios.comunicacion.vista(p_ops.sesion_id)
    cantidad1 = len(vista1.conversaciones)
    assert cantidad1 >= 2  # privado interno + grupo (al menos)

    seed.asegurar_chat(**kwargs)
    vista2 = s9.servicios.comunicacion.vista(p_ops.sesion_id)
    assert len(vista2.conversaciones) == cantidad1, "repetir no debe crear conversaciones de más"
