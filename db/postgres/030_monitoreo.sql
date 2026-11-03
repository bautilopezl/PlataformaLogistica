-- 030_monitoreo.sql  ·  Sensores, condiciones de monitoreo, alertas e incidentes

CREATE TABLE IF NOT EXISTS sensor (
    id                     UUID PRIMARY KEY,
    codigo                 VARCHAR(80)  NOT NULL UNIQUE,
    tipo                   VARCHAR(30)  NOT NULL,
    fabricante             VARCHAR(100) NOT NULL,
    modelo                 VARCHAR(100),
    estado                 VARCHAR(20)  NOT NULL DEFAULT 'HABILITADO',
    intervalo_esperado_seg INTEGER      NOT NULL DEFAULT 60,
    creado_en              TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_sensor_tipo CHECK (tipo IN ('AMBIENTAL', 'GPS', 'MULTISENSOR', 'OTRO')),
    CONSTRAINT ck_sensor_estado CHECK (estado IN ('HABILITADO', 'MANTENIMIENTO', 'BAJA')),
    CONSTRAINT ck_sensor_intervalo CHECK (intervalo_esperado_seg > 0),
    CONSTRAINT ck_sensor_codigo CHECK (btrim(codigo) <> '')
);

CREATE TABLE IF NOT EXISTS sensor_capacidad (
    sensor_id UUID        NOT NULL REFERENCES sensor(id),
    variable  VARCHAR(20) NOT NULL,
    PRIMARY KEY (sensor_id, variable),
    CONSTRAINT ck_sensor_capacidad_var CHECK (variable IN ('TEMPERATURA', 'HUMEDAD', 'VIBRACION', 'GPS', 'BATERIA'))
);

CREATE TABLE IF NOT EXISTS sensor_contenedor (
    id               UUID PRIMARY KEY,
    sensor_id        UUID        NOT NULL REFERENCES sensor(id),
    contenedor_id    UUID        NOT NULL REFERENCES contenedor(id),
    instalado_en     TIMESTAMPTZ NOT NULL,
    retirado_en      TIMESTAMPTZ,
    instalado_por_id UUID        NOT NULL REFERENCES usuario(id),
    observaciones    TEXT,
    CONSTRAINT ck_sensorcont_retiro CHECK (retirado_en IS NULL OR retirado_en > instalado_en),
    -- Un sensor no puede estar instalado en dos contenedores a la vez
    CONSTRAINT ex_sensorcont_solapamiento EXCLUDE USING gist (
        sensor_id WITH =,
        tstzrange(instalado_en, COALESCE(retirado_en, 'infinity'::timestamptz), '[)') WITH &&
    )
);
CREATE INDEX IF NOT EXISTS ix_sensorcont_contenedor ON sensor_contenedor (contenedor_id);

CREATE TABLE IF NOT EXISTS condicion_monitoreo (
    id                          UUID PRIMARY KEY,
    envio_id                    UUID         NOT NULL UNIQUE REFERENCES envio(id),
    temperatura_min_c           NUMERIC(6,2),
    temperatura_max_c           NUMERIC(6,2),
    humedad_min_pct             NUMERIC(5,2),
    humedad_max_pct             NUMERIC(5,2),
    vibracion_max_g             NUMERIC(10,3),
    bateria_min_pct             NUMERIC(5,2),
    tiempo_max_sin_lecturas_seg INTEGER      NOT NULL DEFAULT 180,
    version_reglas              VARCHAR(30)  NOT NULL,
    reglas_evaluacion           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    creado_en                   TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_cond_temperatura CHECK (
        temperatura_min_c IS NULL OR temperatura_max_c IS NULL OR temperatura_min_c <= temperatura_max_c
    ),
    CONSTRAINT ck_cond_humedad CHECK (
        humedad_min_pct IS NULL OR humedad_max_pct IS NULL OR humedad_min_pct <= humedad_max_pct
    ),
    CONSTRAINT ck_cond_humedad_rango CHECK (
        (humedad_min_pct IS NULL OR humedad_min_pct BETWEEN 0 AND 100)
        AND (humedad_max_pct IS NULL OR humedad_max_pct BETWEEN 0 AND 100)
    ),
    CONSTRAINT ck_cond_bateria CHECK (bateria_min_pct IS NULL OR bateria_min_pct BETWEEN 0 AND 100),
    CONSTRAINT ck_cond_vibracion CHECK (vibracion_max_g IS NULL OR vibracion_max_g >= 0),
    CONSTRAINT ck_cond_tiempo CHECK (tiempo_max_sin_lecturas_seg > 0),
    CONSTRAINT ck_cond_reglas CHECK (jsonb_typeof(reglas_evaluacion) = 'object')
);

CREATE TABLE IF NOT EXISTS sensor_estado_monitoreo (
    sensor_id              UUID PRIMARY KEY REFERENCES sensor(id),
    ultima_recepcion_en    TIMESTAMPTZ NOT NULL,
    ultima_medicion_en     TIMESTAMPTZ NOT NULL,
    ultima_lectura_id      UUID        NOT NULL,       -- referencia lógica a MongoDB
    actualizado_en         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    evaluacion_condiciones JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_sem_evaluacion CHECK (jsonb_typeof(evaluacion_condiciones) = 'object')
);

CREATE TABLE IF NOT EXISTS alerta (
    id                   UUID PRIMARY KEY,
    envio_id             UUID         NOT NULL REFERENCES envio(id),
    contenedor_id        UUID         NOT NULL REFERENCES contenedor(id),
    tramo_id             UUID         REFERENCES tramo(id),
    sensor_id            UUID         REFERENCES sensor(id),
    tipo                 VARCHAR(40)  NOT NULL,
    severidad            VARCHAR(10)  NOT NULL,
    estado               VARCHAR(15)  NOT NULL DEFAULT 'ABIERTA',
    primera_deteccion_en TIMESTAMPTZ  NOT NULL,
    ultima_deteccion_en  TIMESTAMPTZ  NOT NULL,
    normalizada_en       TIMESTAMPTZ,
    cerrada_en           TIMESTAMPTZ,
    reconocida_por_id    UUID         REFERENCES usuario(id),
    reconocida_en        TIMESTAMPTZ,
    cerrada_por_id       UUID         REFERENCES usuario(id),
    motivo_cierre        TEXT,
    cierre_tipo          VARCHAR(15),
    clave_deduplicacion  VARCHAR(200) NOT NULL,
    detalle              JSONB        NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_alerta_severidad CHECK (severidad IN ('BAJA', 'MEDIA', 'ALTA')),
    CONSTRAINT ck_alerta_estado CHECK (estado IN ('ABIERTA', 'RECONOCIDA', 'CERRADA')),
    CONSTRAINT ck_alerta_detecciones CHECK (ultima_deteccion_en >= primera_deteccion_en),
    CONSTRAINT ck_alerta_cierre_tipo CHECK (cierre_tipo IS NULL OR cierre_tipo IN ('MANUAL', 'AUTOMATICO')),
    -- CERRADA exige cerrada_en y cierre_tipo; las demás no los tienen
    CONSTRAINT ck_alerta_cerrada CHECK (
        (estado = 'CERRADA' AND cerrada_en IS NOT NULL AND cierre_tipo IS NOT NULL)
        OR (estado <> 'CERRADA' AND cerrada_en IS NULL AND cierre_tipo IS NULL)
    ),
    -- Cierre manual: usuario y motivo no vacío
    CONSTRAINT ck_alerta_cierre_manual CHECK (
        cierre_tipo IS DISTINCT FROM 'MANUAL'
        OR (cerrada_por_id IS NOT NULL AND motivo_cierre IS NOT NULL AND btrim(motivo_cierre) <> '')
    ),
    -- Reconocer exige usuario y fecha juntos
    CONSTRAINT ck_alerta_reconocida CHECK ((reconocida_por_id IS NULL) = (reconocida_en IS NULL)),
    CONSTRAINT ck_alerta_detalle CHECK (jsonb_typeof(detalle) = 'object')
);
-- Una sola alerta activa por clave de deduplicación (ABIERTA o RECONOCIDA)
CREATE UNIQUE INDEX IF NOT EXISTS uq_alerta_clave_activa
    ON alerta (clave_deduplicacion) WHERE estado IN ('ABIERTA', 'RECONOCIDA');
CREATE INDEX IF NOT EXISTS ix_alerta_envio_estado ON alerta (envio_id, estado);
CREATE INDEX IF NOT EXISTS ix_alerta_sensor_deteccion ON alerta (sensor_id, ultima_deteccion_en);

CREATE TABLE IF NOT EXISTS incidente (
    id               UUID PRIMARY KEY,
    envio_id         UUID        NOT NULL REFERENCES envio(id),
    tramo_id         UUID        REFERENCES tramo(id),
    contenedor_id    UUID        REFERENCES contenedor(id),
    tipo             VARCHAR(40) NOT NULL,
    severidad        VARCHAR(10) NOT NULL,
    estado           VARCHAR(15) NOT NULL DEFAULT 'ABIERTO',
    descripcion      TEXT        NOT NULL,
    ocurrido_en      TIMESTAMPTZ NOT NULL,
    abierto_en       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    responsable_id   UUID        REFERENCES usuario(id),
    resuelto_en      TIMESTAMPTZ,
    resolucion       TEXT,
    alerta_origen_id UUID        UNIQUE REFERENCES alerta(id),
    CONSTRAINT ck_incidente_severidad CHECK (severidad IN ('BAJA', 'MEDIA', 'ALTA')),
    CONSTRAINT ck_incidente_estado CHECK (estado IN ('ABIERTO', 'EN_GESTION', 'RESUELTO')),
    CONSTRAINT ck_incidente_descripcion CHECK (btrim(descripcion) <> ''),
    -- RESUELTO exige fecha y explicación; los demás estados no tienen resolución
    CONSTRAINT ck_incidente_resuelto CHECK (
        (estado = 'RESUELTO' AND resuelto_en IS NOT NULL AND resolucion IS NOT NULL
            AND btrim(resolucion) <> '' AND resuelto_en >= abierto_en)
        OR (estado <> 'RESUELTO' AND resuelto_en IS NULL AND resolucion IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_incidente_estado_apertura ON incidente (estado, abierto_en);
CREATE INDEX IF NOT EXISTS ix_incidente_envio_ocurrido ON incidente (envio_id, ocurrido_en);

CREATE TABLE IF NOT EXISTS incidente_alerta (
    incidente_id UUID NOT NULL REFERENCES incidente(id),
    alerta_id    UUID NOT NULL REFERENCES alerta(id),
    PRIMARY KEY (incidente_id, alerta_id)
);
CREATE INDEX IF NOT EXISTS ix_incidente_alerta_alerta ON incidente_alerta (alerta_id);

-- Marca de idempotencia: lectura (MongoDB, referencia lógica) ya evaluada con una versión de reglas.
CREATE TABLE IF NOT EXISTS lectura_procesada (
    lectura_id     UUID        NOT NULL,
    version_reglas VARCHAR(30) NOT NULL,
    procesada_en   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (lectura_id, version_reglas)
);

-- Coherencia compuesta que una FK simple no garantiza:
--   * tramo_id debe pertenecer al envío (alerta e incidente)
--   * alerta_origen e incidente_alerta deben ser del mismo envío
CREATE OR REPLACE FUNCTION fn_alerta_tramo_del_envio() RETURNS trigger AS $$
BEGIN
    IF NEW.tramo_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM tramo t WHERE t.id = NEW.tramo_id AND t.envio_id = NEW.envio_id
    ) THEN
        RAISE EXCEPTION 'tramo % no pertenece al envio %', NEW.tramo_id, NEW.envio_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_alerta_tramo_del_envio ON alerta;
CREATE TRIGGER trg_alerta_tramo_del_envio
    BEFORE INSERT OR UPDATE OF tramo_id, envio_id ON alerta
    FOR EACH ROW EXECUTE FUNCTION fn_alerta_tramo_del_envio();

DROP TRIGGER IF EXISTS trg_incidente_tramo_del_envio ON incidente;
CREATE TRIGGER trg_incidente_tramo_del_envio
    BEFORE INSERT OR UPDATE OF tramo_id, envio_id ON incidente
    FOR EACH ROW EXECUTE FUNCTION fn_alerta_tramo_del_envio();

CREATE OR REPLACE FUNCTION fn_incidente_alerta_mismo_envio() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM incidente i JOIN alerta a ON a.envio_id = i.envio_id
        WHERE i.id = NEW.incidente_id AND a.id = NEW.alerta_id
    ) THEN
        RAISE EXCEPTION 'incidente % y alerta % no pertenecen al mismo envio', NEW.incidente_id, NEW.alerta_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_incidente_alerta_mismo_envio ON incidente_alerta;
CREATE TRIGGER trg_incidente_alerta_mismo_envio
    BEFORE INSERT OR UPDATE ON incidente_alerta
    FOR EACH ROW EXECUTE FUNCTION fn_incidente_alerta_mismo_envio();
