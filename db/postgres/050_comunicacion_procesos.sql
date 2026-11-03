-- 050_comunicacion_procesos.sql  ·  Conversaciones, reportes, predicción y outbox

CREATE TABLE IF NOT EXISTS conversacion (
    id            UUID PRIMARY KEY,
    tipo          VARCHAR(15)  NOT NULL,
    nombre        VARCHAR(150),
    envio_id      UUID         REFERENCES envio(id),
    creada_por_id UUID         NOT NULL REFERENCES usuario(id),
    creado_en     TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archivada_en  TIMESTAMPTZ,
    CONSTRAINT ck_conv_tipo CHECK (tipo IN ('PRIVADA', 'GRUPAL', 'PUBLICA')),
    -- Grupal y pública exigen nombre
    CONSTRAINT ck_conv_nombre CHECK (
        tipo = 'PRIVADA' OR (nombre IS NOT NULL AND btrim(nombre) <> '')
    )
);
CREATE INDEX IF NOT EXISTS ix_conv_envio ON conversacion (envio_id);

CREATE TABLE IF NOT EXISTS conversacion_miembro (
    id              UUID PRIMARY KEY,
    conversacion_id UUID        NOT NULL REFERENCES conversacion(id),
    usuario_id      UUID        NOT NULL REFERENCES usuario(id),
    rol             VARCHAR(15) NOT NULL DEFAULT 'MIEMBRO',
    ingreso_en      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    salida_en       TIMESTAMPTZ,
    CONSTRAINT ck_convmiembro_rol CHECK (rol IN ('ADMINISTRADOR', 'MIEMBRO')),
    CONSTRAINT ck_convmiembro_salida CHECK (salida_en IS NULL OR salida_en > ingreso_en),
    -- Un usuario no puede tener dos membresías simultáneas en la misma conversación
    CONSTRAINT ex_convmiembro_solapamiento EXCLUDE USING gist (
        conversacion_id WITH =,
        usuario_id WITH =,
        tstzrange(ingreso_en, COALESCE(salida_en, 'infinity'::timestamptz), '[)') WITH &&
    )
);
CREATE INDEX IF NOT EXISTS ix_convmiembro_usuario_salida ON conversacion_miembro (usuario_id, salida_en);

CREATE TABLE IF NOT EXISTS reporte (
    id               UUID PRIMARY KEY,
    solicitado_por_id UUID        NOT NULL REFERENCES usuario(id),
    tipo             VARCHAR(60)  NOT NULL,
    parametros       JSONB        NOT NULL DEFAULT '{}'::jsonb,   -- filtros validados, nunca SQL del usuario
    formato          VARCHAR(10)  NOT NULL,
    estado           VARCHAR(15)  NOT NULL DEFAULT 'PENDIENTE',
    solicitado_en    TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    iniciado_en      TIMESTAMPTZ,
    finalizado_en    TIMESTAMPTZ,
    archivo_clave    TEXT,
    error_codigo     VARCHAR(60),
    CONSTRAINT ck_reporte_formato CHECK (formato IN ('PDF', 'CSV')),
    CONSTRAINT ck_reporte_estado CHECK (estado IN ('PENDIENTE', 'EN_PROCESO', 'COMPLETADO', 'ERROR')),
    CONSTRAINT ck_reporte_parametros CHECK (jsonb_typeof(parametros) = 'object'),
    CONSTRAINT ck_reporte_resultado CHECK (
        (estado = 'COMPLETADO' AND archivo_clave IS NOT NULL AND finalizado_en IS NOT NULL AND error_codigo IS NULL)
        OR (estado = 'ERROR' AND error_codigo IS NOT NULL AND finalizado_en IS NOT NULL)
        OR (estado IN ('PENDIENTE', 'EN_PROCESO') AND finalizado_en IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_reporte_solicitante ON reporte (solicitado_por_id, solicitado_en);
CREATE INDEX IF NOT EXISTS ix_reporte_pendientes ON reporte (solicitado_en) WHERE estado IN ('PENDIENTE', 'EN_PROCESO');

CREATE TABLE IF NOT EXISTS prediccion_riesgo (
    id                 UUID PRIMARY KEY,
    envio_id           UUID         NOT NULL REFERENCES envio(id),
    fecha_evaluacion   DATE         NOT NULL,
    probabilidad_demora NUMERIC(6,5),
    riesgo_climatico   VARCHAR(15)  NOT NULL DEFAULT 'SIN_DATOS',
    riesgo_operativo   VARCHAR(15)  NOT NULL DEFAULT 'SIN_DATOS',
    metodo_codigo      VARCHAR(50)  NOT NULL,
    metodo_version     VARCHAR(30)  NOT NULL,
    datos_entrada      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    explicacion        TEXT         NOT NULL DEFAULT '',
    calculado_en       TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_prediccion UNIQUE (envio_id, fecha_evaluacion, metodo_codigo, metodo_version),
    CONSTRAINT ck_pred_probabilidad CHECK (probabilidad_demora IS NULL OR probabilidad_demora BETWEEN 0 AND 1),
    CONSTRAINT ck_pred_clima CHECK (riesgo_climatico IN ('BAJO', 'MEDIO', 'ALTO', 'SIN_DATOS')),
    CONSTRAINT ck_pred_operativo CHECK (riesgo_operativo IN ('BAJO', 'MEDIO', 'ALTO', 'SIN_DATOS')),
    CONSTRAINT ck_pred_datos CHECK (jsonb_typeof(datos_entrada) = 'object'),
    -- Sin probabilidad, el riesgo operativo es SIN_DATOS
    CONSTRAINT ck_pred_sin_datos CHECK (probabilidad_demora IS NOT NULL OR riesgo_operativo = 'SIN_DATOS')
);
CREATE INDEX IF NOT EXISTS ix_pred_envio_calculado ON prediccion_riesgo (envio_id, calculado_en);

CREATE TABLE IF NOT EXISTS outbox_evento (
    id               UUID PRIMARY KEY,
    agregado_tipo    VARCHAR(50)  NOT NULL,
    agregado_id      UUID         NOT NULL,                       -- referencia lógica, sin FK
    agregado_version BIGINT       NOT NULL,
    evento_tipo      VARCHAR(80)  NOT NULL,
    esquema_version  INTEGER      NOT NULL DEFAULT 1,
    payload          JSONB        NOT NULL,
    creado_en        TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_outbox_versiones CHECK (agregado_version > 0 AND esquema_version > 0),
    CONSTRAINT ck_outbox_payload CHECK (jsonb_typeof(payload) = 'object')
);
CREATE INDEX IF NOT EXISTS ix_outbox_agregado ON outbox_evento (agregado_tipo, agregado_id, agregado_version);

-- El evento es inmutable; el seguimiento mutable vive por consumidor (destino).
CREATE OR REPLACE FUNCTION fn_outbox_evento_inmutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'outbox_evento es inmutable (evento %)', OLD.id USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_outbox_evento_inmutable ON outbox_evento;
CREATE TRIGGER trg_outbox_evento_inmutable
    BEFORE UPDATE ON outbox_evento
    FOR EACH ROW EXECUTE FUNCTION fn_outbox_evento_inmutable();

CREATE TABLE IF NOT EXISTS outbox_entrega (
    evento_id          UUID         NOT NULL REFERENCES outbox_evento(id),
    destino            VARCHAR(40)  NOT NULL,                     -- MONGO_EVENTOS, NEO4J_LOGISTICA
    estado             VARCHAR(15)  NOT NULL DEFAULT 'PENDIENTE',
    intentos           INTEGER      NOT NULL DEFAULT 0,
    proximo_intento_en TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    bloqueada_hasta    TIMESTAMPTZ,
    procesada_en       TIMESTAMPTZ,
    ultimo_error       TEXT,
    propietario_id     UUID,
    version_reclamo    BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (evento_id, destino),
    CONSTRAINT ck_entrega_estado CHECK (estado IN ('PENDIENTE', 'EN_PROCESO', 'COMPLETADA', 'FALLIDA')),
    CONSTRAINT ck_entrega_intentos CHECK (intentos >= 0 AND version_reclamo >= 0),
    CONSTRAINT ck_entrega_completada CHECK (estado <> 'COMPLETADA' OR procesada_en IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_entrega_estado_proximo ON outbox_entrega (estado, proximo_intento_en);
