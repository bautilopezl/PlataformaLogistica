# Plataforma de logística internacional · Persistencia políglota

Aplicación de escritorio (Python + PySide6) conectada **directamente** a PostgreSQL, MongoDB y Neo4j.
Sin API, sin servidor HTTP, sin Tauri (restricción confirmada por el profesor).

## Puesta en marcha

```bash
cp .env.example .env            # completar claves
docker compose up -d            # las tres bases
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python scripts/setup_databases.py                     # crea tablas, colecciones, constraints, roles
PYTHONPATH=src python scripts/crear_admin.py --email admin@empresa.com --nombre Ana --apellido Paz   # primer administrador (pide la clave)
PYTHONPATH=src python scripts/sincronizar.py --estado     # cola de la outbox (Mongo y Neo4j); ver --help
PYTHONPATH=src python -m logistica.main               # Windows PowerShell: $env:PYTHONPATH="src"; python -m logistica.main
```

`setup_databases.py` es idempotente (`--only postgres|mongo|neo4j`, `--reset --yes` borra todo y recrea).

**Preparado para particionar (no activado):** no hay clúster, así que por defecto no se particiona, pero el proyecto ya lo deja listo y probado:
- PostgreSQL: `python scripts/setup_databases.py --only postgres --particionar` reparte por hash `lectura_procesada` y `outbox_entrega` en 4 particiones (script `db/postgres/opcional/950_particionar_hash.sql`, idempotente, conserva datos; probado en `tests/test_particionamiento_postgres.py`).
- MongoDB: `lecturas_sensor` tiene clave de sharding `(sensor_id, fecha_medicion)`; `setup_mongo.py` la aplica solo si se conecta a un `mongos` y en servidor único lo omite.
Usa la identidad de **administración**; la aplicación usa `PG_APP_USER` (solo DML, sin DDL).

## Estructura

```
db/postgres/   000 extensiones · 010 acceso · 020 logística · 030 monitoreo · 040 comercial
               050 comunicación/procesos · 090 seed roles y permisos · 900 grants  (38 tablas)
db/mongo/      setup_mongo.py  → validadores $jsonSchema e índices (3 colecciones)
db/neo4j/      constraints.cypher → unicidad e índices
scripts/       setup_databases.py
src/logistica/ core (config, logging sin secretos, errores) · domain
               repositories (base.py: traducción de errores · postgres/ · mongo/ · neo4j/ · connections.py)
               services (health_service.py) · ui (worker.py · theme.py · main_window.py) · main.py
tests/         test_postgres_integrity.py (37) · test_particionamiento_postgres.py (3)
               test_static_artifacts.py (6) · test_fase0_cimientos.py (43)
               test_fase1_dominio.py (28) · test_fase1_acceso.py (52) · test_fase1_ui.py (22)
               test_fase2_dominio.py (35) · test_fase2_logistica.py (34) · test_fase2_revision.py (16)
               test_fase2_ui.py (9) · test_fase2_concurrencia.py (15)
               test_fase3_dominio.py (3) · test_fase3_sincronizacion.py (18)
```

Capas: `ui → services → repositories → (PostgreSQL | MongoDB | Neo4j)`. La UI nunca ejecuta SQL ni
consultas bloqueantes: usa `ui/worker.py` (`Worker` sobre `QThreadPool` + señales; ver `ui/main_window.py`).
Los repositorios envuelven cada acceso con `translate_errors` para que servicios y UI solo vean errores de
`core/errors.py`. `DB_TIMEOUT_S` (`.env`, 5 s por defecto) acota la conexión a cada motor.

## Pruebas

```bash
pytest tests/test_static_artifacts.py tests/test_fase0_cimientos.py    # sin servidores (UI en modo offscreen)
LOGISTICA_TEST_CONNECTIONS=1 LOGISTICA_TEST_PG="host=localhost port=5432 dbname=logistica user=logistica_admin password=..." pytest tests
```

- `LOGISTICA_TEST_PG`: pruebas de integridad y particionamiento. Corren dentro de una transacción que se revierte: no dejan datos.
- `LOGISTICA_TEST_CONNECTIONS=1`: pruebas de conexión contra los tres motores usando el `.env` del proyecto.
- Sin esas variables, esas pruebas se omiten con el motivo indicado.

## Qué está verificado y qué no

| Parte | Estado |
|---|---|
| DDL PostgreSQL | Ejecutado dos veces seguidas contra PostgreSQL 16.15 real: 37 tablas, sin errores (idempotente). Hoy son 38: se agregó `auditoria` el 3/11/2026 |
| Restricciones PostgreSQL | 35 pruebas pasan; se comprobó que cada rechazo viene de la restricción prevista |
| `setup_databases.py` (PostgreSQL) | Ejecutado; el rol de ejecución puede leer/escribir pero no crear tablas ni borrar la outbox |
| Validadores/índices MongoDB | Ejecutados contra MongoDB 7.0.45: los inválidos se rechazan (código 121) y los índices existen |
| Constraints Neo4j | Ejecutados contra Neo4j 5.26 Community: 7 constraints y 5 índices; un `:Envio` duplicado se rechaza |
| Fase 0 (cimientos) | Ventana abierta en Windows 11; muestra el estado de los 3 motores sin congelarse (probado con un chequeo lento); un motor caído solo marca ese motor; logs sin contraseñas; un chequeo colgado se corta por timeout. 85 pruebas en verde con servidores |
| Fase 1 (acceso) | Login argon2id, sesiones verificadas en PostgreSQL en cada operación, revocación, usuarios y roles, ABM de terceros y roles comerciales, pestañas por perfil. Probado contra PostgreSQL real con la identidad limitada: la matriz de los 5 perfiles, revocar sesión / desactivar usuario / quitar rol cortan la operación siguiente, alcance por empresa. **Falta** probarlo a mano en la ventana y los datos de demostración |
| Fase 2 (logística) | Ubicaciones, vehículos, contenedores, sensores (instalación sin solapamiento), envíos con tramos continuos, asignación de contenedores con capacidad, planificación, inicio/fin de tramos, eventos, entrega y cancelación; outbox en la misma transacción. Probado contra PostgreSQL real (envío completo de punta a punta, reglas de §10.2, alcance por perfil) y el contrato de eventos contra el validador real de MongoDB. Supuestos S1–S10 en `AGENTS.md` §16. **Falta** probarlo a mano en la ventana |
| Fase 3 (sincronización) | Outbox con reclamo atómico (dos trabajadores no procesan la misma entrega), histórico de eventos en MongoDB (idempotente por `_id`) y proyección de cada envío en Neo4j recalculada desde PostgreSQL con guarda de versión. Con Neo4j caído se sigue operando y al volver el grafo converge sin duplicados (probado; la caída se simula con un puerto sin servidor). `scripts/sincronizar.py` procesa, reencola fallidas o reconstruye el grafo |

Pendiente de implementar: fases 4 a 9 de `AGENTS.md` (monitoreo,
trazabilidad, comercial, analítica, procesos periódicos, cierre).

## Decisiones tomadas al pasar el diseño a DDL (revisarlas con tu compañero)

Cosas que **no figuraban** en los Documentos 1 y 2 y elegí yo:

- `tramo.medio`: `TERRESTRE`, `MARITIMO`, `AEREO` (el documento solo decía `VARCHAR(15)`).
- `envio`: agregué `origen_id <> destino_id`.
- `prediccion_riesgo`: sin probabilidad, `riesgo_operativo` debe ser `SIN_DATOS` (coherente con el método descrito).
- MongoDB `lecturas_sensor`: agregué el subdocumento `evaluacion` (estado, versión de reglas, intentos,
  reclamo) porque el Documento 2 lo usa pero el Documento 1 no lo listaba. Conviene unificar ambos documentos.
- MongoDB `eventos_logisticos`: validación de `detalles` por tipo solo para `RETRASO` y `ADUANA`;
  el resto queda como objeto libre. Los tipos de evento listados son una propuesta.
- Neo4j: Tercero con etiquetas adicionales `:Cliente`/`:Operador` (un nodo por empresa), resolviendo la
  decisión que el Documento 1 dejaba abierta.
- Neo4j Community no permite constraints de existencia ni unicidad sobre relaciones: la unicidad de
  `UTILIZA.asignacion_id` y `(envio_id, orden)` de Tramo debe garantizarse con `MERGE` en el repositorio.
- Reglas entre filas implementadas como triggers (no como CHECK): compromiso de entrega inmutable,
  movimientos inmutables, reversas válidas, tramo del mismo envío en alertas/incidentes, outbox inmutable.
  Siguen en la capa de servicios (con transacciones y bloqueos): continuidad de tramos, capacidad total del
  contenedor, compatibilidad cuenta/pago/factura (tercero, moneda, sentido) y límites de aplicación de pagos.
