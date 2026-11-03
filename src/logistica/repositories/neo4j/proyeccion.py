"""Proyección de envíos en Neo4j (reconstruible desde PostgreSQL, AGENTS.md §8.3).

Modelo:
  (:Tercero:Cliente)-[:SOLICITA]->(:Envio)-[:UTILIZA {asignacion_id, desde, hasta, estado}]->(:Contenedor)
  (:Envio)-[:TIENE_TRAMO]->(:Tramo)-[:SIGUIENTE]->(:Tramo)
  (:Tercero:Operador)-[:OPERA]->(:Tramo)
  (:Tramo)-[:SALE_DE]->(:Ubicacion), (:Tramo)-[:LLEGA_A]->(:Ubicacion)

`aplicar` reemplaza el subgrafo de UN envío por el estado actual que le pasa el servicio (leído de
PostgreSQL), siempre con MERGE por id: aplicarlo dos veces da el mismo grafo. El nodo Envio guarda la
`version` del agregado; si la que llega no es mayor, no se toca nada (un evento viejo o repetido no hace
retroceder la proyección).
"""

from __future__ import annotations

import uuid
from typing import Any

from logistica.domain.grafo import EnvioGrafo
from logistica.repositories.neo4j import Neo4jRepository

# Primero se TOMA EL LOCK de escritura del nodo (SET/REMOVE de una propiedad auxiliar) y recién después se
# compara la versión. Sin eso, dos trabajadores leían la versión sin lock, los dos pasaban la guarda y podía
# quedar la versión más vieja (se reprodujo en tests/test_fase3_sincronizacion.py). Con el lock, el segundo
# espera al primero y ve la versión que dejó.
# `forzar` (reconstrucción) permite volver a aplicar la MISMA versión, nunca una más vieja.
_ENVIO = """
MERGE (e:Envio {id: $envio.id})
SET e._bloqueo = true
REMOVE e._bloqueo
WITH e WHERE CASE WHEN $forzar THEN coalesce(e.version, -1) <= $envio.version
                  ELSE coalesce(e.version, 0) < $envio.version END
SET e += $envio
WITH e
MERGE (c:Tercero {id: $cliente.id})
SET c:Cliente, c.razon_social = $cliente.razon_social
MERGE (c)-[:SOLICITA]->(e)
RETURN e.id AS id
"""

_UBICACIONES = """
UNWIND $ubicaciones AS u
MERGE (x:Ubicacion {id: u.id})
SET x.nombre = u.nombre, x.pais = u.pais, x.ciudad = u.ciudad
"""

_TRAMOS = """
MATCH (e:Envio {id: $envio_id})
// Tramos que ya no existen en PostgreSQL (se redefinió el recorrido): se borran con sus relaciones.
OPTIONAL MATCH (viejo:Tramo {envio_id: $envio_id}) WHERE NOT viejo.id IN $tramo_ids
DETACH DELETE viejo
WITH DISTINCT e
// Las relaciones de los tramos se recrean desde cero (son pocas y así no quedan restos).
OPTIONAL MATCH (:Tramo {envio_id: $envio_id})-[r:SALE_DE|LLEGA_A|SIGUIENTE]->()
DELETE r
WITH DISTINCT e
OPTIONAL MATCH ()-[r:OPERA]->(:Tramo {envio_id: $envio_id})
DELETE r
WITH DISTINCT e
UNWIND $tramos AS t
MERGE (n:Tramo {id: t.id})
SET n += t.propiedades, n.envio_id = $envio_id
MERGE (e)-[:TIENE_TRAMO]->(n)
WITH n, t
MATCH (o:Ubicacion {id: t.origen_id}), (d:Ubicacion {id: t.destino_id})
MERGE (n)-[:SALE_DE]->(o)
MERGE (n)-[:LLEGA_A]->(d)
MERGE (op:Tercero {id: t.operador_id})
SET op:Operador, op.razon_social = t.operador_razon_social
MERGE (op)-[:OPERA]->(n)
"""

_SIGUIENTES = """
UNWIND $pares AS par
MATCH (a:Tramo {id: par[0]}), (b:Tramo {id: par[1]})
MERGE (a)-[:SIGUIENTE]->(b)
"""

_USOS = """
MATCH (e:Envio {id: $envio_id})
OPTIONAL MATCH (e)-[u:UTILIZA]->() WHERE NOT u.asignacion_id IN $asignacion_ids
DELETE u
WITH DISTINCT e
UNWIND $usos AS a
MERGE (k:Contenedor {id: a.contenedor_id})
SET k.codigo_internacional = a.codigo
MERGE (e)-[u:UTILIZA {asignacion_id: a.id}]->(k)
SET u.desde = a.desde, u.hasta = a.hasta, u.estado = a.estado
"""


class ProyeccionRepository(Neo4jRepository):
    def aplicar(self, g: EnvioGrafo, *, forzar: bool = False) -> bool:
        """Reemplaza el subgrafo del envío por `g`. Devuelve False si la proyección ya estaba al día."""
        with self._session() as s:
            return s.execute_write(_aplicar, g, forzar)

    def envios_proyectados(self) -> list[str]:
        with self._session() as s:
            return [r["id"] for r in s.run("MATCH (e:Envio) RETURN e.id AS id")]

    def borrar_envios(self, ids: list[str]) -> int:
        """Quita del grafo esos envíos y sus tramos (los que ya no existen en PostgreSQL)."""
        if not ids:
            return 0
        with self._session() as s:
            return s.execute_write(
                lambda tx: tx.run(
                    "MATCH (e:Envio) WHERE e.id IN $ids "
                    "OPTIONAL MATCH (e)-[:TIENE_TRAMO]->(t:Tramo) "
                    "DETACH DELETE t, e RETURN count(DISTINCT e) AS n",
                    ids=ids,
                ).single()["n"]
            )

    def subgrafo(self, envio_id: uuid.UUID) -> dict[str, Any]:
        """Vista compacta del subgrafo de un envío (para verificar la proyección y para diagnóstico)."""
        eid = str(envio_id)
        with self._session() as s:
            envio = s.run(
                "MATCH (e:Envio {id: $id}) RETURN e.estado AS estado, e.version AS version", id=eid
            ).single()
            nodos = s.run("MATCH (e:Envio {id: $id}) RETURN count(e) AS n", id=eid).single()["n"]
            cliente = s.run(
                "MATCH (c:Cliente)-[:SOLICITA]->(:Envio {id: $id}) RETURN c.id AS id", id=eid
            ).single()
            tramos = s.run(
                "MATCH (:Envio {id: $id})-[:TIENE_TRAMO]->(t:Tramo)-[:SALE_DE]->(o), (t)-[:LLEGA_A]->(d),"
                " (op:Operador)-[:OPERA]->(t) RETURN t.orden AS orden, o.id AS o, d.id AS d, op.id AS op,"
                " t.estado AS estado, t.vehiculo AS vehiculo ORDER BY orden",
                id=eid,
            ).data()
            siguientes = s.run(
                "MATCH (a:Tramo {envio_id: $id})-[:SIGUIENTE]->(b:Tramo) RETURN a.orden AS a, b.orden AS b "
                "ORDER BY a",
                id=eid,
            ).data()
            usos = s.run(
                "MATCH (:Envio {id: $id})-[u:UTILIZA]->(k:Contenedor) RETURN u.asignacion_id AS a, k.id AS "
                "contenedor"
                " ORDER BY u.desde, k.codigo_internacional",
                id=eid,
            ).data()
            huerfanos = s.run(
                "MATCH (t:Tramo {envio_id: $id}) WHERE NOT (:Envio {id: $id})-[:TIENE_TRAMO]->(t) RETURN "
                "count(t) AS n",
                id=eid,
            ).single()["n"]
        return {
            "estado": envio["estado"] if envio else None,
            "version": envio["version"] if envio else None,
            "nodos_envio": nodos,
            "cliente": cliente["id"] if cliente else None,
            "tramos": [(t["orden"], t["o"], t["d"], t["op"], t["estado"]) for t in tramos],
            "vehiculos": [t["vehiculo"] for t in tramos],
            "siguientes": [(x["a"], x["b"]) for x in siguientes],
            "utiliza": [(u["a"], u["contenedor"]) for u in usos],
            "tramos_huerfanos": huerfanos,
        }


def _aplicar(tx: Any, g: EnvioGrafo, forzar: bool) -> bool:
    """Todo en UNA transacción de Neo4j: o se aplica el subgrafo completo, o nada."""
    if tx.run(_ENVIO, envio=g.envio, cliente=g.cliente, forzar=forzar).single() is None:
        return False  # versión vieja o repetida: la proyección ya incluye este estado
    tx.run(_UBICACIONES, ubicaciones=g.ubicaciones).consume()
    tx.run(_TRAMOS, envio_id=g.envio["id"], tramo_ids=[t["id"] for t in g.tramos], tramos=g.tramos).consume()
    tx.run(_SIGUIENTES, pares=g.siguientes).consume()
    tx.run(_USOS, envio_id=g.envio["id"], asignacion_ids=[u["id"] for u in g.usos], usos=g.usos).consume()
    return True
