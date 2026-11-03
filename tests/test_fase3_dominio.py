"""Fase 3 — partes puras de la sincronización (sin servidores)."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from logistica.domain.grafo import armar_envio_grafo  # noqa: E402
from logistica.domain.logistica_dtos import AsignacionDTO, EnvioDTO, TramoDTO, UbicacionDTO  # noqa: E402
from logistica.repositories.mongo.eventos import documento_evento  # noqa: E402

T0 = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)
A, B, C = (uuid.uuid4() for _ in range(3))


def _envio():
    return EnvioDTO(
        uuid.uuid4(),
        "ENV-1",
        uuid.uuid4(),
        "Cliente SA",
        A,
        "Rosario",
        C,
        "Santos",
        "Café",
        "PLANIFICADO",
        "NORMAL",
        Decimal("8000.500"),
        Decimal("20"),
        T0,
        T0,
        None,
        None,
        4,
    )


def _tramo(orden, o, d, estado="PENDIENTE"):
    return TramoDTO(
        uuid.uuid4(),
        uuid.uuid4(),
        orden,
        o,
        "o",
        d,
        "d",
        "TERRESTRE",
        uuid.uuid4(),
        "Op SA",
        None,
        None,
        estado,
        T0,
        T0 + timedelta(hours=1),
        None,
        None,
        Decimal("10"),
        "USD",
    )


def _asignacion(estado):
    return AsignacionDTO(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        "MSKU1",
        estado,
        T0,
        T0 + timedelta(days=1),
        None,
        None,
        Decimal("1"),
        Decimal("1"),
    )


def test_proyeccion_ordena_tramos_encadena_siguientes_y_omite_canceladas():
    t2, t1 = _tramo(2, B, C), _tramo(1, A, B)  # desordenados a propósito
    ubic = {
        i: UbicacionDTO(i, f"U{n}", "PUERTO", "AR", "X", None, None, None, True)
        for n, i in enumerate((A, B, C))
    }
    g = armar_envio_grafo(_envio(), [t2, t1], [_asignacion("RESERVADA"), _asignacion("CANCELADA")], ubic)
    assert [t["propiedades"]["orden"] for t in g.tramos] == [1, 2]
    assert g.siguientes == [[str(t1.id), str(t2.id)]]
    assert len(g.usos) == 1  # la cancelada no es un uso del contenedor
    assert g.envio["version"] == 4 and g.envio["peso_kg"] == 8000.5
    # Neo4j no tiene UUID ni Decimal: todo viaja como texto o número simple.
    assert all(isinstance(v, (str, int, float, type(None))) for v in g.envio.values())


def test_costos_no_se_proyectan_al_grafo():
    g = armar_envio_grafo(_envio(), [_tramo(1, A, C)], [], {})
    assert "costo" not in g.tramos[0]["propiedades"]  # el grafo no decide ni expone importes


def test_documento_de_mongo_desde_un_payload_de_outbox():
    eid, envio = uuid.uuid4(), uuid.uuid4()
    payload = {
        "event_id": str(eid),
        "tipo": "RETRASO",
        "envio_id": str(envio),
        "usuario_id": str(uuid.uuid4()),
        "fecha_ocurrencia": "2026-12-01T05:00:00-03:00",
        "contenedor_ids": [str(uuid.uuid4())],
        "detalles": {"motivo": "x"},
    }
    doc = documento_evento(payload, fecha_registro=T0)
    assert doc["_id"] == eid and doc["envio_id"] == envio  # UUID reales (binData en Mongo)
    assert doc["fecha_ocurrencia"] == T0  # convertido a UTC
    assert isinstance(doc["contenedor_ids"][0], uuid.UUID)
    assert "event_id" not in doc and "tramo_id" not in doc
