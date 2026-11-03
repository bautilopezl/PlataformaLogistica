-- 950_particionar_hash.sql  ·  OPCIONAL: no lo ejecuta setup_databases.py por defecto.
--
-- Deja listo y probado el particionamiento de PostgreSQL para cuando el volumen lo justifique:
--   scripts/setup_databases.py --only postgres --particionar
--
-- Convierte dos tablas a particionadas por HASH, en 4 particiones:
--   * lectura_procesada  por lectura_id   (clave primaria: lectura_id, version_reglas)
--   * outbox_entrega     por evento_id    (clave primaria: evento_id, destino)
--
-- Por qué estas dos y por qué hash:
--   * Ninguna otra tabla las referencia, así que no hay claves foráneas entrantes que rehacer.
--   * La columna de partición YA forma parte de su clave primaria. PostgreSQL exige que la clave primaria
--     y toda restricción UNIQUE de una tabla particionada incluyan la columna de partición; por eso la
--     garantía de no procesar dos veces la misma lectura (o entrega) se conserva sin cambios.
--   * Descartadas: alerta, outbox_evento y movimiento_cuenta (otras tablas las referencian).
--
-- Es idempotente: si la tabla ya está particionada no hace nada. Conserva los datos existentes.
-- Todo ocurre en una transacción: si algo falla, no queda nada a medias.
-- Cambiar la cantidad de particiones = cambiar n_particiones (potencia de 2 recomendada).

BEGIN;

DO $$
DECLARE
    n_particiones CONSTANT int := 4;
    tipo "char";
    i int;
BEGIN
    ------------------------------------------------------------------ lectura_procesada
    SELECT c.relkind INTO tipo FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = 'lectura_procesada';

    IF tipo = 'r' THEN
        LOCK TABLE lectura_procesada IN ACCESS EXCLUSIVE MODE;
        ALTER TABLE lectura_procesada RENAME TO lectura_procesada_vieja;
        ALTER TABLE lectura_procesada_vieja RENAME CONSTRAINT lectura_procesada_pkey TO lectura_procesada_vieja_pkey;

        CREATE TABLE lectura_procesada (LIKE lectura_procesada_vieja INCLUDING DEFAULTS)
            PARTITION BY HASH (lectura_id);
        ALTER TABLE lectura_procesada ADD PRIMARY KEY (lectura_id, version_reglas);
        FOR i IN 0..n_particiones - 1 LOOP
            EXECUTE format('CREATE TABLE lectura_procesada_p%s PARTITION OF lectura_procesada
                            FOR VALUES WITH (MODULUS %s, REMAINDER %s)', i, n_particiones, i);
        END LOOP;

        INSERT INTO lectura_procesada SELECT * FROM lectura_procesada_vieja;
        DROP TABLE lectura_procesada_vieja;
        RAISE NOTICE 'lectura_procesada particionada por hash en % particiones', n_particiones;
    ELSIF tipo = 'p' THEN
        RAISE NOTICE 'lectura_procesada ya estaba particionada';
    END IF;

    ------------------------------------------------------------------ outbox_entrega
    SELECT c.relkind INTO tipo FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = 'outbox_entrega';

    IF tipo = 'r' THEN
        LOCK TABLE outbox_entrega IN ACCESS EXCLUSIVE MODE;
        ALTER TABLE outbox_entrega RENAME TO outbox_entrega_vieja;
        ALTER TABLE outbox_entrega_vieja RENAME CONSTRAINT outbox_entrega_pkey TO outbox_entrega_vieja_pkey;
        ALTER INDEX ix_entrega_estado_proximo RENAME TO ix_entrega_estado_proximo_vieja;

        CREATE TABLE outbox_entrega (LIKE outbox_entrega_vieja INCLUDING DEFAULTS INCLUDING CONSTRAINTS)
            PARTITION BY HASH (evento_id);
        ALTER TABLE outbox_entrega ADD PRIMARY KEY (evento_id, destino);
        ALTER TABLE outbox_entrega ADD FOREIGN KEY (evento_id) REFERENCES outbox_evento (id);
        FOR i IN 0..n_particiones - 1 LOOP
            EXECUTE format('CREATE TABLE outbox_entrega_p%s PARTITION OF outbox_entrega
                            FOR VALUES WITH (MODULUS %s, REMAINDER %s)', i, n_particiones, i);
        END LOOP;
        CREATE INDEX ix_entrega_estado_proximo ON outbox_entrega (estado, proximo_intento_en);

        INSERT INTO outbox_entrega SELECT * FROM outbox_entrega_vieja;
        DROP TABLE outbox_entrega_vieja;
        RAISE NOTICE 'outbox_entrega particionada por hash en % particiones', n_particiones;
    ELSIF tipo = 'p' THEN
        RAISE NOTICE 'outbox_entrega ya estaba particionada';
    END IF;

    ------------------------------------------------------------------ privilegios (las tablas nuevas no heredan los de 900_grants.sql)
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logistica_app_rw') THEN
        -- Solo las tablas nuevas: un GRANT sobre todas desharía los REVOKE de 900_grants.sql.
        GRANT SELECT, INSERT, UPDATE, DELETE ON lectura_procesada, outbox_entrega TO logistica_app_rw;
        FOR i IN 0..n_particiones - 1 LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON lectura_procesada_p%s, outbox_entrega_p%s TO logistica_app_rw', i, i);
        END LOOP;
        -- registro que nunca se borra desde la aplicación (igual que en 900_grants.sql)
        REVOKE DELETE ON lectura_procesada FROM logistica_app_rw;
        FOR i IN 0..n_particiones - 1 LOOP
            EXECUTE format('REVOKE DELETE ON lectura_procesada_p%s FROM logistica_app_rw', i);
        END LOOP;
    END IF;
END
$$;

COMMIT;
