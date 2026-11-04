-- 900_grants.sql  ·  Privilegios de la identidad de ejecución (idempotente)
--
-- Dos identidades distintas (Documento 2, "Sesiones, credenciales y privilegios"):
--   * administración: dueña del esquema, ejecuta migraciones (la usa setup_databases.py)
--   * ejecución: rol `logistica_app_rw`, solo DML sobre las tablas; sin DDL
--
-- El usuario con LOGIN se crea con setup_databases.py a partir de PG_APP_USER / PG_APP_PASSWORD
-- (la clave no se escribe en archivos versionados) y se agrega a este rol de grupo.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logistica_app_rw') THEN
        CREATE ROLE logistica_app_rw NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO logistica_app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO logistica_app_rw;

-- Tablas de catálogo de seguridad: la aplicación solo lee (los cambios los hace administración).
-- Incluye `rol`: los roles los siembra 090_seed_roles_permisos.sql y la app nunca los crea ni renombra.
REVOKE INSERT, UPDATE, DELETE ON rol, permiso, rol_permiso FROM logistica_app_rw;

-- Auditoría: la aplicación solo agrega y consulta registros (además hay un trigger de inmutabilidad).
REVOKE UPDATE, DELETE ON auditoria FROM logistica_app_rw;

-- Registros que nunca se borran desde la aplicación (trazabilidad)
REVOKE DELETE ON
    outbox_evento, movimiento_cuenta, cierre_cuenta, lectura_procesada, prediccion_riesgo
FROM logistica_app_rw;

-- Comercial (fase 6): facturas y pagos se ANULAN, nunca se borran; sus cargos, renglones, aplicaciones y cuentas
-- son su trazabilidad. Así la regla no depende solo del código del servicio.
REVOKE DELETE ON
    factura, factura_detalle, pago, pago_aplicacion, cargo_envio, cuenta_corriente
FROM logistica_app_rw;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO logistica_app_rw;
