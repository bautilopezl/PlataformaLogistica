// constraints.cypher  ·  Restricciones e índices de Neo4j 5 Community (idempotente)
//
// Modelo (proyección reconstruible desde PostgreSQL):
//   (:Tercero:Cliente)-[:SOLICITA]->(:Envio)-[:UTILIZA {asignacion_id, desde, hasta}]->(:Contenedor)
//   (:Envio)-[:TIENE_TRAMO]->(:Tramo)-[:SIGUIENTE]->(:Tramo)
//   (:Tercero:Operador)-[:OPERA]->(:Tramo)
//   (:Tramo)-[:SALE_DE]->(:Ubicacion), (:Tramo)-[:LLEGA_A]->(:Ubicacion)
//
// Decisión: una empresa con varios roles es UN solo nodo (:Tercero) con etiquetas
// adicionales (:Cliente, :Operador); conserva la identidad del tercero de PostgreSQL.
//
// Limitaciones de Community: no hay constraints de existencia de propiedad ni node keys, y
// tampoco unicidad sobre propiedades de relaciones. Por eso la unicidad de UTILIZA
// (asignacion_id) y de (envio_id, orden) del Tramo se garantiza con MERGE por clave estable
// en el repositorio, y con índices de apoyo aquí.

CREATE CONSTRAINT tercero_id_unico IF NOT EXISTS FOR (n:Tercero) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT envio_id_unico IF NOT EXISTS FOR (n:Envio) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT envio_codigo_unico IF NOT EXISTS FOR (n:Envio) REQUIRE n.codigo IS UNIQUE;
CREATE CONSTRAINT tramo_id_unico IF NOT EXISTS FOR (n:Tramo) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT ubicacion_id_unico IF NOT EXISTS FOR (n:Ubicacion) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT contenedor_id_unico IF NOT EXISTS FOR (n:Contenedor) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT contenedor_codigo_unico IF NOT EXISTS FOR (n:Contenedor) REQUIRE n.codigo_internacional IS UNIQUE;

CREATE INDEX tramo_envio_orden IF NOT EXISTS FOR (n:Tramo) ON (n.envio_id, n.orden);
CREATE INDEX envio_estado IF NOT EXISTS FOR (n:Envio) ON (n.estado);
CREATE INDEX ubicacion_pais IF NOT EXISTS FOR (n:Ubicacion) ON (n.pais);
CREATE INDEX tercero_razon_social IF NOT EXISTS FOR (n:Tercero) ON (n.razon_social);
CREATE INDEX utiliza_asignacion IF NOT EXISTS FOR ()-[r:UTILIZA]-() ON (r.asignacion_id);
