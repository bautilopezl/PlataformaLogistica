-- 040_comercial.sql  ·  Cargos, facturas, pagos, cuentas corrientes y cierres
-- Importes en NUMERIC; nunca flotantes. No se mezclan monedas.

CREATE TABLE IF NOT EXISTS cargo_envio (
    id              UUID PRIMARY KEY,
    envio_id        UUID          NOT NULL REFERENCES envio(id),
    concepto        VARCHAR(200)  NOT NULL,
    cantidad        NUMERIC(14,3) NOT NULL,
    precio_unitario NUMERIC(18,4) NOT NULL,
    moneda_codigo   VARCHAR(3)    NOT NULL,
    estado          VARCHAR(15)   NOT NULL DEFAULT 'PENDIENTE',
    creado_en       TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_cargo_cantidad CHECK (cantidad > 0),
    CONSTRAINT ck_cargo_precio CHECK (precio_unitario >= 0),
    CONSTRAINT ck_cargo_estado CHECK (estado IN ('PENDIENTE', 'FACTURADO', 'CANCELADO')),
    CONSTRAINT ck_cargo_moneda CHECK (moneda_codigo ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_cargo_concepto CHECK (btrim(concepto) <> '')
);
CREATE INDEX IF NOT EXISTS ix_cargo_envio_estado ON cargo_envio (envio_id, estado);

CREATE TABLE IF NOT EXISTS factura (
    id               UUID PRIMARY KEY,
    tercero_id       UUID          NOT NULL REFERENCES tercero(id),
    sentido          VARCHAR(10)   NOT NULL,
    tipo_comprobante VARCHAR(30)   NOT NULL,
    serie            VARCHAR(30),
    numero           VARCHAR(40),
    emitida_en       TIMESTAMPTZ,
    vence_en         TIMESTAMPTZ,
    moneda_codigo    VARCHAR(3)    NOT NULL,
    importe_total    NUMERIC(18,2) NOT NULL DEFAULT 0,
    estado           VARCHAR(15)   NOT NULL DEFAULT 'BORRADOR',
    creada_por_id    UUID          NOT NULL REFERENCES usuario(id),
    creado_en        TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    anulada_en       TIMESTAMPTZ,
    anulada_por_id   UUID          REFERENCES usuario(id),
    motivo_anulacion TEXT,
    CONSTRAINT ck_factura_sentido CHECK (sentido IN ('VENTA', 'COMPRA')),
    CONSTRAINT ck_factura_estado CHECK (estado IN ('BORRADOR', 'EMITIDA', 'ANULADA')),
    CONSTRAINT ck_factura_moneda CHECK (moneda_codigo ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_factura_total CHECK (
        (estado = 'BORRADOR' AND importe_total >= 0) OR (estado <> 'BORRADOR' AND importe_total > 0)
    ),
    -- Serie, número y fechas obligatorios al emitir (y para anuladas, que fueron emitidas)
    CONSTRAINT ck_factura_emision CHECK (
        estado = 'BORRADOR'
        OR (serie IS NOT NULL AND numero IS NOT NULL AND emitida_en IS NOT NULL AND vence_en IS NOT NULL)
    ),
    CONSTRAINT ck_factura_vencimiento CHECK (vence_en IS NULL OR emitida_en IS NULL OR vence_en >= emitida_en),
    CONSTRAINT ck_factura_anulacion CHECK (
        (estado = 'ANULADA' AND anulada_en IS NOT NULL AND anulada_por_id IS NOT NULL
            AND motivo_anulacion IS NOT NULL AND btrim(motivo_anulacion) <> '')
        OR (estado <> 'ANULADA' AND anulada_en IS NULL AND anulada_por_id IS NULL AND motivo_anulacion IS NULL)
    )
);
-- Ventas: documento único por tipo + serie + número (empresa propia única).
-- Compras: numeración del proveedor, por eso se agrega tercero_id.
-- Los números de documentos anulados no se reutilizan (la fila anulada sigue ocupando el número).
CREATE UNIQUE INDEX IF NOT EXISTS uq_factura_venta_numero
    ON factura (tipo_comprobante, COALESCE(serie, ''), numero) WHERE sentido = 'VENTA' AND numero IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_factura_compra_numero
    ON factura (tercero_id, tipo_comprobante, COALESCE(serie, ''), numero) WHERE sentido = 'COMPRA' AND numero IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_factura_tercero_estado_vence ON factura (tercero_id, estado, vence_en);

CREATE TABLE IF NOT EXISTS factura_detalle (
    id              UUID PRIMARY KEY,
    factura_id      UUID          NOT NULL REFERENCES factura(id),
    renglon         INTEGER       NOT NULL,
    envio_id        UUID          REFERENCES envio(id),
    cargo_envio_id  UUID          UNIQUE REFERENCES cargo_envio(id),   -- un cargo se factura una sola vez
    concepto        VARCHAR(200)  NOT NULL,
    cantidad        NUMERIC(14,3) NOT NULL,
    precio_unitario NUMERIC(18,4) NOT NULL,
    subtotal        NUMERIC(18,2) NOT NULL,
    CONSTRAINT uq_factura_detalle_renglon UNIQUE (factura_id, renglon),
    CONSTRAINT ck_fdetalle_renglon CHECK (renglon > 0),
    CONSTRAINT ck_fdetalle_cantidad CHECK (cantidad > 0),
    CONSTRAINT ck_fdetalle_precio CHECK (precio_unitario >= 0),
    CONSTRAINT ck_fdetalle_subtotal CHECK (subtotal >= 0),
    CONSTRAINT ck_fdetalle_concepto CHECK (btrim(concepto) <> '')
);
CREATE INDEX IF NOT EXISTS ix_fdetalle_envio ON factura_detalle (envio_id);

CREATE TABLE IF NOT EXISTS pago (
    id                 UUID PRIMARY KEY,
    tercero_id         UUID          NOT NULL REFERENCES tercero(id),
    sentido            VARCHAR(10)   NOT NULL,
    fecha              TIMESTAMPTZ   NOT NULL,
    monto              NUMERIC(18,2) NOT NULL,
    moneda_codigo      VARCHAR(3)    NOT NULL,
    medio              VARCHAR(20)   NOT NULL,
    referencia         VARCHAR(150),
    estado             VARCHAR(15)   NOT NULL DEFAULT 'CONFIRMADO',
    registrado_por_id  UUID          NOT NULL REFERENCES usuario(id),
    clave_idempotencia UUID          NOT NULL UNIQUE,
    creado_en          TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    anulado_en         TIMESTAMPTZ,
    anulado_por_id     UUID          REFERENCES usuario(id),
    motivo_anulacion   TEXT,
    CONSTRAINT ck_pago_sentido CHECK (sentido IN ('COBRO', 'PAGO')),
    CONSTRAINT ck_pago_monto CHECK (monto > 0),
    CONSTRAINT ck_pago_moneda CHECK (moneda_codigo ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_pago_medio CHECK (medio IN ('EFECTIVO', 'TRANSFERENCIA', 'TARJETA', 'OTRO')),
    CONSTRAINT ck_pago_estado CHECK (estado IN ('CONFIRMADO', 'ANULADO')),
    CONSTRAINT ck_pago_anulacion CHECK (
        (estado = 'ANULADO' AND anulado_en IS NOT NULL AND anulado_por_id IS NOT NULL
            AND motivo_anulacion IS NOT NULL AND btrim(motivo_anulacion) <> '')
        OR (estado <> 'ANULADO' AND anulado_en IS NULL AND anulado_por_id IS NULL AND motivo_anulacion IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_pago_tercero_fecha ON pago (tercero_id, fecha);

CREATE TABLE IF NOT EXISTS pago_aplicacion (
    id               UUID PRIMARY KEY,
    pago_id          UUID          NOT NULL REFERENCES pago(id),
    factura_id       UUID          NOT NULL REFERENCES factura(id),
    monto_aplicado   NUMERIC(18,2) NOT NULL,
    creado_en        TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    anulada_en       TIMESTAMPTZ,
    motivo_anulacion TEXT,
    CONSTRAINT ck_paplic_monto CHECK (monto_aplicado > 0),
    CONSTRAINT ck_paplic_anulacion CHECK (
        (anulada_en IS NULL AND motivo_anulacion IS NULL)
        OR (anulada_en IS NOT NULL AND anulada_en >= creado_en
            AND motivo_anulacion IS NOT NULL AND btrim(motivo_anulacion) <> '')
    )
);
-- Una aplicación activa por par pago-factura; una reaplicación posterior a una anulación crea otra fila
CREATE UNIQUE INDEX IF NOT EXISTS uq_paplic_activa
    ON pago_aplicacion (pago_id, factura_id) WHERE anulada_en IS NULL;
CREATE INDEX IF NOT EXISTS ix_paplic_pago ON pago_aplicacion (pago_id);
CREATE INDEX IF NOT EXISTS ix_paplic_factura ON pago_aplicacion (factura_id);

CREATE TABLE IF NOT EXISTS cuenta_corriente (
    id            UUID PRIMARY KEY,
    tercero_id    UUID        NOT NULL REFERENCES tercero(id),
    naturaleza    VARCHAR(10) NOT NULL,
    moneda_codigo VARCHAR(3)  NOT NULL,
    activo        BOOLEAN     NOT NULL DEFAULT TRUE,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_cuenta UNIQUE (tercero_id, naturaleza, moneda_codigo),
    CONSTRAINT ck_cuenta_naturaleza CHECK (naturaleza IN ('COBRAR', 'PAGAR')),
    CONSTRAINT ck_cuenta_moneda CHECK (moneda_codigo ~ '^[A-Z]{3}$')
);

CREATE TABLE IF NOT EXISTS movimiento_cuenta (
    id           UUID PRIMARY KEY,
    cuenta_id    UUID          NOT NULL REFERENCES cuenta_corriente(id),
    fecha        TIMESTAMPTZ   NOT NULL,
    sentido      VARCHAR(10)   NOT NULL,
    importe      NUMERIC(18,2) NOT NULL,
    factura_id   UUID          REFERENCES factura(id),
    pago_id      UUID          REFERENCES pago(id),
    reversa_de_id UUID         UNIQUE REFERENCES movimiento_cuenta(id),  -- una única reversa por original
    descripcion  TEXT          NOT NULL DEFAULT '',
    clave_origen VARCHAR(150)  NOT NULL UNIQUE,                          -- evita doble contabilización
    creado_en    TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_mov_sentido CHECK (sentido IN ('AUMENTA', 'DISMINUYE')),
    CONSTRAINT ck_mov_importe CHECK (importe > 0),
    -- Exactamente una referencia de origen: factura, pago o reversa
    CONSTRAINT ck_mov_origen CHECK (num_nonnulls(factura_id, pago_id, reversa_de_id) = 1),
    CONSTRAINT ck_mov_no_autoreversa CHECK (reversa_de_id IS NULL OR reversa_de_id <> id)
);
CREATE INDEX IF NOT EXISTS ix_mov_cuenta_fecha ON movimiento_cuenta (cuenta_id, fecha);

-- Los movimientos confirmados son inmutables y no se borran: se corrigen con reversas.
CREATE OR REPLACE FUNCTION fn_movimiento_inmutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'movimiento_cuenta es inmutable: use una reversa (movimiento %)', OLD.id
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_movimiento_inmutable ON movimiento_cuenta;
CREATE TRIGGER trg_movimiento_inmutable
    BEFORE UPDATE OR DELETE ON movimiento_cuenta
    FOR EACH ROW EXECUTE FUNCTION fn_movimiento_inmutable();

-- Una reversa debe ser de la misma cuenta, mismo importe y sentido opuesto; no se revierte una reversa.
CREATE OR REPLACE FUNCTION fn_movimiento_reversa_valida() RETURNS trigger AS $$
DECLARE
    orig movimiento_cuenta%ROWTYPE;
BEGIN
    IF NEW.reversa_de_id IS NOT NULL THEN
        SELECT * INTO orig FROM movimiento_cuenta WHERE id = NEW.reversa_de_id;
        IF orig.cuenta_id <> NEW.cuenta_id
           OR orig.importe <> NEW.importe
           OR orig.sentido = NEW.sentido THEN
            RAISE EXCEPTION 'reversa invalida: debe ser de la misma cuenta, mismo importe y sentido opuesto'
                USING ERRCODE = 'check_violation';
        END IF;
        IF orig.reversa_de_id IS NOT NULL THEN
            RAISE EXCEPTION 'no se permite revertir una reversa'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_movimiento_reversa_valida ON movimiento_cuenta;
CREATE TRIGGER trg_movimiento_reversa_valida
    BEFORE INSERT ON movimiento_cuenta
    FOR EACH ROW EXECUTE FUNCTION fn_movimiento_reversa_valida();

CREATE TABLE IF NOT EXISTS cierre_cuenta (
    id                  UUID PRIMARY KEY,
    cuenta_id           UUID          NOT NULL REFERENCES cuenta_corriente(id),
    periodo             DATE          NOT NULL,
    saldo_inicial       NUMERIC(18,2) NOT NULL,
    total_aumentos      NUMERIC(18,2) NOT NULL,
    total_disminuciones NUMERIC(18,2) NOT NULL,
    saldo_final         NUMERIC(18,2) NOT NULL,
    cerrado_en          TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_cierre UNIQUE (cuenta_id, periodo),
    CONSTRAINT ck_cierre_periodo CHECK (periodo = date_trunc('month', periodo)::date),
    CONSTRAINT ck_cierre_totales CHECK (total_aumentos >= 0 AND total_disminuciones >= 0),
    CONSTRAINT ck_cierre_saldo CHECK (saldo_final = saldo_inicial + total_aumentos - total_disminuciones)
);
