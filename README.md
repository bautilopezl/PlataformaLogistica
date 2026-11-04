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
scripts/       setup_databases.py
src/logistica/ core (config, logging sin secretos, errores) · domain
               repositories (base.py: traducción de errores · postgres/ · mongo/ · neo4j/ · connections.py)
               services (health_service.py) · ui (worker.py · theme.py · main_window.py) · main.py
tests/         test_postgres_integrity.py (37) · test_particionamiento_postgres.py (3)
               test_static_artifacts.py (6) · test_fase0_cimientos.py (43)
               test_fase1_dominio.py (28) · test_fase1_acceso.py (52) · test_fase1_ui.py (23)
               test_fase2_dominio.py (35) · test_fase2_logistica.py (34) · test_fase2_revision.py (16)
               test_fase2_ui.py (9) · test_fase2_concurrencia.py (15)
               test_fase3_dominio.py (3) · test_fase3_sincronizacion.py (18)
               test_fase5_trazabilidad.py (11, requiere los tres motores)
               test_fase5_comunicacion.py (27) · test_fase5_chat_ui.py (12)
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

Pendiente: prueba manual del grupo del chat y fases **6 a 9**, que no se implementaron en este corte.
Estado al 4/11/2026: **558 pruebas en verde** con los tres motores (suite completa, salida 0), incluidas las 60 de la fase 6 y las 49 de la fase 7.

**Decisión del grupo para la fase 6 (4/11/2026):** la facturación automática se genera **al confirmar la
entrega de cada envío**: una factura por envío entregado, con sus cargos facturables. Siguen abiertas la
numeración de comprobantes, las anulaciones y la moneda por defecto (`AGENTS.md` §16.3).

## Comercial: facturación, pagos y cuentas (fase 6)

**Circuito.** Comercial carga los **cargos** de un envío (lo que se le cobra al cliente; no es el costo del tramo).
Operaciones confirma la entrega y, segundos después, la instancia interna abierta (o `scripts/sincronizar.py
--continuo`) emite **una factura de venta por moneda** con los cargos pendientes. La factura, sus renglones, el
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

**Prueba manual sugerida (pendiente del grupo):** entrar como Comercial y cargar un cargo a un envío en tránsito;
como Operaciones, confirmar la entrega; volver a **Comercial → Facturas** y ver la factura `FV-…`; registrar un
cobro parcial, aplicarlo y ver el saldo en **Cuentas corrientes**; anular una factura con y sin refacturación.

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
