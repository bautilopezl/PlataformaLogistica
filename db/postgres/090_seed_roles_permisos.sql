-- 090_seed_roles_permisos.sql  ·  Datos base de autorización (idempotente)
-- Perfiles y permisos según las matrices del Documento 2. El alcance (por tercero,
-- asignación o membresía) se comprueba además en la capa de servicios.

INSERT INTO rol (id, codigo, descripcion) VALUES
    (gen_random_uuid(), 'ADMINISTRADOR',      'Administración, usuarios y configuración'),
    (gen_random_uuid(), 'OPERACIONES',        'Envíos, tramos, contenedores, sensores e incidentes'),
    (gen_random_uuid(), 'COMERCIAL',          'Facturas, pagos, cuentas y reportes comerciales'),
    (gen_random_uuid(), 'CLIENTE',            'Consulta de sus envíos y documentación; conversaciones autorizadas'),
    (gen_random_uuid(), 'OPERADOR_LOGISTICO', 'Consulta y actualización de tramos asignados a su empresa')
ON CONFLICT (codigo) DO NOTHING;

INSERT INTO permiso (id, codigo, descripcion) VALUES
    (gen_random_uuid(), 'usuarios.administrar',   'Administrar usuarios y roles'),
    (gen_random_uuid(), 'sesiones.administrar',   'Revocar sesiones ajenas'),
    (gen_random_uuid(), 'terceros.gestionar',     'Crear, editar y desactivar terceros y roles comerciales'),
    (gen_random_uuid(), 'envios.crear',           'Crear envíos'),
    (gen_random_uuid(), 'envios.planificar',      'Planificar envíos, tramos y contenedores'),
    (gen_random_uuid(), 'envios.consultar',       'Consultar envíos'),
    (gen_random_uuid(), 'tramos.actualizar',      'Iniciar o finalizar tramos y registrar eventos'),
    (gen_random_uuid(), 'monitoreo.consultar',    'Ver lecturas IoT y posiciones GPS'),
    (gen_random_uuid(), 'alertas.reconocer',      'Reconocer alertas'),
    (gen_random_uuid(), 'alertas.cerrar',         'Cerrar alertas manualmente (con motivo)'),
    (gen_random_uuid(), 'incidentes.crear',       'Abrir incidentes'),
    (gen_random_uuid(), 'incidentes.gestionar',   'Resolver o asignar incidentes'),
    (gen_random_uuid(), 'cargos.gestionar',       'Crear cargos de envío'),
    (gen_random_uuid(), 'facturas.emitir',        'Emitir facturas'),
    (gen_random_uuid(), 'facturas.anular',        'Anular facturas'),
    (gen_random_uuid(), 'pagos.registrar',        'Registrar pagos y cobros'),
    (gen_random_uuid(), 'pagos.aplicar',          'Aplicar pagos a facturas'),
    (gen_random_uuid(), 'pagos.anular',           'Anular pagos'),
    (gen_random_uuid(), 'cuentas.consultar',      'Consultar cuentas corrientes'),
    (gen_random_uuid(), 'cuentas.cerrar',         'Cierre mensual de cuentas'),
    (gen_random_uuid(), 'conversaciones.crear',   'Crear grupos y canales'),
    (gen_random_uuid(), 'reportes.operativos',    'Solicitar reportes operativos y analíticos'),
    (gen_random_uuid(), 'reportes.comerciales',   'Solicitar reportes comerciales'),
    (gen_random_uuid(), 'predicciones.consultar', 'Consultar predicciones de riesgo'),
    (gen_random_uuid(), 'auditoria.consultar',    'Consultar el registro de auditoría')
ON CONFLICT (codigo) DO NOTHING;

-- Asignación rol -> permisos
WITH asignacion(rol_codigo, permiso_codigo) AS (
    VALUES
    -- ADMINISTRADOR: todos los permisos (ver INSERT aparte abajo)
    -- OPERACIONES
    ('OPERACIONES', 'envios.crear'), ('OPERACIONES', 'envios.planificar'), ('OPERACIONES', 'envios.consultar'),
    ('OPERACIONES', 'tramos.actualizar'), ('OPERACIONES', 'monitoreo.consultar'),
    ('OPERACIONES', 'alertas.reconocer'), ('OPERACIONES', 'alertas.cerrar'),
    ('OPERACIONES', 'incidentes.crear'), ('OPERACIONES', 'incidentes.gestionar'),
    ('OPERACIONES', 'conversaciones.crear'), ('OPERACIONES', 'reportes.operativos'),
    ('OPERACIONES', 'predicciones.consultar'),
    -- COMERCIAL
    ('COMERCIAL', 'terceros.gestionar'), ('COMERCIAL', 'envios.consultar'), ('COMERCIAL', 'cargos.gestionar'),
    ('COMERCIAL', 'facturas.emitir'), ('COMERCIAL', 'facturas.anular'),
    ('COMERCIAL', 'pagos.registrar'), ('COMERCIAL', 'pagos.aplicar'), ('COMERCIAL', 'pagos.anular'),
    ('COMERCIAL', 'cuentas.consultar'), ('COMERCIAL', 'cuentas.cerrar'), ('COMERCIAL', 'reportes.comerciales'),
    -- CLIENTE (externo; alcance restringido a su empresa en los servicios)
    ('CLIENTE', 'envios.consultar'), ('CLIENTE', 'monitoreo.consultar'), ('CLIENTE', 'cuentas.consultar'),
    ('CLIENTE', 'predicciones.consultar'), ('CLIENTE', 'reportes.operativos'),
    -- OPERADOR_LOGISTICO (externo; solo tramos asignados)
    ('OPERADOR_LOGISTICO', 'envios.consultar'), ('OPERADOR_LOGISTICO', 'tramos.actualizar'),
    ('OPERADOR_LOGISTICO', 'monitoreo.consultar'), ('OPERADOR_LOGISTICO', 'alertas.reconocer'),
    ('OPERADOR_LOGISTICO', 'incidentes.crear'), ('OPERADOR_LOGISTICO', 'cuentas.consultar'),
    ('OPERADOR_LOGISTICO', 'predicciones.consultar')
)
INSERT INTO rol_permiso (rol_id, permiso_id)
SELECT r.id, p.id
FROM asignacion a
JOIN rol r ON r.codigo = a.rol_codigo
JOIN permiso p ON p.codigo = a.permiso_codigo
ON CONFLICT DO NOTHING;

INSERT INTO rol_permiso (rol_id, permiso_id)
SELECT r.id, p.id
FROM rol r CROSS JOIN permiso p
WHERE r.codigo = 'ADMINISTRADOR'
ON CONFLICT DO NOTHING;
