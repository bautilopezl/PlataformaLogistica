# Guion de defensa — recorrido de demostración (10–15 min)

Pensado para que cualquiera de los dos integrantes pueda llevarlo adelante y responder preguntas sobre
cualquier parte (la consigna evalúa individualmente). No reemplaza recorrer la aplicación antes de la
defensa: es la guía de qué mostrar y qué decir mientras se muestra.

## 0. Antes de empezar

- `docker compose up -d` (los tres motores healthy) y `python -m logistica.main` (o el ejecutable
  empaquetado) ya abiertos, con el administrador real y, preferentemente, la siembra de demostración ya
  corrida (`scripts/seed_demo.py`) para tener datos de sobra sin tener que cargar nada en vivo.
- Tener a mano una segunda ventana/terminal para mostrar, si hace falta, las tres bases por fuera de la app
  (`psql`, `mongosh`, `neo4j browser` o `cypher-shell`) — pero el recorrido principal es **todo desde la
  UI**: la plataforma es de escritorio con conexión directa, sin API, y eso también se defiende mostrando
  que no hay un servidor HTTP corriendo.

## 1. Arquitectura y las tres bases (2 min)

"Es una aplicación de escritorio en Python con PySide6, sin servidor de aplicación: cada pantalla llama a un
servicio que abre su propia conexión a la base que corresponda. Usamos tres motores, cada uno para lo que
mejor resuelve":

- **PostgreSQL**: todo lo que necesita transacciones y relaciones estrictas — clientes, usuarios y permisos,
  envíos y tramos, alertas e incidentes, facturas y pagos. Mostrar **Terceros** o **Envíos** y mencionar que
  cada alta pasa por sesión → permiso → alcance → regla → transacción → auditoría.
- **MongoDB**: lo de alto volumen y forma variable — lecturas de sensores IoT, el histórico de eventos
  logísticos, los mensajes de chat. Mostrar **Monitoreo → Historial** (miles de lecturas paginadas) o
  **Trazabilidad → Línea de tiempo** (eventos de un envío).
- **Neo4j**: lo que es naturalmente un grafo — la ruta de un envío, la red cliente–operador–envío, el
  recorrido de un contenedor. Mostrar **Trazabilidad → Red** (buscar el envío `ENV-20261103-GHP2TD`: aparece
  **Exportadora Demo SA**, con Ctrl+rueda para acercar).

## 2. Consistencia eventual, en vivo (3 min)

"Los tres motores nunca comparten una transacción: cuando un envío cambia, PostgreSQL guarda el cambio y un
evento en una 'outbox' en la MISMA transacción; un trabajador en segundo plano reparte esos eventos a Mongo
y Neo4j, con reintentos. Esto lo podemos ver fallando y recuperándose":

1. Detener el contenedor de Neo4j (`docker stop logistica-neo4j`) con la ventana abierta.
2. Iniciar un tramo o confirmar una entrega de un envío **PLANIFICADO/EN_TRANSITO de prueba** (nunca sobre
   `ENV-20261103-GHP2TD`, que queda como demo congelada). PostgreSQL confirma igual: la operación de negocio
   no depende de Neo4j.
3. Mostrar en la barra de estado que la sincronización queda con entregas pendientes para Neo4j (no se
   oculta el problema).
4. Volver a levantar Neo4j (`docker start logistica-neo4j`). En la siguiente vuelta del timer (≤10 s) o
   corriendo `scripts/sincronizar.py`, el grafo se pone al día — mostrar **Trazabilidad → Ruta** del mismo
   envío y que ahora dice "ACTUALIZADA".
5. Mencionar la **reconciliación** (`scripts/reconciliar.py` o la tarea diaria del planificador): es un
   chequeo de diagnóstico que compara los tres motores y el estado comercial, y que avisa si algo quedó
   desincronizado o si un motor no respondió — nunca repara nada solo, para no fingir una atomicidad que el
   sistema no tiene.

## 3. Permisos y alcance (2 min)

"Los permisos no son solo botones ocultos: se revalidan en el servicio en cada operación." Mostrar:

- Login como **Administrador** → ve todo, incluida **Auditoría** (quién hizo qué y cuándo).
- Login como **Cliente** (`seed.cliente.norte@demo.local`) → solo ve sus propios envíos y su cuenta por
  cobrar; no ve costos internos ni lo pagado a operadores.
- Login como **Operador** → solo sus tramos, ni el cliente ni lo que se le cobró.
- Mencionar: revocar una sesión (**Sesiones** → Revocar) corta la siguiente operación de esa sesión, en
  cualquier pantalla donde esté.

## 4. Procesos periódicos (2 min)

"Cuatro tareas corren solas, mientras haya una instancia interna abierta (o `scripts/procesos.py` sin
ventana), coordinadas con un advisory lock de PostgreSQL por tarea y período, así dos instancias no hacen
el mismo trabajo dos veces":

- **Facturación automática**: al confirmar la entrega de un envío (mostrar un envío EN_TRANSITO de prueba
  llegando a ENTREGADO), en un minuto aparece la factura en **Comercial → Facturas** sin que nadie la cargue
  a mano.
- **Sensores sin comunicación**: se calcula, no es un estado que alguien marca.
- **Cierre mensual** y **predicción diaria de atrasos**: mostrar **Reportes → Predicción**, explicando que
  es una frecuencia histórica por ruta (mínimo 10 entregas de los últimos 180 días), no un modelo de machine
  learning ni una probabilidad individual calibrada.
- La **reconciliación** diaria (fase 9) es la quinta tarea del mismo planificador.

## 5. Preguntas típicas y respuesta corta

- **"¿Por qué no una API REST?"** — La consigna la prohíbe explícitamente (confirmado por el profesor);
  la app conecta directo a cada motor con su driver.
- **"¿Qué pasa si se cae PostgreSQL?"** — Se bloquean las operaciones que necesitan sesión/integridad; no
  se confirma éxito desde caché. Es la fuente de verdad de permisos y estado operativo.
- **"¿Por qué Neo4j es una proyección y no la fuente del grafo?"** — Porque relaciones/rutas se derivan de
  datos que YA viven en PostgreSQL (tramos, asignaciones); mantenerlo como proyección permite reconstruirlo
  sin perder nada si se corrompe o se reinicia.
- **"¿Cómo evitan procesar un evento dos veces?"** — No evitan la **reentrega** (el patrón es "al menos una
  vez"): lo que evitan es que una reentrega duplique el **efecto**. Mongo inserta por `_id` estable; Neo4j
  aplica con `MERGE`; además cada proyección guarda la versión del agregado y nunca retrocede.
- **"¿Qué tan probado está esto?"** — 604 pruebas automáticas contra los tres motores reales (ver
  `docs/informe-final.md` §7), y ser honestos sobre lo que el equipo probó a mano y lo que no.
- **"¿Por qué a veces falla una prueba de timeout?"** — Un puñado mide tiempo real de respuesta (6-35 s); la
  hipótesis más probable es que fallan en una máquina compartida con otras aplicaciones pesadas, pero no se
  confirmó con un experimento controlado (ver `docs/informe-final.md` §7-8). Mostrarlo con la máquina de la
  defensa libre de otras cargas.
