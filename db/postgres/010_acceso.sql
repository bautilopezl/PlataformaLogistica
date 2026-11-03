-- 010_acceso.sql  ·  Empresas, usuarios, sesiones, roles y permisos
-- Convenciones: PK UUID generadas por la aplicación, TIMESTAMPTZ en UTC,
-- NUMERIC para importes y magnitudes, FK con borrado restringido.

CREATE TABLE IF NOT EXISTS tercero (
    id                     UUID PRIMARY KEY,
    razon_social           VARCHAR(200) NOT NULL,
    pais_codigo            VARCHAR(2)   NOT NULL,
    tipo_identificacion    VARCHAR(30)  NOT NULL,
    numero_identificacion  VARCHAR(40)  NOT NULL,
    email                  VARCHAR(254),
    telefono               VARCHAR(40),
    direccion              TEXT,
    activo                 BOOLEAN      NOT NULL DEFAULT TRUE,
    creado_en              TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actualizado_en         TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_tercero_identificacion UNIQUE (pais_codigo, tipo_identificacion, numero_identificacion),
    CONSTRAINT ck_tercero_razon_social CHECK (btrim(razon_social) <> ''),
    CONSTRAINT ck_tercero_identificacion CHECK (btrim(numero_identificacion) <> '' AND btrim(tipo_identificacion) <> ''),
    CONSTRAINT ck_tercero_pais CHECK (pais_codigo ~ '^[A-Z]{2}$')
);

CREATE TABLE IF NOT EXISTS tercero_rol (
    tercero_id    UUID        NOT NULL REFERENCES tercero(id),
    rol_comercial VARCHAR(20) NOT NULL,
    activo        BOOLEAN     NOT NULL DEFAULT TRUE,
    PRIMARY KEY (tercero_id, rol_comercial),
    CONSTRAINT ck_tercero_rol CHECK (rol_comercial IN ('CLIENTE', 'PROVEEDOR', 'OPERADOR'))
);
CREATE INDEX IF NOT EXISTS ix_tercero_rol_rol_activo ON tercero_rol (rol_comercial, activo);

CREATE TABLE IF NOT EXISTS usuario (
    id             UUID PRIMARY KEY,
    tercero_id     UUID REFERENCES tercero(id),      -- NULL = usuario interno
    nombre         VARCHAR(100) NOT NULL,
    apellido       VARCHAR(100) NOT NULL,
    email          VARCHAR(254) NOT NULL,
    password_hash  TEXT         NOT NULL,
    activo         BOOLEAN      NOT NULL DEFAULT TRUE,
    creado_en      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actualizado_en TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_usuario_nombres CHECK (btrim(nombre) <> '' AND btrim(apellido) <> ''),
    CONSTRAINT ck_usuario_hash CHECK (btrim(password_hash) <> '')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_usuario_email ON usuario (lower(email));
CREATE INDEX IF NOT EXISTS ix_usuario_tercero ON usuario (tercero_id);

CREATE TABLE IF NOT EXISTS sesion (
    id                  UUID PRIMARY KEY,
    usuario_id          UUID         NOT NULL REFERENCES usuario(id),
    inicio_en           TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    vence_en            TIMESTAMPTZ  NOT NULL,
    fin_en              TIMESTAMPTZ,
    estado              VARCHAR(15)  NOT NULL DEFAULT 'ABIERTA',
    cliente_descripcion VARCHAR(200),
    instancia_id        UUID         NOT NULL,        -- ejecución local; no es credencial ni FK
    CONSTRAINT ck_sesion_estado CHECK (estado IN ('ABIERTA', 'CERRADA', 'REVOCADA')),
    CONSTRAINT ck_sesion_vencimiento CHECK (vence_en > inicio_en),
    CONSTRAINT ck_sesion_fin CHECK (fin_en IS NULL OR fin_en >= inicio_en),
    CONSTRAINT ck_sesion_estado_fin CHECK (
        (estado = 'ABIERTA' AND fin_en IS NULL) OR (estado IN ('CERRADA', 'REVOCADA') AND fin_en IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_sesion_usuario_estado ON sesion (usuario_id, estado, vence_en);

CREATE TABLE IF NOT EXISTS rol (
    id          UUID PRIMARY KEY,
    codigo      VARCHAR(50) NOT NULL UNIQUE,
    descripcion TEXT        NOT NULL DEFAULT '',
    CONSTRAINT ck_rol_codigo CHECK (btrim(codigo) <> '')
);

CREATE TABLE IF NOT EXISTS permiso (
    id          UUID PRIMARY KEY,
    codigo      VARCHAR(100) NOT NULL UNIQUE,
    descripcion TEXT         NOT NULL DEFAULT '',
    CONSTRAINT ck_permiso_codigo CHECK (btrim(codigo) <> '')
);

CREATE TABLE IF NOT EXISTS usuario_rol (
    usuario_id UUID NOT NULL REFERENCES usuario(id),
    rol_id     UUID NOT NULL REFERENCES rol(id),
    PRIMARY KEY (usuario_id, rol_id)
);
CREATE INDEX IF NOT EXISTS ix_usuario_rol_rol ON usuario_rol (rol_id);

CREATE TABLE IF NOT EXISTS rol_permiso (
    rol_id     UUID NOT NULL REFERENCES rol(id),
    permiso_id UUID NOT NULL REFERENCES permiso(id),
    PRIMARY KEY (rol_id, permiso_id)
);
CREATE INDEX IF NOT EXISTS ix_rol_permiso_permiso ON rol_permiso (permiso_id);

-- Auditoría de operaciones relevantes (login, cambios de usuarios, roles, terceros, sesiones...).
-- Cada fila se inserta en la MISMA transacción que la operación auditada: si la operación se revierte, la
-- fila también. Los intentos rechazados (login fallido) se registran en una transacción propia.
-- Nunca guarda contraseñas, hashes ni datos personales innecesarios: solo identificadores y un detalle
-- mínimo.
-- Preparado para particionar (AGENTS.md §8.2): la columna de partición sería `id` (hash), que ya es la clave
-- primaria; ninguna tabla tiene claves foráneas hacia `auditoria`.
CREATE TABLE IF NOT EXISTS auditoria (
    id           UUID PRIMARY KEY,
    -- clock_timestamp(): hora real del registro, no la del inicio de la transacción.
    fecha        TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    accion       VARCHAR(60)  NOT NULL,
    resultado    VARCHAR(10)  NOT NULL DEFAULT 'EXITO',
    usuario_id   UUID REFERENCES usuario(id),          -- quién (NULL: login con email inexistente)
    sesion_id    UUID REFERENCES sesion(id),
    instancia_id UUID,                                             -- ejecución de la app; no es FK
    entidad      VARCHAR(40),                                      -- sobre qué: usuario, tercero, sesion...
    entidad_id   UUID,
    detalle      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_auditoria_accion CHECK (btrim(accion) <> ''),
    CONSTRAINT ck_auditoria_resultado CHECK (resultado IN ('EXITO', 'RECHAZO')),
    CONSTRAINT ck_auditoria_detalle CHECK (jsonb_typeof(detalle) = 'object'),
    CONSTRAINT ck_auditoria_entidad CHECK ((entidad IS NULL) = (entidad_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_auditoria_fecha ON auditoria (fecha DESC, id);
CREATE INDEX IF NOT EXISTS ix_auditoria_usuario_fecha ON auditoria (usuario_id, fecha DESC);
CREATE INDEX IF NOT EXISTS ix_auditoria_entidad ON auditoria (entidad, entidad_id, fecha DESC);

-- Inmutable también para la identidad de administración (la de ejecución, además, no tiene UPDATE/DELETE:
-- ver 900_grants.sql). Corregir un registro = agregar otro.
CREATE OR REPLACE FUNCTION fn_auditoria_inmutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'auditoria es inmutable (registro %)', OLD.id USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_auditoria_inmutable ON auditoria;
CREATE TRIGGER trg_auditoria_inmutable
    BEFORE UPDATE OR DELETE ON auditoria
    FOR EACH ROW EXECUTE FUNCTION fn_auditoria_inmutable();
