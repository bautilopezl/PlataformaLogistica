# Informe final — Plataforma de logística internacional (persistencia políglota)

UADE · Ingeniería de Datos II · Trabajo Práctico "Persistencia Políglota" · 2026
Integrantes: Margets Lucas (legajo 1171541) · Lopez Lopez Bautista (legajo 1172886)
Profesor: Godio Claudio Jose

> **Fuente de verdad.** Este informe sintetiza para la defensa lo que está versionado en el repositorio:
> `AGENTS.md` (guía de trabajo local del equipo con supuestos y estado fase por fase — **documento interno,
> no se entrega**: está excluido de git a propósito, ver `docs/entrega-reproducible.md` §1.1) y
> [`README.md`](../README.md) (instrucciones operativas, manual de uso por pantalla, resultados de pruebas,
> **sí entregado**). Las decisiones y supuestos que importan para evaluar el trabajo están reproducidos acá
> mismo, no solo referenciados, precisamente porque `AGENTS.md` no viaja con la entrega. **Material de la
> consigna no disponible en este repositorio:** el enunciado original (`TP_Persistencia_Poliglota.pdf`) y el
> informe de la Entrega 1 (`Informe_Entrega_1_UADE.docx`) no están versionados acá ni se encontraron en esta
> máquina (son documentos de la cátedra/entrega que el grupo conserva aparte); este informe no puede
> citarlos textualmente ni contrastar sus criterios punto por punto — queda pendiente que el grupo lo haga
> con su copia antes de la defensa.

## 1. Alcance y estado real (no solo "pruebas en verde")

Tener pruebas automáticas en verde **no** equivale a una funcionalidad terminada: la consigna exige que
funcione de punta a punta desde la UI, con datos de demostración. El resumen honesto, por área:

| Área | Servicio + UI | Pruebas automáticas | Datos de demostración | Prueba manual del equipo |
|---|---|---|---|---|
| Acceso (terceros, usuarios, sesiones, auditoría) | Sí | Sí | Sí (siembra) | Creación manual de terceros informada; el resto no |
| Logística (envíos, tramos, catálogos) | Sí | Sí | Sí (siembra) | Circuito completo informado (fase 2) |
| Monitoreo IoT (lecturas, alertas, incidentes, GPS) | Sí | Sí | Sí (siembra + demo de fase 4) | Informada (temperatura, recuperación, desconexión, historial, mapa, incidente) |
| Trazabilidad (línea de tiempo, ruta, Red) | Sí | Sí | Sí (siembra) | Informada sobre el envío entregado y la Red |
| Chat (privado/grupal/público) | Sí | Sí | Sí (siembra) | **No comunicada** |
| Comercial (cargos, facturación, pagos, cuentas, cierres) | Sí | Sí | Sí (siembra) | Verificación visual puntual de una pantalla (factura vista en **Comercial → Facturas**); circuito completo **no comunicado** |
| Reportes y predicción de atrasos | Sí | Sí | Sí (siembra) | **No comunicada** |
| Procesos periódicos (facturación, sensores, cierre, predicción) | Sí | Sí | Sí (corre sola con la ventana abierta) | **No comunicada** |
| Reconciliación (diagnóstico entre motores y comercial) | Sí | Sí | — (es un proceso técnico, no genera datos propios) | **No comunicada** |

"Prueba manual del equipo" significa, exactamente, lo que el grupo mismo reportó haber hecho en la ventana
y quedó así documentado en el README — **nunca** se da por probada a mano una pantalla que nadie del grupo
recorrió. Las filas marcadas "No comunicada" tienen automatización completa y datos de demostración, pero
siguen pendientes de un recorrido manual por el grupo antes de la defensa (ver §7, "Limitaciones").

### 1.1 Requisitos pendientes reales (no opcionales)

1. **Prueba manual del grupo** sobre chat, comercial (circuito completo: cargo → confirmación → factura →
   cobro → cierre), reportes/predicción y procesos periódicos (ver tabla). No es que falte funcionalidad: es
   que nadie del grupo las recorrió todavía de punta a punta en la ventana. La única verificación visual
   comunicada hasta ahora en Comercial es puntual (ver §7, "Manuales") y no reemplaza este recorrido.
2. **Instancia de entrega confirmada.** La fecha "4/11/2026" quedó anotada como confirmada por el grupo para
   "una entrega", sin precisar si se refiere a la Entrega 1 (ya hecha, informe de las secciones 1–2) o a la
   entrega final; confirmarlo con la cátedra antes de la defensa.

### 1.2 Mejoras opcionales / decisiones de diseño abiertas (no bloquean la entrega salvo que la cátedra lo exija)

- **Retención de la outbox:** hoy `outbox_evento`/`outbox_entrega` no se borran nunca (la identidad de
  ejecución ni siquiera tiene permiso `DELETE` sobre `outbox_evento`); es una decisión de diseño del grupo
  todavía sin cerrar, no un requisito identificado en la consigna disponible. Se documenta como tal: si la
  cátedra exige explícitamente una política de purga/archivado, pasaría a ser un requisito real.
- Particionamiento activado en un clúster real (hoy solo probado con conexiones simuladas al sharding).
- Carpeta compartida de reportes en una instalación multi-equipo (hoy, carpeta local + regeneración).
- Medir en el entorno real los parámetros "a ojo" (polling, tamaño de pool, costo de argon2) con carga real.

## 2. Arquitectura

```
ui (PySide6)  →  services  →  repositories  →  drivers (SQLAlchemy | PyMongo | neo4j)
   core: config · logging · errores · seguridad (transversal)      domain: entidades y reglas puras
```

- **Aplicación de escritorio Python + PySide6, con conexión directa** a los tres motores. Sin servidor de
  aplicación, sin API REST, sin WebSocket, sin Tauri (restricción confirmada por el profesor el 20/10/2026).
- **UI → services → repositories → driver**, en una sola dirección: la UI nunca ejecuta SQL/Cypher/consultas
  Mongo ni importa un repositorio o un driver. Los repositorios traducen los errores de cada driver a una
  jerarquía propia (`core/errors.py`) para que servicios y UI nunca vean una excepción de psycopg, pymongo o
  neo4j directamente.
- **Hilos**: el hilo gráfico nunca espera una consulta. `ui/worker.py` corre cada llamada a un servicio en
  `QThreadPool` y devuelve resultado/error por señales; cada trabajador abre su propia sesión de PostgreSQL
  (nunca se comparte una `Session` entre hilos). Un `QTimer` dispara la sincronización (10 s), el ciclo de
  monitoreo (60 s) y el planificador de procesos periódicos (60 s), pero su callback jamás bloquea: encola
  trabajo al pool.
- **Patrones usados**, donde aportan (sin sobre-ingeniería): Repository (un repositorio por motor), Service
  (una transacción de PostgreSQL por operación, con sesión→permiso→alcance→regla→cambio→auditoría), DTO
  (los repositorios nunca devuelven filas/documentos/nodos crudos a los servicios), Strategy (reglas de
  alerta por condición en `domain/monitoreo.py`, método de predicción en `domain/analitica.py`, formato de
  reporte PDF/CSV en `repositories/archivos.py`), Observer (señales Qt), Outbox (sincronización entre
  motores, `repositories/postgres/outbox.py`).

Composición de servicios: `services/contenedor.py::crear_servicios` arma los ~17 servicios de la aplicación
a partir de la configuración — es la única "raíz de composición"; ni la UI ni los servicios crean
repositorios o conexiones por su cuenta.

## 3. Modelos físicos de las tres bases

La fuente de verdad son los scripts versionados; este informe no los duplica, los resume. **Diagramas**
(ER por módulo de PostgreSQL, modelo de documentos de Mongo, modelo de grafo de Neo4j): no había ninguno
versionado en el repositorio como imagen o archivo de diagrama — los que sí se dibujaron viven únicamente en
`Informe_Entrega_1_UADE.docx` (no disponible, ver nota al inicio). Se generaron de nuevo, derivados
directamente de los scripts actuales sin tocar ninguna base, en
[`docs/diagramas.md`](./diagramas.md).

### 3.1 PostgreSQL (relacional) — `db/postgres/*.sql`, 38 tablas

5 grupos: **acceso** (9 tablas: tercero, usuario, roles, permisos, sesión, auditoría inmutable), **logística**
(6: ubicación, vehículo, contenedor, envío, tramo, envío_contenedor), **monitoreo** (9: sensor, capacidades,
instalaciones, condiciones, estado, alertas, incidentes, lectura_procesada), **comercial** (8: cargos,
facturas + detalle, pagos + aplicación, cuenta corriente, movimientos, cierres), **comunicación y procesos**
(6: conversación + miembros, reporte, predicción de riesgo, outbox evento + entrega).

Convenciones: PK UUID generados por la aplicación (no autoincrementales, compartidos entre motores); fechas
`TIMESTAMPTZ` en UTC; dinero/peso/volumen `NUMERIC` (`Decimal` en Python, nunca `float`); bajas lógicas
(`activo`/`estado`, nunca `DELETE` de lo necesario para trazabilidad — reforzado en `900_grants.sql`, que le
quita DELETE a la identidad de ejecución sobre auditoría, outbox y las tablas comerciales). Reglas que no
entran en un CHECK viven en **triggers** (compromiso de entrega inmutable, movimientos de cuenta
inmutables, outbox inmutable, tramo/alerta/incidente del mismo envío) o en el **servicio con transacción y
bloqueo** (continuidad de tramos, capacidad de contenedor, aplicaciones de pago). Exclusiones temporales
(`EXCLUDE USING gist`, `btree_gist`) garantizan sin solapamiento: reservas/uso de contenedor, instalación de
sensor, membresías de conversación.

### 3.2 MongoDB (documental) — `db/mongo/setup_mongo.py`

3 colecciones con validador `$jsonSchema`: `lecturas_sensor` (alto volumen; `_id` UUID estable para
idempotencia; `posicion` GeoJSON `[lon, lat]`; subdocumento `evaluacion` con estado/versión de
reglas/reclamo), `eventos_logisticos` (histórico de eventos; `detalles` validado por tipo solo para
`RETRASO`/`ADUANA`, el resto es libre), `mensajes` (chat; texto de 1 a 4000 caracteres). Índices clave:
`ix_lectura_sensor_fecha`, `ix_lectura_posicion_2dsphere`, `ix_evento_envio_fecha_id`,
`ix_mensaje_conversacion_fecha_id` (paginación estable por `(fecha, _id)`). Preparado para **sharding**
condicional de `lecturas_sensor` por `(sensor_id, fecha_medicion)` — se aplica solo si hay un `mongos`
real; en un servidor único se omite (probado con conexiones simuladas, no con un clúster real).

### 3.3 Neo4j (grafos) — `db/neo4j/constraints.cypher`

Nodos `Tercero` (con etiquetas adicionales `:Cliente`/`:Operador`, un nodo por empresa), `Envio`, `Tramo`,
`Ubicacion`, `Contenedor`. Relaciones `SOLICITA`, `UTILIZA {asignacion_id, desde, hasta}`, `TIENE_TRAMO`,
`SIGUIENTE` (solo entre tramos del mismo envío), `OPERA`, `SALE_DE`/`LLEGA_A`. 7 constraints de unicidad + 5
índices. Es una **proyección reconstruible desde PostgreSQL**: nunca decide permisos, saldos ni estados; se
puede borrar y reconstruir (`scripts/sincronizar.py --reconstruir-grafo`). Neo4j Community no admite
constraints de existencia ni unicidad sobre relaciones: la unicidad de `UTILIZA.asignacion_id` y de
`(envio_id, orden)` en `Tramo` se garantiza con `MERGE` por clave estable en el repositorio, no por el motor.

## 4. Justificación de persistencia políglota

| Dato | Motor | Por qué ese motor y no otro |
|---|---|---|
| Terceros, usuarios, roles, permisos, sesiones, envíos, tramos, sensores, alertas, incidentes, cargos, facturas, pagos, cuentas, cierres, conversaciones (metadatos), reportes, predicciones, outbox | **PostgreSQL** | Necesitan transacciones ACID, llaves foráneas, `CHECK`/`UNIQUE`, exclusión temporal y triggers: son el estado operativo y financiero del negocio, donde una inconsistencia (una factura duplicada, una reserva de contenedor superpuesta) es inaceptable. |
| Lecturas de sensores (altísimo volumen, un documento por medición), eventos logísticos (estructura variable por tipo), mensajes de chat | **MongoDB** | Esquema flexible por tipo de evento (un `$jsonSchema` por colección, sin migración de esquema para campos opcionales nuevos), un documento por lectura/mensaje (evita un historial embebido que crezca sin límite dentro de una sola fila relacional) e índices geoespaciales/de paginación estable (`2dsphere`, `(fecha, _id)`) nativos del motor. **No se midió** si esto es más rápido que PostgreSQL con el volumen académico de este TP: la justificación es de modelado (forma del dato, no de rendimiento). |
| Ruta de un envío, red cliente–operador–envío, recorrido de un contenedor | **Neo4j** | Recorridos de varios saltos (siguiente tramo, quién opera, qué contenedor se usó) y consultas de red se expresan de forma más directa en Cypher que con JOINs/CTEs recursivas en SQL — una diferencia de **expresividad y mantenibilidad**, no una medición de velocidad (no se comparó el tiempo de una consulta equivalente en PostgreSQL). PostgreSQL no tiene una limitación real que le impida resolver estas consultas (soporta CTEs recursivas); se eligió Neo4j porque el TP exige los tres paradigmas y porque, al ser una PROYECCIÓN reconstruible, no corre riesgo de ser la única fuente de un dato crítico. |

Paradigmas evaluados y descartados en la Entrega 1 (objetos, clave-valor puro, tabular plano): la
comparación completa vivía en `Informe_Entrega_1_UADE.docx` (no disponible en este repositorio, ver nota al
inicio); no se puede reproducir ni citar textualmente acá. Lo único que se puede afirmar con la fuente
disponible es el resultado ya volcado en la tabla de arriba (qué motor terminó eligiéndose para cada dato).

## 5. Sincronización, consistencia eventual y el desafío adicional

Principios: sin transacción distribuida entre los tres motores; entrega **al menos una vez, nunca
exactamente una** (ver la aclaración de la outbox más abajo); consumidores **idempotentes** (`_id`/`MERGE`
por clave estable); **versión por agregado** (un evento viejo nunca hace retroceder la proyección); estado
de sincronización siempre **visible**, nunca oculto.

- **Cambio de envío/tramo:** el servicio guarda el cambio de negocio y una fila en `outbox_evento` (+ una
  `outbox_entrega` por destino) **en la misma transacción de PostgreSQL**. Un trabajador reclama entregas
  pendientes atómicamente (`FOR UPDATE SKIP LOCKED`, reclamo con propietario y versión), así que **dos
  trabajadores nunca reclaman la misma fila de `outbox_entrega` al mismo tiempo**. Eso evita un doble
  procesamiento concurrente, pero **no** convierte el patrón en "exactamente una vez" de punta a punta: si un
  trabajador se cae después de aplicar el efecto en Mongo/Neo4j pero antes de marcar la entrega
  `COMPLETADA`, esa misma entrega se reintenta y se **reentrega** al consumidor. Por eso cada consumidor
  (Mongo por `_id` con `$setOnInsert`, Neo4j por `MERGE` con guarda de versión) tiene que ser **idempotente**
  de verdad: la garantía real del sistema es "al menos una entrega, consumo idempotente", no "exactamente
  una vez".
- **Facturación automática** (decisión del grupo): confirmar la entrega **solo** confirma; el evento
  `ENVIO_ENTREGADO` lleva el destino `FACTURACION` en la outbox y un consumidor emite la factura **en la
  misma transacción** en la que completa esa entrega. Un problema comercial nunca bloquea una entrega — se
  reintenta como cualquier otra entrega de la outbox.
- **Lectura IoT:** la lectura se escribe **directo en MongoDB** (con `_id` estable), fuera de cualquier
  transacción de PostgreSQL — ese primer `insert` en Mongo no tiene ninguna atomicidad con PostgreSQL, es un
  paso aparte. Después, una tarea **reserva** ese documento en Mongo (marca `evaluacion.estado` de forma
  atómica para que no la tome dos veces), evalúa las reglas de alerta y recién ahí abre **una** transacción
  de PostgreSQL que escribe `lectura_procesada` + los efectos sobre alertas/incidentes/estado del sensor
  **juntos**. Esa transacción de PostgreSQL cubre solo el lado de PostgreSQL: si el proceso se cae entre
  reservar en Mongo y confirmar en PostgreSQL, la reserva vencida se recupera y se reevalúa (detalle del
  seguimiento `evaluacion` en `src/logistica/services/monitoreo_service.py` y `domain/monitoreo.py`); no hay
  una transacción única que abarque Mongo y PostgreSQL a la vez (eso sería la transacción distribuida que la
  consigna prohíbe).
- **Reconstrucción del grafo:** Neo4j se recalcula ÍNTEGRAMENTE desde PostgreSQL por envío (nunca se
  "parchea"); un `MERGE` con guarda de versión impide que una proyección vieja pise a una más nueva.

**Problemas de consistencia que pueden aparecer y su estrategia** (demostrado, no solo descrito):

| Problema | Estrategia | Evidencia real |
|---|---|---|
| Neo4j caído mientras se opera | La escritura de negocio en PostgreSQL sigue funcionando; las entregas a Neo4j quedan `PENDIENTE`/reintentando con espera creciente, visibles en la cola | `tests/test_fase3_sincronizacion.py`: Neo4j se apaga simulando un puerto sin servidor (falla de conexión real), se opera igual y al volver el grafo converge sin duplicados |
| Reentrega de un evento (al menos una vez, nunca exactamente una) | Mongo inserta por `_id` (`$setOnInsert`); Neo4j aplica con `MERGE` | Pruebas de idempotencia en fase 3 y en la siembra de demostración (dos corridas, cero duplicados) |
| Eventos desordenados entre trabajadores | Versión por agregado: un evento con versión menor nunca sobrescribe una proyección más nueva | `test_redefinir_tramos_reemplaza_los_nodos_sin_dejar_huerfanos` y similares |
| Outbox que agota sus reintentos | Queda `FALLIDA` (nunca se borra); se ve en `scripts/sincronizar.py --estado` y ahora también en el reporte de **reconciliación** (§6) | `tests/test_fase9_reconciliacion.py::test_detecta_outbox_mongo_completada_sin_documento` |
| Un motor caído durante la reconciliación periódica | Se reporta "no disponible" para esa corrida; nunca se interpreta como "todo bien" ni se intenta reparar | `tests/test_fase9_reconciliacion.py::test_reporta_neo4j_no_disponible_sin_romperse` |

## 6. Reconciliación periódica (diagnóstico, nunca reparación)

`services/reconciliacion_service.py` (nuevo, fase 9) es un proceso TÉCNICO más, con el mismo diseño que
`SincronizacionService`/`PrediccionService`: sin sesión de usuario, solo lee y compara, **nunca repara ni
reconstruye nada global**. Corre periódicamente dentro del planificador de la fase 8 (tarea
`RECONCILIACION`, diaria, con su propio advisory lock) y también a mano con `scripts/reconciliar.py`.

Qué compara, acotado a un volumen manejable (últimos 200 envíos; ampliable):

1. **Entre motores:** por cada envío, si Neo4j tiene su nodo `:Envio` y si su versión no quedó atrás de la
   de PostgreSQL sin una entrega pendiente/fallida que lo explique; si una entrega `MONGO_EVENTOS` marcada
   `COMPLETADA` realmente tiene un documento en `eventos_logisticos`. Reporta también, aparte, el resumen
   global de la cola (`PENDIENTE`/`EN_PROCESO`/`FALLIDA`/`COMPLETADA` por destino) y el detalle de cada
   entrega `FALLIDA` (motivo por el que la fase 3 ya la dejó de reintentar).
2. **Comercial:** pagos cuyas aplicaciones vigentes superan el disponible, facturas cuyas aplicaciones
   superan el saldo (verificación independiente: los servicios ya lo impiden con bloqueos, pero reconciliar
   lo vuelve a comprobar sin confiar en eso) y cierres mensuales cuyo saldo guardado no coincide con lo que
   dan los movimientos reales de ese período, recalculado desde cero.
3. **Motor no disponible:** cada verificación de Neo4j/Mongo está en su propio `try/except
   DatabaseUnavailableError`; si un motor no responde, esa parte del reporte dice explícitamente "no
   disponible" y se omite — nunca se asume consistencia ni se repara nada en su ausencia.

El resultado (`ResultadoReconciliacion`) nunca modifica datos: reparar una entrega fallida sigue siendo
`scripts/sincronizar.py --reintentar-fallidas`/`--reconstruir-grafo`, y corregir un pago/factura sigue
siendo una acción manual de un usuario con permiso. 10 pruebas en `tests/test_fase9_reconciliacion.py`
(contra los tres motores reales, con limpieza por envío) prueban que detecta cada caso sin generar falsos
positivos sobre datos consistentes y que no se rompe con un motor caído.

## 7. Pruebas reales

**Automatizadas:** 604 casos en total (conteo estable: dos corridas completas e independientes de la suite,
descriptas abajo, colectaron exactamente 604 cada una). Corren contra los tres motores reales, mayormente
dentro de una transacción de PostgreSQL que se revierte (no dejan datos), con Mongo/Neo4j reales limpiados
por id al terminar; las de concurrencia usan bases temporales descartables con dos conexiones o dos
instancias reales a la vez.

**No se registró ninguna corrida completa de la suite con 604/604 aprobadas.** Las dos corridas completas
que sí se hicieron al cierre de esta tarea dieron, cada una, un resultado distinto:

| Corrida | Condición del equipo | Resultado |
|---|---|---|
| Completa 1 | Con un build de PyInstaller compilando en paralelo | **602 aprobadas, 2 falladas, 1 error de teardown** (604 casos) |
| Completa 2 | Sin ese build, pero con otras aplicaciones de alto uso de CPU abiertas (confirmado con `Get-Process`: dos procesos de VS Code, un juego, un cliente de voz y un launcher, todos con uso de CPU acumulado alto) | **597 aprobadas, 7 falladas** (604 casos) |

Entre las dos corridas completas aparecieron **8 identidades de prueba distintas** con problemas (nunca una
prueba de lógica de negocio — dominio, reglas de alerta, cálculo de importes, permisos, etc. —, siempre las
mismas 8, listadas abajo):

- Solo en la corrida 1 (1 caso): `test_fase9_reconciliacion.py::test_un_envio_consistente_no_genera_discrepancias_propias` — no falló la prueba en sí, sino su *teardown*: un `TimeoutError` real al conectar a Neo4j para limpiar.
- Solo en la corrida 2 (5 casos): `test_fase0_cimientos.py::test_conexiones_reales_los_tres_motores_responden`,
  `test_fase5_chat_ui.py::test_chat_desde_dos_ventanas_con_sesiones_y_conexiones_independientes[PRIVADA]`,
  `test_fase5_chat_ui.py::test_dos_procesos_de_aplicacion_intercambian_mensajes_con_sesiones_propias`,
  `test_fase5_comunicacion.py::test_reintento_con_uuid_estable_conserva_fecha_y_rechaza_otro_contenido`,
  `test_fase5_comunicacion.py::test_falla_entre_motores_no_confirma_y_reintenta_sin_duplicar[auditoria]`.
- En las dos corridas (2 casos): `test_fase9_seed_demo.py::test_cli_ayuda_sale_con_codigo_0` y
  `test_fase9_seed_demo.py::test_cli_sin_email_sale_con_codigo_distinto_de_0`.

Lo que tienen en común las 8: todas hacen una aserción sobre **tiempo real de respuesta** (un límite de 6 s
para que los tres motores contesten el chequeo de salud, 30-35 s para que un subproceso de Python/Qt
arranque y termine) o dependen de que un socket a PostgreSQL/MongoDB/Neo4j responda dentro de un timeout de
librería. Ninguna falló por un valor calculado incorrecto, una regla de negocio violada o una aserción de
dominio.

**Reejecuciones parciales (no son una corrida de la suite completa; se listan aparte a propósito):**

- Después de la corrida completa 1, se volvieron a correr, aisladas (sin el build compilando), únicamente
  los archivos `test_fase9_seed_demo.py` + `test_fase9_reconciliacion.py` (22 casos, el subconjunto que
  contiene las 3 fallas/errores de esa corrida): **22/22 aprobadas, salida 0.** Esto confirma que ESE
  subconjunto pasa limpio sin el build compitiendo; no confirma el resultado de los otros 582 casos de la
  suite, que no se volvieron a correr en esa reejecución.
- Después de la corrida completa 2, se volvieron a correr, aisladas, únicamente los 7 casos que habían
  fallado en esa corrida: **3 aprobadas, 4 falladas** (con las mismas aplicaciones de alto uso de CPU
  todavía abiertas, confirmado de nuevo con `Get-Process` inmediatamente antes de esta reejecución).

**Hipótesis, no conclusión probada:** la causa más probable de estas 8 fallas es la carga de CPU del equipo
compartido (evidencia a favor: el patrón se repite solo en pruebas que miden tiempo real; en la reejecución
parcial sin el build 3/3 de las fallas de la corrida 1 desaparecieron; en la reejecución con carga
persistente, 4/7 de las fallas de la corrida 2 persistieron). **No se hizo un experimento controlado**
(correr las mismas 8 pruebas repetidas veces con y sin carga medida) que lo confirme estadísticamente, así
que se deja planteado como hipótesis respaldada por esta evidencia, no como hecho demostrado. No se
modificó ningún timeout del código para "hacerlas pasar": sería un cambio sin evidencia de que el valor
esté mal.

**Seed de demostración en la base real:** el equipo ejecutó `scripts/seed_demo.py` contra la base de
desarrollo con **código de salida 0**. No se volvió a ejecutar ni se consultaron las bases en esta revisión documental. La preservación de los registros y fechas corresponde a lo informado en revisiones anteriores; no es una nueva comprobación del estado de las bases.

**Manuales:** únicamente se declaran hechas las que el grupo efectivamente comunicó haber recorrido en la
ventana (fases 2, 4 y parte de la 5 — ver el detalle exacto en `README.md`, sección "Qué está verificado y
qué no"). Además, el grupo comunicó haber mirado la pantalla **Comercial → Facturas** y haber visto la
factura esperada listada ahí; eso es **una verificación visual puntual de una pantalla**, no una prueba
manual del circuito comercial completo (cargar un cargo, confirmar la entrega, ver la factura automática,
registrar un cobro parcial, aplicarlo, cerrar el período) — ese recorrido completo sigue sin comunicarse. El
chat, los reportes/predicción y los procesos periódicos **no tienen ninguna prueba manual del grupo
todavía**: tienen automatización y datos de demostración, pero nadie del grupo los recorrió en la ventana.
No se atribuye al grupo ninguna prueba manual que no haya comunicado, y no se exige un recorrido exhaustivo
pantalla por pantalla como condición para entregar: el grupo decidió no hacer esa campaña de QA manual.

## 8. Limitaciones conocidas

- Sharding de MongoDB: preparado y probado solo con conexiones simuladas, no contra un clúster `mongos` real.
- Neo4j "caído" siempre se simuló con un puerto sin servidor (falla de conexión real), nunca deteniendo el
  contenedor real durante una corrida.
- Parámetros de polling/pool/argon2: valores iniciales razonables, no medidos en un entorno de demostración
  con carga real.
- Reconciliación revisa los últimos 200 envíos por corrida (acotado a propósito; volumen académico). Con
  mucho más historial habría que paginar o filtrar por fecha.
- **Retención de la outbox:** hoy `outbox_evento`/`outbox_entrega` no se borran nunca (no hay DELETE
  concedido sobre `outbox_evento`); es una decisión de diseño sin cerrar, no un requisito confirmado de la
  consigna (ver §1.2).
- Chat, procesos periódicos y reportes/predicción: sin prueba manual del grupo. Comercial: solo una
  verificación visual puntual de una pantalla (ver §7); el circuito completo sigue sin prueba manual.
- 8 pruebas que miden tiempo real de respuesta (ver §7) fallaron de forma intermitente en las dos corridas
  completas de cierre; la hipótesis más probable es la carga de CPU del equipo compartido, no un error de
  lógica, pero no se confirmó con un experimento controlado. Para una corrida de verificación, conviene no
  tener otras aplicaciones pesadas abiertas al mismo tiempo.

## 9. Manual breve de uso

Instalación, variables de entorno, preparación de las bases, creación del administrador, ejecución desde
código fuente y como ejecutable empaquetado: ver [`docs/entrega-reproducible.md`](./entrega-reproducible.md).
Recorrido pantalla por pantalla de cada módulo (Monitoreo, Trazabilidad, Chat, Comercial): ver las secciones
"Probar ... desde la ventana" del [`README.md`](../README.md). Guion de demostración para la defensa:
[`docs/guion-defensa.md`](./guion-defensa.md).
