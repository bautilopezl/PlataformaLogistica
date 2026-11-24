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
python scripts/simulador_iot.py --help                   # escenarios independientes de monitoreo
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
scripts/       setup_databases.py · seed_demo.py (demostración) · reconciliar.py (diagnóstico) ·
               empaquetar.py (ejecutable, fase 9)
src/logistica/ core (config, logging sin secretos, errores) · domain
               repositories (base.py: traducción de errores · postgres/ · mongo/ · neo4j/ · connections.py)
               services (health_service.py, reconciliacion_service.py) · ui (worker.py · theme.py ·
               main_window.py) · main.py
tests/         test_postgres_integrity.py (37) · test_particionamiento_postgres.py (3)
               test_static_artifacts.py (6) · test_fase0_cimientos.py (43)
               test_fase1_dominio.py (28) · test_fase1_acceso.py (52) · test_fase1_ui.py (23)
               test_fase2_dominio.py (35) · test_fase2_logistica.py (34) · test_fase2_revision.py (16)
               test_fase2_ui.py (9) · test_fase2_concurrencia.py (15)
               test_fase3_dominio.py (3) · test_fase3_sincronizacion.py (18)
               test_fase5_trazabilidad.py (11, requiere los tres motores)
               test_fase5_comunicacion.py (27) · test_fase5_chat_ui.py (12)
               test_fase9_seed_demo.py (15, requiere los tres motores; idempotencia, recuperación y
               sincronización de scripts/seed_demo.py)
               test_fase9_reconciliacion.py (10, requiere los tres motores; detección de discrepancias
               Neo4j/Mongo/comerciales de services/reconciliacion_service.py)
docs/          informe-final.md · entrega-reproducible.md · guion-defensa.md (informe final y entrega, fase 9)
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
| Fase 1 (acceso) | Login, permisos, alcance, revocación, usuarios, terceros, sesiones y auditoría con pruebas automáticas. El grupo informó creación manual de terceros desde la UI; no se atribuyen pruebas manuales nuevas a esta integración |
| Fase 2 (logística) | Circuito probado automáticamente. El grupo informó creación manual de ubicaciones, vehículo, contenedor, sensor e instalación; creación, planificación, inicio, finalización, aduana y entrega de un envío. `ENV-20261103-GHP2TD` permanece ENTREGADO: su entrega fue elegida por el grupo y no se modifica |
| Fase 3 (sincronización) | Outbox con reclamo atómico (dos trabajadores no procesan la misma entrega), histórico de eventos en MongoDB (idempotente por `_id`) y proyección de cada envío en Neo4j recalculada desde PostgreSQL con guarda de versión. Con Neo4j caído se sigue operando y al volver el grafo converge sin duplicados (probado; la caída se simula con un puerto sin servidor). `scripts/sincronizar.py` procesa, reencola fallidas o reconstruye el grafo |
| Fase 4 (monitoreo) | Servicio compuesto, pantalla Qt con condiciones, sensores, últimas mediciones, historial filtrable/paginado, alertas, incidentes y GPS nativo. El grupo informó pruebas manuales de demos de temperatura, recuperación y desconexión; historial de lecturas y mapa GPS; gestión y resolución de un incidente. Al recuperar, la alerta de temperatura se cierra automáticamente y el incidente queda abierto; `SIN_COMUNICACION` también se cierra al recuperar contacto |
| Fase 5 (trazabilidad y chat) | El grupo informó línea de tiempo del envío entregado, ruta ACTUALIZADA 8/8, asignación del contenedor/tramo y GPS vacío esperado. **Repitió Trazabilidad → Red:** aparece **Exportadora Demo SA**, el gráfico es legible y funciona **Ctrl+rueda**. No acredita trazabilidad completa ni permisos externos. Chat privado/grupal/público interno implementado, con pruebas automáticas de servicios, dos ventanas y dos procesos Qt con sesiones independientes; **chat manual del grupo pendiente** |
| Fase 9 (siembra, reconciliación, ejecutable) | Seed corrido contra la base real con código 0 (conteos confirmados por consulta directa). Reconciliación de solo lectura (Neo4j/Mongo/comercial) con 10 pruebas contra los tres motores reales, integrada como tarea diaria del planificador. Ejecutable compilado con PyInstaller y verificado: abre la ventana de login con `.env` presente, falla con código 2 y sin ventana si falta. **Nada de esto se probó a mano en la ventana todavía** |

Pendiente: prueba manual del grupo del chat; prueba manual de las fases 6 a 9 (facturación, reportes/predicción,
procesos periódicos, siembra de demostración, reconciliación y ejecutable — todas con pruebas automáticas pero
**no probadas a mano en la ventana**); convertir `docs/informe-final.md`/`docs/entrega-reproducible.md`/
`docs/guion-defensa.md` al formato final que pida la cátedra si Markdown no alcanza; y confirmar contra qué
instancia de entrega corresponde el 4/11/2026.
Estado al cierre de la fase 9 (4/11/2026): **604 casos en total** (conteo estable: dos corridas completas
colectaron exactamente 604 cada una). **Ninguna de las dos corridas completas dio 604/604**: una, con un
build de PyInstaller compilando en paralelo, dio 602 aprobadas/2 falladas/1 error de teardown; otra, sin ese
build pero con la máquina compartida por otras aplicaciones de alto uso de CPU (confirmado con
`Get-Process`), dio 597 aprobadas/7 falladas. Entre ambas aparecieron 8 identidades de prueba distintas con
problemas, siempre las mismas, y siempre una aserción sobre tiempo real de respuesta (chequeo de los tres
motores con límite de 6 s, subprocesos de Python/Qt con límite de 30-35 s) — nunca una prueba de lógica de
negocio. Reejecutadas aisladas, el subconjunto de 22 casos de la primera corrida pasó 22/22, pero de los 7
casos de la segunda corrida solo 3 pasaron aislados (4 siguieron fallando con la misma carga de CPU todavía
presente): la carga del equipo compartido es la hipótesis más probable, no una conclusión confirmada con un
experimento controlado. El detalle completo (qué prueba, qué corrida, qué reejecución) está en
[`docs/informe-final.md` §7](./docs/informe-final.md). No se tocó ningún timeout del código para "arreglar"
esto. Antes de la corrida de la tarde se reaplicó
`db/postgres/900_grants.sql` (`setup_databases.py --only postgres`, idempotente, sin `--reset`): revoca
DELETE sobre las tablas comerciales para la identidad de ejecución, un cambio de privilegios
(no de esquema ni de datos) que venía en el pull y que la base local todavía no tenía aplicado; sin eso, 6
pruebas de `test_fase6_comercial.py` fallaban. Se verificó antes y después que `ENV-20261103-GHP2TD` sigue
ENTREGADO con sus mismas fechas y que los conteos de `tercero`/`usuario`/`envio`/`sesion` no cambiaron (salvo
por la siembra de demostración, corrida después y confirmada por separado).


**Decisión del grupo para la fase 6 (4/11/2026):** la facturación automática se genera **al confirmar la
entrega de cada envío**: una factura por envío entregado, con sus cargos facturables. Siguen abiertas la
numeración de comprobantes, las anulaciones y la moneda por defecto (`AGENTS.md` §16.3).

## Comercial: facturación, pagos y cuentas (fase 6)

**Circuito.** Comercial carga los **cargos** de un envío (lo que se le cobra al cliente; no es el costo del tramo).
Operaciones confirma la entrega y, al minuto como máximo, el planificador de procesos (cualquier instancia interna
abierta, o `scripts/procesos.py --continuo`) emite **una factura de venta por moneda** con los cargos pendientes. La factura, sus renglones, el
estado de los cargos y el movimiento de la cuenta por cobrar quedan en **una sola transacción**, junto con la
marca de la entrega `FACTURACION` de la outbox: una reentrega nunca factura dos veces. Si la facturación falla, la
entrega del envío **igual queda confirmada** y la factura se reintenta.

**Anular** nunca borra: la factura queda `ANULADA` con motivo y su movimiento se compensa con una reversa. No se
anula con pagos aplicados (primero se anula la aplicación). Quien anula elige **refacturar**: sí crea cargos de
reemplazo para facturar a mano en **Comercial → Cargos → Facturar pendientes**; no termina ahí.

**Pagos.** Un cobro o pago disminuye la cuenta al registrarse y después se **aplica** a una o varias facturas
(parcial o total), nunca por encima del disponible ni del saldo. **Cierre mensual**: en **Cuentas corrientes**,
elegir la cuenta y **Cerrar período** (solo meses terminados; repetirlo devuelve el mismo cierre).

**Quién ve qué.** Comercial y Administración operan; Operaciones no ve la pestaña. Un cliente ve solo sus
facturas, cobros y su cuenta **por cobrar**; un operador solo lo que se le debe (**por pagar**).

**Envío, cargo, factura y cobro son cosas distintas.** El *costo* de un tramo (lo que nos cobra el operador) no es
el *precio de venta*: el precio al cliente son los **cargos** del envío. Sin cargos no hay factura (no se inventan
importes ni impuestos). Emitir una factura **no** registra un cobro: el cobro se registra cuando efectivamente se
recibe el dinero. **Venta** (FV-…, la emitimos al cliente) se cancela con un **cobro**; **compra** (la registramos al
recibirla de un operador/proveedor) se cancela con un **pago**. Un cobro nunca se aplica a una factura de compra.

**Recorrido desde la interfaz (Comercial o Administración):**
1. **Envíos → Nuevo envío** → completar → (se abre el detalle) **Definir tramos**, **Asignar contenedor**,
   **Planificar**, **Iniciar tramo**…
2. En el detalle del envío: **Cargos / facturación** → **Agregar precio / cargo** (concepto, cantidad, precio
   unitario de venta y moneda). La línea *Comercial:* del detalle muestra si falta el precio.
3. **Finalizar tramo** → **Confirmar entrega**. La entrega nunca se bloquea por un problema comercial: si el envío
   no tiene precio se avisa y se ofrece cargarlo. Con precio, la factura de venta se emite sola (≤ 1 min con una
   instancia interna abierta). Cargos agregados después de entregar, de reemplazo o con la facturación automática
   FALLIDA: **Cargos / facturación → Facturar pendientes** (informa la factura emitida).
4. **Cargos / facturación → Ver factura / PDF** (o **Comercial → Facturas → Ver detalle / PDF**): detalle legible y
   **Exportar PDF** (`REPORTES_DIR/comprobantes`). Es un documento **académico/interno sin validez fiscal** (no es un
   comprobante autorizado por ARCA); usa los importes guardados en la factura y muestra aparte el estado de
   cobranza a la fecha. El emisor sale de `EMISOR_RAZON_SOCIAL`, `EMISOR_IDENTIFICACION`, `EMISOR_DOMICILIO`
   (opcionales, ver `.env.example`); si faltan, el PDF lo dice.
5. Al recibir el dinero: seleccionar la factura → **Registrar cobro de esta factura** (en compras: **Registrar pago al
   proveedor de esta factura**). Tercero, moneda y saldo vienen precargados; se confirman fecha, importe, medio y el
   importe a aplicar. Registrar y aplicar ocurren en **una transacción**: si algo falla no queda ninguno. Un
   excedente queda como saldo a favor del cobro.
6. Un cobro registrado antes sin factura (**Pagos → Registrar cobro o pago**) se aplica después con **Pagos →
   Aplicar a factura**: las facturas candidatas se buscan en la base (no dependen del filtro de la pestaña) y, si no
   hay, se explica por qué (por ejemplo, el tercero solo tiene facturas de compra).

**Prueba manual sugerida (pendiente del grupo):** el recorrido anterior con un envío nuevo; un cobro parcial y otro
por el saldo; ver el saldo en **Cuentas corrientes**; anular una factura con y sin refacturación.

**Pruebas automáticas de este corte:** `tests/test_fase6_dominio.py` (sin base),
`tests/test_fase6_comercial.py` (PostgreSQL real en transacción revertida), `tests/test_fase6_ui.py` (Qt
offscreen) y `tests/test_fase6_concurrencia.py`, que crea una **base PostgreSQL temporal** con nombre UUID, aplica
`db/postgres/*.sql`, corre carreras con **dos conexiones reales** (doble facturación, sobre-aplicación, cierre
repetido, numeración simultánea) y la elimina al terminar. Los movimientos y la auditoría son inmutables, por eso
esas carreras no usan la base de desarrollo. Supuestos C1–C7 en `AGENTS.md` §16.

**Cambio de esquema de este corte:** `db/postgres/900_grants.sql` quita a la identidad de ejecución el DELETE sobre `factura`, `factura_detalle`, `pago`, `pago_aplicacion`, `cargo_envio` y `cuenta_corriente` (se anulan, nunca se borran). Es idempotente: aplicarlo con `python scripts/setup_databases.py --only postgres`.

## Reportes, analítica y predicción (fase 7)

En la pestaña **Reportes**: elegir el tipo, los filtros (rango de fechas, cliente y, según el reporte, la forma de
agrupar), **Ver** muestra la tabla con un gráfico de barras y **Exportar PDF/CSV** genera el archivo, que queda
registrado (tabla `reporte`, estados PENDIENTE → EN_PROCESO → COMPLETADO o ERROR) en la carpeta `REPORTES_DIR`
(por defecto `reportes/`, fuera de git). **Abrir** vuelve a comprobar el permiso; **Generar de nuevo** rehace un
reporte con sus mismos parámetros (por ejemplo, desde otro equipo).

Hay 14 tipos: 5 operativos (envíos por país, estado y cliente; demorados; incidentes abiertos), los analíticos de la
consigna (tiempos máximo y mínimo por cliente, promedio de incidentes, tiempos promedio, rankings de rutas e
incidentes y de clientes por volumen, desempeño histórico, anomalías de temperatura, predicción) y uno comercial
(costos por tipo de transporte). Cada métrica sigue `AGENTS.md` §10.6; sin casos válidos se muestra `SIN_DATOS`,
que no es lo mismo que cero.

**Predicción de atrasos** (`FRECUENCIA_HISTORICA` 1.0): de las entregas de la misma ruta de los últimos 180 días
(mínimo 10; si no, todas las rutas, mínimo 20), qué fracción llegó después del compromiso. Con menos historia el
resultado es `SIN_DATOS`. Es una frecuencia histórica, **no** un modelo de aprendizaje automático ni una
probabilidad individual calibrada. El botón **Calcular predicciones de hoy** (usuarios internos) es idempotente:
repetirlo el mismo día no duplica.

**Quién ve qué:** Operaciones, los reportes operativos y analíticos; Comercial, el de costos; un cliente, solo los
habilitados y con sus datos; el operador, la predicción. Supuestos R1–R6 en `AGENTS.md` §16.

**Pruebas:** `tests/test_fase7_dominio.py` (cada reporte con datos chicos y el resultado calculado a mano),
`tests/test_fase7_reportes.py` (PostgreSQL real: resultados, permisos, alcance, registro, archivo faltante, error
al escribir y sesión revocada durante la generación) y `tests/test_fase7_ui.py` (Qt offscreen). **No se probó a mano
en la ventana.**

## Procesos periódicos (fase 8)

Cuatro tareas: **facturación** de las entregas confirmadas, **sensores sin comunicación**, **cierre mensual** de
cuentas y **predicción diaria** de atrasos. Las ejecuta cada instancia abierta por un usuario interno (al iniciar y
cada 60 s; el resultado se ve en la barra inferior: "Procesos: …", con el detalle al pasar el mouse) o, sin ventana:

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe scripts\procesos.py              # una vuelta: revisa todo lo pendiente
.\.venv\Scripts\python.exe scripts\procesos.py --continuo   # en bucle cada 60 s (Ctrl+C corta)
```

Varias instancias a la vez **no duplican nada**: cada tarea toma un advisory lock de PostgreSQL por tarea y
período (por ejemplo `proceso:CIERRE_MENSUAL:2026-10`) en una conexión dedicada que se libera siempre; la que no lo
consigue omite esa vuelta. Además cada efecto tiene su clave única (un cargo en una sola factura, un cierre por
cuenta y mes, una predicción por envío y día). Al arrancar, la primera vuelta factura lo que quedó pendiente y
cierra los meses terminados que falten; la predicción es solo la del día. Si solo hay clientes u operadores
conectados, nadie procesa (supuesto P1). Supuestos P1–P4 en `AGENTS.md` §16.

**Pruebas:** `tests/test_fase8_concurrencia.py` corre **dos planificadores reales a la vez** sobre una base
temporal (una instancia ejecuta y la otra omite; un solo cierre, una factura y una predicción) y la recuperación de
lo pendiente al iniciar; `tests/test_fase8_procesos.py` prueba el lock (incluido que una conexión que no pudo
soltarlo no vuelva al pool) y el planificador. **No se probó a mano en la ventana.**

## Datos de demostración (fase 9, en curso)

`scripts/seed_demo.py` (agente, 4/11/2026) siembra datos reproducibles pasando por los **servicios** (sesión
→ permiso → alcance → regla → transacción → auditoría), igual que la aplicación: 5 terceros (2 clientes, 2
operadores, 1 proveedor), 4 ubicaciones reales, 5 usuarios (uno por perfil: OPERACIONES, COMERCIAL, dos
CLIENTE de la misma empresa y un OPERADOR_LOGISTICO), vehículos y contenedores, **12 envíos ENTREGADOS en la
misma ruta** Buenos Aires–Rotterdam repartidos en los últimos ~170 días (3 con atraso, para que la
predicción de atrasos tenga la muestra mínima de la §10.5 y un resultado no trivial), un envío PLANIFICADO
en esa misma ruta (el "objetivo" de la predicción), un BORRADOR y un CANCELADO en otra ruta terrestre, un
incidente manual resuelto, facturación automática + 2 cobros (uno total, uno parcial) + una factura de
compra con su pago, la demo de monitoreo IoT ya probada de la fase 4 (`MonitoreoService.preparar_demo`) y 4
conversaciones de chat (privada interna, grupal, canal público y privada entre dos clientes).

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe scripts\seed_demo.py --email admin@empresa.com   # pide la clave sin eco
```

**Revisión del 4/11/2026 (tarde-noche):** una segunda pasada, pedida explícitamente antes de correrlo contra
la base real, encontró y corrigió varios problemas reales que la primera validación (un harness manual, no
repetible) no cubría:

- **Idempotencia real, no solo por prefijo.** Un prefijo (`DEMO-SEED`) identifica que un dato es de
  demostración, pero no alcanza para saber si ya existe: terceros/usuarios/vehículos/contenedores se
  reconocen por su identificación, email o código antes de crear. Para la ruta histórica (12 envíos sin un
  id elegible y con código aleatorio), cada ocurrencia se identifica por el número embebido en
  `descripcion_carga` (`"ocurrencia N/12"`). Cada conversación de chat también se reconoce por separado (no
  había un único "ya existe" para todo el chat).
- **Recuperación tras una ejecución parcial.** Si el script se interrumpe a mitad de la ruta histórica, el
  envío que quedó a mitad puede estar en BORRADOR/PLANIFICADO (se cancela y se reemplaza; S8 lo permite en
  esos estados) o en EN_TRANSITO (S8 ya no permite cancelarlo: en vez de abandonarlo, se **termina en el
  mismo lugar donde quedó**, usando su propia `salida_real_en` ya registrada, nunca una fecha recalculada).
  Antes de esta revisión, el chequeo de "¿ya sembré la ruta?" solo contaba ENTREGADOS: una interrupción
  parcial hacía que la siguiente corrida sembrara los 12 de nuevo (duplicando lo ya entregado).
- **Dos bugs de normalización que rompían la idempotencia silenciosamente:** `VehiculoDatos`/
  `ContenedorDatos`/`UsuarioDatos` normalizan (mayúsculas, sin espacios, minúsculas) antes de guardar;
  `asegurar_vehiculo`/`asegurar_contenedor`/`asegurar_usuario` comparaban contra el valor SIN normalizar, así
  que una segunda llamada con una variante de mayúsculas/minúsculas no encontraba el existente e intentaba
  crear un duplicado (fallaba con "vehículo duplicado"). Encontrado por
  `tests/test_fase9_seed_demo.py::test_asegurar_ubicacion_vehiculo_contenedor_son_idempotentes`.
- **La sincronización ahora se acota por envío (`agregado_id`), nunca por lote general.** Antes procesaba
  hasta 50 entregas pendientes de la cola SIN filtrar por envío: en una base con otros envíos reales con
  entregas pendientes (la de desarrollo HOY no tiene ninguna pendiente, pero podría tenerlas en el futuro),
  el script les habría tocado la cola. Ahora cada destino se procesa un envío a la vez, solo para los
  envíos que esta siembra creó.
- **La predicción de atrasos YA NO se calcula desde este script.** `PrediccionService.calcular()` es, por
  diseño, un proceso de alcance TODO EL SISTEMA (evalúa cualquier envío PLANIFICADO/EN_TRANSITO existente,
  no solo los de la siembra; es el mismo método que corre la fase 8 una vez al día). La base de desarrollo
  tiene HOY 3 envíos EN_TRANSITO reales del equipo (`ENV-20261103-GF5A8U`, `JWCP8R`, `R6Z65H`): dispararlo
  desde la siembra les habría agregado una predicción que nadie pidió (aditiva e idempotente, nunca
  modifica el envío, pero fuera del alcance de "solo crear datos de demostración"). Se calculará sola con
  una instancia interna abierta (cada 60 s) o con `scripts/procesos.py`.

**Por qué las corridas del harness anterior habían devuelto código de salida 1 pese a texto de éxito:** se
investigó en lugar de asumir. En reproducciones aisladas, ni `2>&1` con texto acentuado ni múltiples
`log.warning` a stderr alcanzaron para reproducir un `$LASTEXITCODE` distinto de 0; probablemente fue un
artefacto de estado acumulado en una sesión de PowerShell muy larga (con `NativeCommandError` de PowerShell
7.3+ al mezclar stderr con `2>&1`), no un fallo real del script. A partir de esta revisión, toda corrida de
validación comprueba `$LASTEXITCODE` explícitamente después de cada invocación, sin depender del texto
impreso ni del código que reporta la terminal.

**Pruebas permanentes:** el harness manual temporal se reemplazó por `tests/test_fase9_seed_demo.py` (15
pruebas), que corre contra PostgreSQL real dentro de una transacción que se revierte (`tx_connections`,
igual que el resto de la suite de integración) y contra Mongo/Neo4j reales con limpieza por `envio_id` al
terminar (mismo patrón que `tests/test_fase3_sincronizacion.py`) — nunca contra la base de desarrollo.
Cubre: primera corrida y repetición sin duplicados; recuperación de un huérfano BORRADOR (se cancela y
reemplaza) y de un huérfano EN_TRANSITO (se completa en el lugar donde quedó); conservación de un tercero
ajeno y de las entregas de outbox pendientes de otro envío (confirma que `agregado_id` aísla de verdad);
coherencia temporal dentro de un envío (creación ≤ salida ≤ llegada ≤ entrega, reserva cubre el uso real,
con y sin atraso); que Mongo y Neo4j reciben, por el flujo normal de sincronización, las fechas HISTÓRICAS
correctas (no las de hoy); que `_retrasar_creacion` solo afecta el envío indicado y nunca otro; e
idempotencia del chat. Se ejecutaron con `LOGISTICA_TEST_CONNECTIONS=1` y los tres motores: **15 aprobadas,
0 fallidas, código de salida 0** (verificado con `$LASTEXITCODE`, no deducido del texto). Se verificó antes
y después que no quedó nada en Mongo/Neo4j ni en PostgreSQL de desarrollo (mismos conteos y
`ENV-20261103-GHP2TD` sin cambios).

**Corrida real contra la base de desarrollo (4/11/2026):** el equipo ejecutó `scripts/seed_demo.py` contra
la base real con código de salida 0. Se confirmó por consulta directa (sin modificar nada): `tercero`=11,
`usuario`=6, `envio`=20, `factura`=13, `pago`=3, `conversacion`=4, `cargo_envio`=12, `incidente`=4, y
`ENV-20261103-GHP2TD` sigue `ENTREGADO` con sus mismas fechas.

## Reconciliación de diagnóstico (fase 9, cierre)

`services/reconciliacion_service.py` + `repositories/postgres/reconciliacion.py` + `domain/reconciliacion.py`
comparan, **sin reparar nada**, los hechos de PostgreSQL contra Neo4j (el subgrafo de cada envío reciente) y
Mongo (sus eventos), y revisan sobreaplicaciones de pagos/facturas. Un motor caído se informa como "no
disponible" para esa corrida (nunca se asume consistencia ni se interpreta como éxito). Se integró como una
tarea más del planificador de la fase 8 (`Tarea.RECONCILIACION`, diaria, mismo advisory lock que las demás) y
también puede correrse sola:

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe scripts\reconciliar.py
```

**Pruebas:** `tests/test_fase9_reconciliacion.py` (10, PostgreSQL en transacción revertida + Mongo/Neo4j
reales con limpieza por envío): detecta un envío sin proyección en Neo4j, un envío marcado `COMPLETADA` en
Mongo sin que el documento exista, un pago sobreaplicado (forzado con una inserción SQL directa, ya que los
servicios normales lo impiden con bloqueos), que un motor caído no rompe la corrida, y que un envío
sincronizado de punta a punta no genera ninguna discrepancia propia (aunque la base tenga otros datos). Al
escribirlas se encontraron y corrigieron 2 problemas reales no relacionados con la lógica de reconciliación:
un import directo de `sqlalchemy` en el servicio (violaba la regla de arquitectura "servicios no importan
drivers", detectada por `test_fase0_cimientos.py`) y 3 pruebas de `test_fase6_ui.py` que asumían que la
pestaña Comercial (que un interno ve sin filtro de tercero) tenía una sola fila — dejó de ser cierto al
crecer la base con la siembra; se reescribieron para ubicar su propia fila por un dato único, no por
posición. **Todavía sin prueba manual del grupo.**

## Ejecutable empaquetado (fase 9, cierre)

```powershell
python scripts\empaquetar.py             # --onedir (carpeta), o --onefile
```

Compila con PyInstaller (agregado a `requirements-dev.txt`) y bundlea el único recurso no-Python del
proyecto (el mapa base GPS). **Las bases de datos no se empaquetan**: siguen siendo PostgreSQL/MongoDB/Neo4j
externos, con un `.env` junto al ejecutable (o en el directorio de trabajo).

**Verificado en este cierre:** `python scripts/empaquetar.py --limpio` compiló sin errores y generó
`dist/PlataformaLogistica/PlataformaLogistica.exe` (15,4 MB) + `_internal/` con el GeoJSON del mapa. Se
lanzó dos veces: sin `.env` junto al ejecutable, cierra en segundos con código 2 y sin ventana (falta una
variable de entorno obligatoria — comportamiento esperado, no un crash); con una copia temporal del `.env`
real (borrada después de la prueba, nunca commiteada), abrió la ventana "Iniciar sesión · Plataforma de
logística" conectada a los tres motores reales. No se inició sesión ni se tocó ningún dato: solo se confirmó
que arranca. **No se probó el recorrido completo de pantallas sobre el ejecutable** (solo el arranque).


**Todavía no se corrió contra la base de desarrollo real** (requiere la contraseña del administrador
existente, que el agente no debe conocer ni pedir) ni se probó a mano en la ventana. Para correrlo, ver la
sección de comandos al final de este documento.


## Probar monitoreo desde la ventana (fase 4)

1. Abrí la aplicación con el entorno existente, **sin volver a crear el administrador ni ejecutar resets**:
   ```powershell
   $env:PYTHONPATH="src"
   .\.venv\Scripts\python.exe -m logistica.main
   ```
2. Entrá como **Administrador** → **Monitoreo** → **Crear demo independiente**.
   Escribí `DEMO` y elegí un escenario:
   - **Temperatura:** abre a 120 s de anomalía y escala a ALTA con incidente a 300 s; queda abierta.
   - **Recuperacion:** incluye la anomalía anterior y 120 s en rango; cierra la alerta, no el incidente.
   - **Desconexion:** supera 180/600 s sin contacto y luego recupera comunicación.
   - **Completo:** combina temperatura, recuperación, desconexión y recuperación final.
   - **Normal:** dos lecturas normales separadas por 60 s.
3. La demo crea **empresa, ubicaciones, vehículo, contenedor, sensor y envío NUEVOS** con prefijo
   `DEMO-MON-…`. Queda seleccionada automáticamente. No usa el envío entregado ni recursos/reservas previas.
   Usa un reloj **SIMULADOR explícito**, sin esperas, para reproducir un episodio pasado cuyo último punto
   termina cerca del presente. Salida, mediciones y recepción de ese episodio son datos de demostración;
   las estimaciones quedan futuras. No es telemetría real ni evidencia de daño.
4. En **Sensores y últimas mediciones**, revisá estado administrativo, contacto calculado, alertas y
   últimas 20 lecturas. En **Alertas**, seleccioná una abierta para **Reconocer**, **Cerrar con motivo**
   o **Crear incidente desde alerta**. La demo muestra también cerradas; podés activar **Solo activas**.
5. En **Incidentes**, seleccioná uno → **Tomar a mi cargo** → **Resolver con explicación**.
   También se puede crear uno manual sobre un envío planificado/en tránsito/entregado.
   Todas estas acciones revalidan sesión, permisos, alcance y transiciones, y se auditan.
6. En **Historial**, elegí el envío → **Filtrar** → **Siguiente/Anterior** (50 por página).
   Desde/hasta son opcionales, ISO con zona; hasta es exclusivo, por ejemplo
   `2026-11-03T18:00:00-03:00`. Los internos también pueden filtrar por UUID de sensor o contenedor.
7. En **Mapa GPS**, mové con arrastre y acercá con la rueda. Se muestran hasta las últimas 2000 posiciones,
   con una línea por sensor, marcas con fecha/coordenadas al pasar el mouse y referencias geográficas.
   La línea une mediciones: **no calcula una ruta vial**.
8. Para un envío propio **BORRADOR/PLANIFICADO**, seleccioná **Condiciones antes de salir**.
   Vacío significa variable no controlada; sin condiciones solo se controla comunicación (180 s).
   Una vez iniciado, los umbrales se muestran pero no se editan.

**Pruebas manuales informadas por el grupo:** probaron las demos de temperatura, recuperación y desconexión,
el historial de lecturas y el mapa GPS, además de gestionar y resolver un incidente. Informaron que la
recuperación cierra automáticamente la alerta de temperatura pero no el incidente, y que recuperar el
contacto cierra automáticamente la alerta `SIN_COMUNICACION`. No se atribuyen otras pruebas manuales.

**Perfiles:** Administración/Operaciones consultan todo; Cliente solo sus envíos; Operador solo lecturas/GPS,
alertas e incidentes de sus tramos (estado actual únicamente mientras opera el tramo en curso).
Operador puede reconocer, no cerrar ni resolver. Comercial no ve Monitoreo.
Las credenciales técnicas compartidas **no equivalen a seguridad por fila en las bases**.

**Procesamiento:** al abrir una instancia de Administración/Operaciones y cada 60 s, un trabajador evalúa
hasta 20 lecturas y revisa comunicación. No trabaja en el hilo gráfico ni solapa vueltas de esa instancia.
Los reclamos Mongo, marcas PostgreSQL y locks por sensor coordinan las instancias; se relee contacto después
del lock. Al cerrar se detiene el timer y se cancela cooperativamente entre lecturas/sensores, usando el
cierre de trabajadores existente. Errores/reintentos se muestran abajo y se registran en logs.
Sin una instancia habilitada o simulador activo **no hay procesamiento**. Externos refrescan con **Actualizar**.
La demo no sigue emitiendo sola: si queda en tránsito, posteriormente puede disparar falta de comunicación.

## Probar trazabilidad desde la ventana (fase 5)

1. Abrí la aplicación con el entorno existente, **sin crear administradores, ejecutar resets ni modificar
   los datos manuales**:
   ```powershell
   $env:PYTHONPATH="src"
   .\.venv\Scripts\python.exe -m logistica.main
   ```
2. Entrá como **Administrador** u **Operaciones** → **Trazabilidad** → **Actualizar envíos** y elegí un envío.
   En **Línea de tiempo**, consultá eventos, probá filtros de tipo, tramo, fechas ISO con zona (hasta exclusivo)
   y descripción, y avanzá/retrocedé con la paginación. Los eventos provienen solo de MongoDB. El resumen de
   outbox muestra pendientes/fallidos aparte; pendientes que aún no llegaron a Mongo no se inventan como hechos.
3. En **Ruta del envío**, consultá la ruta proyectada. Revisá orden, origen/destino, medio, operador, versión
   operativa/proyectada y el estado de Neo4j. Una proyección atrasada o un Neo4j no disponible se informa
   explícitamente y no se reemplaza por una ruta calculada desde PostgreSQL.
4. En **Contenedor**, ingresá su código internacional y consultá asignaciones. Seleccioná una fila para ver
   los tramos relacionados; reserva y uso delimitan los períodos. **Cargar GPS medido del uso** consulta
   posiciones por separado y solo dentro del período real de uso.
5. En **Red**, podés filtrar por UUID de cliente, operador o envío; el límite seleccionable es 25, 50 o 100.
   Se muestran tablas y conexiones nativas Qt de cliente–envío–operador. En el gráfico, la rueda desplaza,
   **Ctrl+rueda** acerca/aleja (0,5× a 2,5×) y arrastrar mueve la escena; no se reduce automáticamente para
   encajar redes grandes. Para repetir el caso informado, dejá los filtros vacíos y buscá el envío
   `ENV-20261103-GHP2TD` entre los cuatro resultados; confirmá que el cliente diga
   **Exportadora Demo SA** y que se lean los nombres/códigos del gráfico. Probá también desplazar y acercar
   la escena. Los perfiles externos solo consultan sus envíos/tramos; esta instrucción no implica que esos
   permisos se hayan probado manualmente.
6. La decisión D6 se presenta explícitamente: relaciones, tramos y versiones se leen de Neo4j; nombres
   actuales se resuelven por ID desde PostgreSQL. Un cambio de catálogo puede dejar los atributos almacenados
   del grafo antiguos para otros consumidores hasta una proyección posterior; esta consulta no reconstruye
   globalmente el grafo.

**Arreglo de Red:** la consulta ya obtenía los nombres actuales, pero el servicio asignaba el mapa de
ubicaciones (vacío en esa consulta) al nombre del cliente. El repositorio devuelve primero ubicaciones y
después terceros; ahora Red usa el mapa de terceros para ambos roles, incluso si una empresa es cliente y
operador. El gráfico conserva escala legible, muestra etiquetas completas y permite desplazamiento y zoom
acotado sin ajustar automáticamente toda la escena.

### Simulador por consola

La clave se pide sin eco; no se pasa por argumentos ni se cambia el administrador existente.
Cada preparación crea otra demo; reenviar el mismo episodio conserva los UUID y no duplica efectos.

```powershell
.\.venv\Scripts\python.exe scripts\simulador_iot.py --preparar-demo --email usuario@empresa.com --escenario completo
.\.venv\Scripts\python.exe scripts\simulador_iot.py --envio-id UUID_DE_LA_DEMO --email usuario@empresa.com --escenario completo --reloj-simulado
.\.venv\Scripts\python.exe scripts\simulador_iot.py --envio-id UUID_DE_LA_DEMO --email usuario@empresa.com --escenario normal --continuo
```

`--continuo` emite en tiempo real (normal: cada 60 s), informa el inicio de cada episodio y se corta con Ctrl+C;
`--inicio ISO_CON_ZONA` permite reenviar ese episodio. Sin `--continuo` ni `--inicio`, el episodio parte de la
salida original. Sin reloj simulado, las lecturas antiguas se reciben tarde y quedan históricas, no actualizan
el estado. Solo admite viajes `DEMO-MON` en tránsito con su sensor exclusivo. No reabre viajes entregados.
Una falla de preparación deja visibles los pasos ya confirmados con su prefijo, informa el error y no borra
ni compensa datos; no hay una transacción global.

### Mapa local y licencia

Se distribuye `src/logistica/ui/recursos/tierra_110m.geojson`, **Natural Earth 5.1.2**, tierra/costas a escala
1:110 millones, de **dominio público**. Qt lo dibuja con `QGraphicsView/QPainterPath` y proyección
equirectangular lon/lat WGS84. No usa HTTP, navegador, API, WebSocket ni servicios cartográficos.
No requiere dependencias nuevas. Su baja resolución no sirve para navegación ni mapas de calles.

- [Fuente versionada del GeoJSON](https://github.com/nvkelso/natural-earth-vector/blob/v5.1.2/geojson/ne_110m_land.geojson).
- [Términos de dominio público](https://www.naturalearthdata.com/about/terms-of-use/).
- [Aviso local](src/logistica/ui/recursos/LICENCIA_NATURAL_EARTH.txt).

### Verificación de trazabilidad y Red

En el corte previo de Red, `tests/test_fase5_trazabilidad.py`: **11 aprobadas** contra PostgreSQL, MongoDB, Neo4j y
Qt. La suite completa con `LOGISTICA_TEST_CONNECTIONS=1` y `LOGISTICA_TEST_PG` armado en memoria desde el
`.env` local: **410 aprobadas, 0 fallidas, 0 omitidas**, en 193,35 s. Incluye regresión de nombres actuales
de cliente y operador, una misma empresa con ambos roles, y render Qt de 4 y 100 relaciones; la red de 100
confirma escala inicial 1:1, texto completo, scroll y modo de arrastre. El zoom Ctrl+rueda también se ejercita.
Estas son pruebas automatizadas, no una nueva prueba manual del grupo.

**Prueba manual comunicada por el grupo antes de este arreglo:** línea de tiempo del envío entregado; ruta
ACTUALIZADA con versiones 8/8; asignación del contenedor y su tramo; consulta GPS sin posiciones (esperable);
en Red se obtuvieron cuatro envíos con Neo4j ACTUALIZADA y se observaron los dos defectos corregidos aquí.
**Actualización al comenzar comunicación:** el grupo sí repitió Red: aparece **Exportadora Demo SA**,
el gráfico es legible y funciona **Ctrl+rueda**. Esa repetición no extiende la validación a trazabilidad
completa ni a permisos externos.
No se reinició ninguna base ni se modificaron administrador, envío entregado, fechas o demos.

No se detuvieron los motores físicos para probar caídas; la ruta sin Neo4j se prueba inyectando el error en
la frontera del repositorio y verificando recuperación.

Limitaciones/preguntas de negocio: las lecturas `ERROR` se conservan para revisión y **no tienen acción de
reproceso desde la UI**; falta acordar quién puede autorizarla. Confirmar también la política ya existente
para incidentes manuales sin alerta. En trazabilidad, página de eventos hasta 100, ruta hasta 200 tramos,
recorrido de contenedor hasta 100 asignaciones y 200 posiciones GPS por asignación; la red limita a 500
envíos candidatos y 100 relaciones. Los nombres de catálogo se resuelven desde PostgreSQL según D6; otros
consumidores del grafo pueden conservar nombres antiguos hasta otra proyección.

## Chat: decisiones y prueba manual en dos ventanas (fase 5)

No hace falta migrar el esquema: se reutilizan `conversacion`, `conversacion_miembro`, el validador de
`mensajes` y `ix_mensaje_conversacion_fecha_id`, verificados en las bases existentes. La aplicación conserva
`ui → services → repositories` y conexiones directas; no hay HTTP, WebSocket ni API.

### Quién puede acceder

Decisiones confirmadas por el grupo para este corte:

- **Privado:** dos usuarios diferentes de la misma empresa, o dos internos. Cualquiera de ellos puede
  iniciarlo; no se pueden agregar terceros participantes. El administrador de la aplicación no puede
  leer privados ajenos.
- **Grupal:** sólo internos. Crear grupos/canales requiere `conversaciones.crear`
  (Administración/Operaciones); administrar miembros o archivar exige además pertenecer a esa conversación
  como `ADMINISTRADOR` vigente. No existe un privilegio global de lectura o gestión.
- **Público:** canal **interno**, no público entre empresas ni en Internet. Un interno ve su nombre y
  elige **Incorporarme al canal interno** antes de leer/enviar. Un externo no lo descubre ni ingresa.
  Tras cerrar una membresía, sólo un administrador miembro autorizado puede reincorporarla;
  el botón de incorporación propia no permite eludir la revocación.
- Leer/enviar exige sesión válida, usuario y empresa habilitados, roles efectivos, pertenencia vigente y
  conversación no archivada. El historial incluye sólo los períodos propios **[ingreso, salida)**,
  nunca lo anterior al ingreso ni los intervalos sin membresía. Un revocado tampoco consulta su histórico
  anterior hasta reincorporarse. No se borran mensajes ni períodos.

### Preparar usuarios desde la UI, sin alterar cuentas existentes

1. Entrá con el **administrador existente** → **Usuarios → Nuevo**.
   Creá **Chat Uno** y **Chat Dos** con emails distintos de demostración, empresa
   **(usuario interno)** y rol **OPERACIONES**. Elegí contraseñas nuevas de al menos 10 caracteres;
   no las escribas en documentos ni uses **Resetear contraseña** sobre cuentas existentes.
2. Para la prueba de usuario ajeno, creá **Chat Ajeno**, interno, rol **COMERCIAL**.
   Para comprobar empresas, opcionalmente creá desde **Terceros → Nuevo** dos terceros de prueba
   `DEMO-CHAT Empresa A/B`, con identificaciones distintas y rol CLIENTE; luego dos usuarios CLIENTE
   asociados a A y uno asociado a B. No cambies empresas de usuarios existentes ni uses los recursos
   de monitoreo/logística como datos de prueba.
3. Cerrá la sesión administrativa. Abrí **dos terminales PowerShell** en este proyecto y ejecutá en cada una:

   ```powershell
   $env:PYTHONPATH="src"
   .\.venv\Scripts\python.exe -m logistica.main
   ```

   Ingresá como **Chat Uno** en A y **Chat Dos** en B. Son procesos y sesiones separados.
   Nunca reutilices la sesión administrativa para simular al segundo participante.

### Intercambiar mensajes y probar acceso

1. **Privado:** en A, **Chat → PRIVADA → Crear conversación**, elegí **Chat Dos** y aceptá.
   Seleccioná la conversación en A; en B pulsá **Actualizar** y seleccionala. Escribí en el compositor
   y pulsá **Enviar / reintentar**, luego respondé desde B. Verificá remitentes distintos, mensajes
   sin duplicados y **Persistencia confirmada: UUID**.
2. **Grupo:** desde A elegí **GRUPAL → Crear conversación**, nombre `DEMO-CHAT Grupo`.
   Seleccionalo y usá **Incorporar participante → Chat Dos → MIEMBRO**.
   Actualizá/seleccioná el grupo en B y respondé. La tabla muestra roles e ingreso/salida.
   Podés designar `ADMINISTRADOR` al incorporar a otro interno; sólo administra si también tiene
   `conversaciones.crear`. No se permite retirar por sí mismo al último administrador: puede archivar.
3. **Público:** desde A creá **PUBLICA**, nombre `DEMO-CHAT Canal interno`, y seleccioná el canal.
   Enviá un texto **antes** del ingreso de B. En B, **Actualizar** → selector de canales públicos →
   elegí ese canal → **Incorporarme al canal interno**. Seleccionalo en conversaciones.
   El texto anterior no debe aparecer; los posteriores sí. Manteniendo Chat visible, las novedades
   llegan por polling cada **3 s**, no por una conexión web.
4. **Revocación:** en A seleccioná la fila vigente de Chat Dos → **Revocar participante**.
   En B, al actualizar o en el siguiente polling se borran los resultados y se rechazan lectura/envío.
   En el público, **Incorporarme** debe rechazar el reingreso. A puede usar **Incorporar participante**
   para reincorporar a Chat Dos: recupera mensajes de sus períodos previos y del nuevo, no del intervalo
   revocado. Archivar un grupo/canal impide lectura y envío para todos sus miembros.
5. **Ajeno y externos:** cerrá sesión en B e ingresá como Chat Ajeno. No debe listar ni leer privados/grupos
   de Uno/Dos; puede descubrir el canal público interno, pero sólo leer mensajes posteriores a su ingreso.
   Con los CLIENTE de prueba, los dos de A pueden conversar en privado; el de B no figura entre sus
   destinatarios y ninguno descubre canales internos ni crea/ingresa en grupos. El administrador tampoco
   lista privados o grupos ajenos por su rol.
6. **Sesión/usuario:** para probar sin tocar identidades reales, cerrá B e ingresá allí con el administrador
   existente; desde **Sesiones** revocá la sesión de Chat Uno abierta en A, o desde **Usuarios**
   desactivá sólo Chat Uno. A debe volver al login al consultar y descartar respuestas pendientes.
   Reactivá únicamente ese usuario de prueba si lo necesitás después; no resetees su contraseña.
7. **Histórico y cierre:** con más de 50 mensajes en una conversación nueva, usá **Más antiguos /
   Más recientes**. Las páginas antiguas mantienen el corte aunque llegue un mensaje nuevo; volver a la
   primera página trae novedades. Cerrá ambas ventanas mientras actualizan y comprobá que no se congelen.
   Estos pasos son un **guion pendiente**, no resultados manuales atribuidos al grupo.

### Persistencia, errores y límites

El remitente sale de la sesión, nunca de un campo editable. Fechas UTC, presentación local; texto de
1–4000 caracteres no blancos. Mongo usa UUID estándar y escritura confirmada `majority`; el mismo UUID
conserva fecha/texto en reintentos y se rechaza si se intenta reutilizar con otro mensaje.
Histórico por `(fecha_envio, _id)` descendente, 50 por página (máximo del servicio: 100), mostrado
cronológicamente dentro de la página. Los límites de membresía se ajustan a milisegundos BSON.

**No hay atomicidad distribuida.** PostgreSQL confirma creación/participantes/auditoría juntos. El envío
serializa contra modificaciones de participantes; Mongo confirma el documento y después PostgreSQL registra
la confirmación observada. Si falla Mongo o la confirmación posterior, la UI dice **Envío NO confirmado**,
conserva el borrador/UUID y permite **Enviar / reintentar**. Puede existir ya el documento: no lo dupliques
creando otro borrador. Una falla de auditoría PostgreSQL posterior a Mongo no se oculta ni borra el mensaje;
el reintento vuelve a validar acceso y completa la confirmación. No hay reconciliador general nuevo.

Al consultar se libera el lock antes de Mongo y se revalidan sesión, permisos, alcance y períodos antes de
devolver resultados; una revocación durante la consulta los descarta. Un error limpia mensajes/participantes
visibles; al recuperar el motor se puede actualizar y seleccionar la conversación nuevamente.
Las credenciales técnicas compartidas no equivalen a seguridad por fila en las bases.

Polling sólo con Chat visible, fuera del hilo gráfico y sin consultas solapadas por vista. Al cerrar se
detiene el timer, se descartan respuestas y se espera al trabajador por timer antes de destruir la ventana
o volver al login. Presupuesto Mongo de 5 s y espera de lock PostgreSQL de 5 s; polling de 3 s: valores
iniciales pendientes de medición en la demostración, no parámetros de un scheduler general.
El borrador/UUID vive **sólo en memoria**: cerrar la ventana o un fallo del proceso lo pierde.
**Descartar borrador / confirmación pendiente** no borra un documento posiblemente persistido.
Sin adjuntos, notificaciones externas ni funciones de fases 6–9.

### Pruebas reales de este corte

- Línea de base antes de editar: **410 aprobadas en 199,09 s**.
- Suite final: **449 aprobadas, 0 fallidas, 0 omitidas**, salida 0, **209,49 s**.
  Incluye 39 nuevas pruebas en `tests/test_fase5_comunicacion.py` y `tests/test_fase5_chat_ui.py`.
  Después del ajuste final de formato, esas **39 pruebas** se repitieron: **39 aprobadas en 46,11 s**.
- Se ejecutó `python -m pytest tests -q --tb=short` con `LOGISTICA_TEST_CONNECTIONS=1`,
  `QT_QPA_PLATFORM=offscreen` y `LOGISTICA_TEST_PG` construido en memoria desde el `.env`, sin imprimir claves.
  Para repetir sólo comunicación:

  ```powershell
  $env:LOGISTICA_TEST_CONNECTIONS="1"
  $env:QT_QPA_PLATFORM="offscreen"
  .\.venv\Scripts\python.exe -m pytest tests\test_fase5_comunicacion.py tests\test_fase5_chat_ui.py -q
  ```

- Casos: remitentes distintos; privados/grupos; público sólo interno e incorporación explícita;
  reingreso revocado sólo por administrador miembro; ajenos/externos; sesión vencida/revocada y usuario
  desactivado; permiso de gestión perdido; períodos históricos; reintentos sin duplicar; fallo antes/después
  de Mongo o en auditoría; fechas iguales y mensajes nuevos durante paginación; polling sin solapamiento;
  Qt receptivo y cierre diferido; descarte tras revocación desde otra conexión.
- Se probó intercambio real desde **dos procesos de aplicación PySide6**, con ventanas principales,
  identidades, `instancia_id`, sesiones y conexiones propias. También se automatizaron formularios y
  mensajes en dos ventanas. Todo fue **automático/offscreen**, no una prueba manual del grupo.
- PostgreSQL de servicios usa transacciones revertidas. Las pruebas de ventanas/procesos crean bases
  PostgreSQL/Mongo nuevas con nombre UUID, aplican sus esquemas y eliminan sólo esas bases nuevas.
  Requieren que la identidad de administración de pruebas pueda crear bases; usan `PG_APP_USER` para DML.
  No se crearon cuentas demo persistentes en las bases existentes ni se modificaron claves existentes.
- Mongo no disponible se prueba con un puerto local reservado sin servidor y recuperación posterior;
  otras fallas se inyectan entre motores. **No se detuvieron contenedores compartidos**. Se preservan
  administrador, demos, `ENV-20261103-GHP2TD` ENTREGADO y sus fechas; sin resets, particionado, commits ni push.

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
