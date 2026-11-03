-- 020_logistica.sql  ·  Ubicaciones, envíos, tramos, vehículos, contenedores y asignaciones

CREATE TABLE IF NOT EXISTS ubicacion (
    id          UUID PRIMARY KEY,
    nombre      VARCHAR(150)  NOT NULL,
    tipo        VARCHAR(20)   NOT NULL,
    pais_codigo VARCHAR(2)    NOT NULL,
    ciudad      VARCHAR(150)  NOT NULL,
    direccion   TEXT,
    latitud     NUMERIC(9,6),
    longitud    NUMERIC(9,6),
    activo      BOOLEAN       NOT NULL DEFAULT TRUE,
    CONSTRAINT ck_ubicacion_tipo CHECK (tipo IN ('DEPOSITO', 'PUERTO', 'AEROPUERTO', 'OTRO')),
    CONSTRAINT ck_ubicacion_pais CHECK (pais_codigo ~ '^[A-Z]{2}$'),
    CONSTRAINT ck_ubicacion_coord_juntas CHECK ((latitud IS NULL) = (longitud IS NULL)),
    CONSTRAINT ck_ubicacion_latitud CHECK (latitud IS NULL OR latitud BETWEEN -90 AND 90),
    CONSTRAINT ck_ubicacion_longitud CHECK (longitud IS NULL OR longitud BETWEEN -180 AND 180)
);
CREATE INDEX IF NOT EXISTS ix_ubicacion_pais_ciudad ON ubicacion (pais_codigo, ciudad);

CREATE TABLE IF NOT EXISTS vehiculo (
    id             UUID PRIMARY KEY,
    operador_id    UUID        NOT NULL REFERENCES tercero(id),
    tipo           VARCHAR(15) NOT NULL,
    identificacion VARCHAR(80) NOT NULL,
    estado         VARCHAR(20) NOT NULL DEFAULT 'HABILITADO',
    CONSTRAINT uq_vehiculo UNIQUE (operador_id, tipo, identificacion),
    CONSTRAINT ck_vehiculo_tipo CHECK (tipo IN ('CAMION', 'BARCO', 'AERONAVE')),
    CONSTRAINT ck_vehiculo_estado CHECK (estado IN ('HABILITADO', 'MANTENIMIENTO', 'BAJA')),
    CONSTRAINT ck_vehiculo_identificacion CHECK (btrim(identificacion) <> '')
);

CREATE TABLE IF NOT EXISTS contenedor (
    id                   UUID PRIMARY KEY,
    codigo_internacional VARCHAR(20)   NOT NULL UNIQUE,
    tipo                 VARCHAR(20)   NOT NULL,
    capacidad_kg         NUMERIC(14,3) NOT NULL,
    capacidad_m3         NUMERIC(14,3) NOT NULL,
    estado               VARCHAR(20)   NOT NULL DEFAULT 'HABILITADO',
    CONSTRAINT ck_contenedor_tipo CHECK (tipo IN ('SECO', 'REFRIGERADO', 'CISTERNA', 'OTRO')),
    CONSTRAINT ck_contenedor_capacidades CHECK (capacidad_kg > 0 AND capacidad_m3 > 0),
    CONSTRAINT ck_contenedor_estado CHECK (estado IN ('HABILITADO', 'MANTENIMIENTO', 'BAJA')),
    CONSTRAINT ck_contenedor_codigo CHECK (btrim(codigo_internacional) <> '')
);

CREATE TABLE IF NOT EXISTS envio (
    id                       UUID PRIMARY KEY,
    codigo                   VARCHAR(40)   NOT NULL UNIQUE,
    cliente_id               UUID          NOT NULL REFERENCES tercero(id),
    origen_id                UUID          NOT NULL REFERENCES ubicacion(id),
    destino_id               UUID          NOT NULL REFERENCES ubicacion(id),
    descripcion_carga        TEXT          NOT NULL,
    estado                   VARCHAR(20)   NOT NULL DEFAULT 'BORRADOR',
    prioridad                VARCHAR(10)   NOT NULL DEFAULT 'NORMAL',
    peso_kg                  NUMERIC(14,3) NOT NULL,
    volumen_m3               NUMERIC(14,3) NOT NULL,
    creado_en                TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    entrega_estimada_en      TIMESTAMPTZ,
    entrega_comprometida_en  TIMESTAMPTZ,
    entrega_real_en          TIMESTAMPTZ,
    creado_por_id            UUID          NOT NULL REFERENCES usuario(id),
    actualizado_en           TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    version                  BIGINT        NOT NULL DEFAULT 1,
    CONSTRAINT ck_envio_estado CHECK (estado IN ('BORRADOR', 'PLANIFICADO', 'EN_TRANSITO', 'ENTREGADO', 'CANCELADO')),
    CONSTRAINT ck_envio_prioridad CHECK (prioridad IN ('BAJA', 'NORMAL', 'ALTA')),
    CONSTRAINT ck_envio_magnitudes CHECK (peso_kg > 0 AND volumen_m3 > 0),
    CONSTRAINT ck_envio_version CHECK (version > 0),
    CONSTRAINT ck_envio_codigo CHECK (btrim(codigo) <> ''),
    CONSTRAINT ck_envio_origen_destino CHECK (origen_id <> destino_id),
    -- ENTREGADO exige entrega real; los demás estados la dejan nula
    CONSTRAINT ck_envio_entrega_real CHECK (
        (estado = 'ENTREGADO' AND entrega_real_en IS NOT NULL)
        OR (estado <> 'ENTREGADO' AND entrega_real_en IS NULL)
    ),
    -- PLANIFICADO y posteriores exigen fecha estimada
    CONSTRAINT ck_envio_estimada CHECK (
        estado NOT IN ('PLANIFICADO', 'EN_TRANSITO', 'ENTREGADO') OR entrega_estimada_en IS NOT NULL
    ),
    -- Compromiso obligatorio desde EN_TRANSITO (su inmutabilidad la garantiza un trigger)
    CONSTRAINT ck_envio_compromiso CHECK (
        estado NOT IN ('EN_TRANSITO', 'ENTREGADO') OR entrega_comprometida_en IS NOT NULL
    ),
    CONSTRAINT ck_envio_fechas_posteriores CHECK (
        (entrega_estimada_en IS NULL OR entrega_estimada_en >= creado_en)
        AND (entrega_comprometida_en IS NULL OR entrega_comprometida_en >= creado_en)
        AND (entrega_real_en IS NULL OR entrega_real_en >= creado_en)
    )
);
CREATE INDEX IF NOT EXISTS ix_envio_cliente_creado ON envio (cliente_id, creado_en);
CREATE INDEX IF NOT EXISTS ix_envio_estado_estimada ON envio (estado, entrega_estimada_en);

-- El compromiso de entrega se fija una vez y no cambia más.
CREATE OR REPLACE FUNCTION fn_envio_compromiso_inmutable() RETURNS trigger AS $$
BEGIN
    IF OLD.entrega_comprometida_en IS NOT NULL
       AND NEW.entrega_comprometida_en IS DISTINCT FROM OLD.entrega_comprometida_en THEN
        RAISE EXCEPTION 'entrega_comprometida_en es inmutable una vez fijada (envio %)', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_envio_compromiso_inmutable ON envio;
CREATE TRIGGER trg_envio_compromiso_inmutable
    BEFORE UPDATE ON envio
    FOR EACH ROW EXECUTE FUNCTION fn_envio_compromiso_inmutable();

CREATE TABLE IF NOT EXISTS tramo (
    id                  UUID PRIMARY KEY,
    envio_id            UUID        NOT NULL REFERENCES envio(id),
    orden               INTEGER     NOT NULL,
    origen_id           UUID        NOT NULL REFERENCES ubicacion(id),
    destino_id          UUID        NOT NULL REFERENCES ubicacion(id),
    medio               VARCHAR(15) NOT NULL,
    operador_id         UUID        NOT NULL REFERENCES tercero(id),
    vehiculo_id         UUID        REFERENCES vehiculo(id),
    estado              VARCHAR(15) NOT NULL DEFAULT 'PENDIENTE',
    salida_estimada_en  TIMESTAMPTZ NOT NULL,
    llegada_estimada_en TIMESTAMPTZ NOT NULL,
    salida_real_en      TIMESTAMPTZ,
    llegada_real_en     TIMESTAMPTZ,
    costo               NUMERIC(18,2),
    moneda_codigo       VARCHAR(3),
    version             BIGINT      NOT NULL DEFAULT 1,
    CONSTRAINT uq_tramo_envio_orden UNIQUE (envio_id, orden),
    CONSTRAINT ck_tramo_orden CHECK (orden > 0),
    CONSTRAINT ck_tramo_origen_destino CHECK (origen_id <> destino_id),
    CONSTRAINT ck_tramo_medio CHECK (medio IN ('TERRESTRE', 'MARITIMO', 'AEREO')),
    CONSTRAINT ck_tramo_estado CHECK (estado IN ('PENDIENTE', 'EN_CURSO', 'FINALIZADO', 'CANCELADO')),
    CONSTRAINT ck_tramo_fechas_estimadas CHECK (llegada_estimada_en >= salida_estimada_en),
    CONSTRAINT ck_tramo_fechas_reales CHECK (
        llegada_real_en IS NULL OR salida_real_en IS NULL OR llegada_real_en >= salida_real_en
    ),
    CONSTRAINT ck_tramo_costo CHECK (
        (costo IS NULL) = (moneda_codigo IS NULL) AND (costo IS NULL OR costo >= 0)
    ),
    CONSTRAINT ck_tramo_moneda CHECK (moneda_codigo IS NULL OR moneda_codigo ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_tramo_version CHECK (version > 0),
    -- Coherencia estado <-> ejecución real
    CONSTRAINT ck_tramo_ejecucion CHECK (
        (estado IN ('PENDIENTE', 'CANCELADO') AND salida_real_en IS NULL AND llegada_real_en IS NULL)
        OR (estado = 'EN_CURSO' AND vehiculo_id IS NOT NULL AND salida_real_en IS NOT NULL AND llegada_real_en IS NULL)
        OR (estado = 'FINALIZADO' AND vehiculo_id IS NOT NULL AND salida_real_en IS NOT NULL AND llegada_real_en IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_tramo_operador_estado ON tramo (operador_id, estado);
CREATE INDEX IF NOT EXISTS ix_tramo_vehiculo_salida ON tramo (vehiculo_id, salida_estimada_en);
CREATE INDEX IF NOT EXISTS ix_tramo_origen ON tramo (origen_id);
CREATE INDEX IF NOT EXISTS ix_tramo_destino ON tramo (destino_id);

CREATE TABLE IF NOT EXISTS envio_contenedor (
    id                  UUID PRIMARY KEY,
    envio_id            UUID          NOT NULL REFERENCES envio(id),
    contenedor_id       UUID          NOT NULL REFERENCES contenedor(id),
    estado              VARCHAR(15)   NOT NULL DEFAULT 'RESERVADA',
    reserva_desde       TIMESTAMPTZ   NOT NULL,
    reserva_hasta       TIMESTAMPTZ   NOT NULL,
    uso_desde           TIMESTAMPTZ,
    uso_hasta           TIMESTAMPTZ,
    peso_asignado_kg    NUMERIC(14,3) NOT NULL,
    volumen_asignado_m3 NUMERIC(14,3) NOT NULL,
    CONSTRAINT ck_envcont_estado CHECK (estado IN ('RESERVADA', 'EN_USO', 'FINALIZADA', 'CANCELADA')),
    CONSTRAINT ck_envcont_reserva CHECK (reserva_hasta > reserva_desde),
    CONSTRAINT ck_envcont_magnitudes CHECK (peso_asignado_kg > 0 AND volumen_asignado_m3 > 0),
    CONSTRAINT ck_envcont_uso_orden CHECK (uso_hasta IS NULL OR uso_desde IS NULL OR uso_hasta > uso_desde),
    CONSTRAINT ck_envcont_uso_estado CHECK (
        (estado IN ('RESERVADA', 'CANCELADA') AND uso_desde IS NULL AND uso_hasta IS NULL)
        OR (estado = 'EN_USO' AND uso_desde IS NOT NULL AND uso_hasta IS NULL)
        OR (estado = 'FINALIZADA' AND uso_desde IS NOT NULL AND uso_hasta IS NOT NULL)
    ),
    -- Reservas vigentes sin superposición por contenedor: [desde, hasta)
    CONSTRAINT ex_envcont_reserva EXCLUDE USING gist (
        contenedor_id WITH =,
        tstzrange(reserva_desde, reserva_hasta, '[)') WITH &&
    ) WHERE (estado IN ('RESERVADA', 'EN_USO')),
    -- Uso real sin superposición; un EN_USO abierto se extiende a infinito
    CONSTRAINT ex_envcont_uso EXCLUDE USING gist (
        contenedor_id WITH =,
        tstzrange(uso_desde, COALESCE(uso_hasta, 'infinity'::timestamptz), '[)') WITH &&
    ) WHERE (uso_desde IS NOT NULL AND estado IN ('EN_USO', 'FINALIZADA'))
);
CREATE INDEX IF NOT EXISTS ix_envcont_envio ON envio_contenedor (envio_id);
CREATE INDEX IF NOT EXISTS ix_envcont_contenedor_uso ON envio_contenedor (contenedor_id, uso_desde);
