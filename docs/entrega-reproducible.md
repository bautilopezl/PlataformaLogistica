# Entrega reproducible — instalación, preparación y ejecución

Checklist operativo para instalar, preparar y ejecutar el proyecto desde una copia de entrega. La instalación en una máquina limpia no se certifica en esta revisión documental.
Para el detalle de cada comando y de cada pantalla, ver [`README.md`](../README.md); este documento es el
checklist de cierre, en orden.

## 1. Qué se entrega

- **Código fuente completo:** `src/logistica/` (dominio, servicios, repositorios, UI), `db/` (scripts de
  creación de las tres bases), `scripts/` (setup, administrador, simulador, sincronización, procesos
  periódicos, reconciliación, siembra de demostración, empaquetado), `tests/` (604 pruebas automáticas).
- **Dependencias:** `requirements.txt` (de la aplicación) y `requirements-dev.txt` (+ pruebas y empaquetado).
- **`.env.example`** sin secretos (placeholders); nunca se versiona el `.env` real.
- **`docker-compose.yml`** (PostgreSQL 16, MongoDB 7, Neo4j 5 Community, con healthchecks).
- **Ejecutable empaquetado** (PyInstaller) además de la ejecución desde fuente — ver §5. **El ejecutable NO
  incluye las bases de datos**: PostgreSQL, MongoDB y Neo4j siguen siendo necesarios y accesibles (por
  Docker o servidores ya levantados) tanto para correr desde fuente como para correr el `.exe`.

### 1.1 Qué se excluye explícitamente

`.gitignore` ya cubre esto; se repite acá porque es parte de la entrega reproducible:

- `.env` (credenciales reales) — solo se versiona `.env.example` con placeholders.
- `.venv/` (entorno virtual) — se recrea con ` .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`.
- `logs/*.log*` (pueden contener metadatos operativos; nunca contraseñas, por `RedactSecretsFilter`, pero
  igual no se versionan).
- Intermedios de PyInstaller (`build/`, `*.spec`). La carpeta `dist/PlataformaLogistica/` **sí se incluye** en este ZIP: contiene el ejecutable y su carpeta `_internal/` completa.
- `reportes/` (archivos PDF/CSV generados por la fase 7).
- `AGENTS.md` (documento de trabajo local del equipo con los asistentes de IA; no es parte de la entrega
  académica y no debe confundirse con el informe final).

## 2. Instalación desde el ZIP

Requisitos previos: Windows para el ejecutable incluido, Docker Desktop con Docker Compose y, para ejecutar desde fuente o preparar las bases con los scripts, Python 3.11 o posterior y acceso a los paquetes de dependencias. Las bases siguen siendo externas al ejecutable.

1. Descomprimir `PlataformaLogistica.zip` en una carpeta propia. Conservar toda la estructura, incluida `dist/PlataformaLogistica/_internal/`.
2. Abrir PowerShell y situarse en la carpeta extraída que contiene `README.md`, `docker-compose.yml` y `.env.example`. El ejemplo siguiente supone `D:\ENTREGA\PlataformaLogistica`; adaptar la ruta.
3. Copiar `.env.example` a `.env` únicamente si todavía no existe, y completar las claves de los motores y del usuario de ejecución con valores propios antes de levantar Docker.

```powershell
Set-Location 'D:\ENTREGA\PlataformaLogistica'
if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
notepad .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d
docker compose ps
```

Esperar a que los tres motores estén `healthy`. En los comandos Python de las secciones siguientes se usa explícitamente el intérprete del entorno virtual; no es necesario activar PowerShell ni cambiar su política de ejecución. Preparar las bases y crear el primer administrador con §3 y §4 antes de iniciar sesión.

### Alternativa: obtener el código con Git

```powershell
git clone https://github.com/bautilopezl/PlataformaLogistica.git
Set-Location PlataformaLogistica
```

Una copia clonada puede diferir de este ZIP si contiene cambios aún no publicados. Para evaluar exactamente esta entrega, usar el ZIP. El ejecutable no necesariamente está versionado en Git; puede generarse con `scripts/empaquetar.py`.

## 3. Preparar las bases (sin reset: no se borra nada existente)

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe scripts\setup_databases.py                 # idempotente: crea lo que falte, no toca lo que ya existe
```

`setup_databases.py` **sin** `--reset` nunca borra datos: cada script SQL usa `CREATE TABLE IF NOT EXISTS` /
`DO $$ ... $$` / `ON CONFLICT`, y `setup_mongo.py`/`constraints.cypher` son igual de idempotentes. **Nunca
ejecutar `--reset --yes`** sobre una base con datos que se quieran conservar (borra todo el esquema). El
particionamiento opcional (`--particionar`) tampoco es necesario para correr la aplicación.

## 4. Crear el primer administrador (solo si no hay ninguno activo)

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe scripts\crear_admin.py --email admin@empresa.com --nombre Ana --apellido Paz
```

Pide la contraseña por teclado (sin eco, nunca como argumento). `crear_administrador_inicial` se niega si ya
existe un administrador activo: no es una puerta trasera para crear uno de más. **Si la base ya tiene un
administrador (como la de desarrollo del equipo, con `lucas@logistica.com`), no hace falta este paso.**

## 5. Ejecutar

### 5.1 Desde el código fuente (siempre disponible)

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe -m logistica.main
```

### 5.2 Como ejecutable empaquetado (PyInstaller)

```powershell
.\.venv\Scripts\python.exe scripts\empaquetar.py             # --onedir (carpeta dist/PlataformaLogistica/), o --onefile
```

El build queda en `dist/PlataformaLogistica/PlataformaLogistica.exe` (más rápido de abrir) o, con
`--onefile`, en `dist/PlataformaLogistica.exe` (un solo archivo, arranque algo más lento porque
autoextrae). **Antes de abrirlo:** copiar el `.env` junto al ejecutable (o dejarlo en el directorio de
trabajo desde el que se lo ejecuta) y tener las tres bases accesibles (`docker compose up -d`, o servidores
ya levantados con las credenciales de ese `.env`) — **empaquetar la aplicación no empaqueta ni reemplaza las
bases de datos**, que siguen siendo externas y obligatorias tanto en desarrollo como en la defensa.

El build se verificó así: `python scripts/empaquetar.py --limpio` compiló sin errores
(`PyInstaller` 6.22.3 + el hook de `pyinstaller-hooks-contrib` para PySide6; agregado a
`requirements-dev.txt`) y generó `dist/PlataformaLogistica/PlataformaLogistica.exe` (15,4 MB) junto a su
carpeta `_internal/` con el recurso del mapa GPS incluido. Se lanzó dos veces: **sin** `.env` junto al
ejecutable, cierra en segundos con código 2 y sin ventana (falta una variable obligatoria, comportamiento
esperado de `load_config()`, nunca un crash de importación/recurso faltante); **con** una copia temporal del
`.env` real junto al ejecutable (borrada después de la prueba), abrió la ventana "Iniciar sesión · Plataforma
de logística" contra los tres motores reales. No se inició sesión ni se tocó ningún dato: solo se confirmó
que arranca. El detalle exacto de lo verificado (y lo que no) queda en el reporte de cierre de esta tarea
(no se vuelve a probar pantalla por pantalla acá).

## 6. Datos de demostración (credenciales de prueba, documentadas por separado)

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe scripts\seed_demo.py --email admin@empresa.com     # el administrador de ESA base; pide su clave sin eco
```

Crea terceros, usuarios, envíos, cargos/facturas/cobros, la demo de monitoreo IoT y conversaciones de chat,
todo marcado con el prefijo `DEMO-SEED`/`DEMO-MON-`. Es **idempotente**: correrlo de nuevo no duplica nada,
y recupera una corrida interrumpida a la mitad (ver `scripts/seed_demo.py`, docstring, y
`tests/test_fase9_seed_demo.py`). **Nunca** toca datos sin ese prefijo ni crea otro administrador.

**Credenciales de las cuentas de demostración** (no son credenciales reales; no usar fuera de este entorno
de prueba):

| Usuario | Rol | Contraseña |
|---|---|---|
| `seed.operaciones@demo.local` | OPERACIONES | `DemoSeed-2026!` (o la que fije `SEED_DEMO_PASSWORD` en `.env`) |
| `seed.comercial@demo.local` | COMERCIAL | ídem |
| `seed.cliente.norte@demo.local` | CLIENTE (Exportadora Demo Seed Norte SA) | ídem |
| `seed.cliente.norte2@demo.local` | CLIENTE (misma empresa, para probar chat privado entre dos usuarios) | ídem |
| `seed.operador.maritimo@demo.local` | OPERADOR_LOGISTICO (Transportes Marítimos Demo Seed SA) | ídem |

El administrador real de la base (ejemplo: `lucas@logistica.com`) **no** se toca ni se lista acá: su
contraseña la conserva quien administra esa base, nunca este documento ni el agente que lo generó.

## 7. Reconciliación de diagnóstico (opcional, no bloquea nada)

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe scripts\reconciliar.py
```

Corre también sola, una vez al día, mientras haya una instancia interna abierta (planificador de la fase 8).
Nunca repara ni borra: solo informa discrepancias y motores no disponibles (ver `docs/informe-final.md` §6).

## 8. Verificación automatizada (opcional, para quien clone el repo)

```powershell
pytest tests/test_static_artifacts.py tests/test_fase0_cimientos.py   # sin servidores
$env:LOGISTICA_TEST_CONNECTIONS="1"
$env:LOGISTICA_TEST_PG="host=localhost port=5434 dbname=logistica user=logistica_admin password=..."
$env:PYTHONPATH="src"
pytest tests -q    # 604 pruebas, los tres motores levantados
```

**Nota sobre estabilidad:** 8 identidades de prueba distintas que miden tiempo real de respuesta (chequeo de
conexión a los tres motores con límite de 6 s, intercambio de chat entre dos procesos Qt, inicio de
`seed_demo.py` con límite de 30-35 s) fallaron de forma intermitente en las dos corridas completas de cierre
de esta tarea (nunca una prueba de dominio/servicio/lógica de negocio). La hipótesis más probable es la
carga de CPU de una máquina compartida con otras aplicaciones, pero no se confirmó con un experimento
controlado: reejecutadas aisladas, un subconjunto pasó limpio y otro volvió a fallar con la misma carga
todavía presente. Detalle completo, caso por caso, en [`docs/informe-final.md` §7](./informe-final.md).
