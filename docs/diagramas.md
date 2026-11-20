# Diagramas de los modelos físicos

Anexo del [informe final](./informe-final.md). Los modelos se derivan de los scripts incluidos en `db/`, sin ejecutar la aplicación ni conectarse a las bases.

## 1. PostgreSQL — 38 tablas en 5 módulos

Se muestran todas las columnas declaradas en los scripts de creación, sus claves primarias y foráneas. Los tipos se simplifican en los diagramas (por ejemplo, `NUMERIC` sin precisión); los scripts conservan longitudes, precisión, valores por defecto, restricciones, índices y triggers.

Cada vínculo representa una FK concreta. En el extremo de la tabla referenciada, `o|` significa referencia opcional (NULL) y `||` referencia obligatoria; en el otro extremo, `o{` permite varias filas y `o|` indica una FK UNIQUE o PK individual. Una PK compuesta no hace única cada columna por separado. La cardinalidad describe el esquema SQL; las reglas adicionales de servicio se explican en el informe.
### 1.1 Acceso (9 tablas)

Fuente: `db/postgres/010_acceso.sql`.

```mermaid
erDiagram
    TERCERO {
        uuid id PK
        varchar razon_social
        varchar pais_codigo
        varchar tipo_identificacion
        varchar numero_identificacion
        varchar email
        varchar telefono
        text direccion
        boolean activo
        timestamptz creado_en
        timestamptz actualizado_en
    }
    TERCERO ||--o{ TERCERO_ROL : "tercero_id"
    TERCERO_ROL {
        uuid tercero_id PK,FK
        varchar rol_comercial PK
        boolean activo
    }
    TERCERO o|--o{ USUARIO : "tercero_id"
    USUARIO {
        uuid id PK
        uuid tercero_id FK "NULL permitido"
        varchar nombre
        varchar apellido
        varchar email
        text password_hash
        boolean activo
        timestamptz creado_en
        timestamptz actualizado_en
    }
    USUARIO ||--o{ SESION : "usuario_id"
    SESION {
        uuid id PK
        uuid usuario_id FK
        timestamptz inicio_en
        timestamptz vence_en
        timestamptz fin_en
        varchar estado
        varchar cliente_descripcion
        uuid instancia_id
    }
    ROL {
        uuid id PK
        varchar codigo UK
        text descripcion
    }
    PERMISO {
        uuid id PK
        varchar codigo UK
        text descripcion
    }
    USUARIO ||--o{ USUARIO_ROL : "usuario_id"
    ROL ||--o{ USUARIO_ROL : "rol_id"
    USUARIO_ROL {
        uuid usuario_id PK,FK
        uuid rol_id PK,FK
    }
    ROL ||--o{ ROL_PERMISO : "rol_id"
    PERMISO ||--o{ ROL_PERMISO : "permiso_id"
    ROL_PERMISO {
        uuid rol_id PK,FK
        uuid permiso_id PK,FK
    }
    USUARIO o|--o{ AUDITORIA : "usuario_id"
    SESION o|--o{ AUDITORIA : "sesion_id"
    AUDITORIA {
        uuid id PK
        timestamptz fecha
        varchar accion
        varchar resultado
        uuid usuario_id FK "NULL permitido"
        uuid sesion_id FK "NULL permitido"
        uuid instancia_id
        varchar entidad
        uuid entidad_id
        jsonb detalle
    }
```

### 1.2 Logística (6 tablas)

Fuente: `db/postgres/020_logistica.sql`.

```mermaid
erDiagram
    UBICACION {
        uuid id PK
        varchar nombre
        varchar tipo
        varchar pais_codigo
        varchar ciudad
        text direccion
        numeric latitud
        numeric longitud
        boolean activo
    }
    TERCERO ||--o{ VEHICULO : "operador_id"
    VEHICULO {
        uuid id PK
        uuid operador_id FK
        varchar tipo
        varchar identificacion
        varchar estado
    }
    CONTENEDOR {
        uuid id PK
        varchar codigo_internacional UK
        varchar tipo
        numeric capacidad_kg
        numeric capacidad_m3
        varchar estado
    }
    TERCERO ||--o{ ENVIO : "cliente_id"
    UBICACION ||--o{ ENVIO : "origen_id"
    UBICACION ||--o{ ENVIO : "destino_id"
    USUARIO ||--o{ ENVIO : "creado_por_id"
    ENVIO {
        uuid id PK
        varchar codigo UK
        uuid cliente_id FK
        uuid origen_id FK
        uuid destino_id FK
        text descripcion_carga
        varchar estado
        varchar prioridad
        numeric peso_kg
        numeric volumen_m3
        timestamptz creado_en
        timestamptz entrega_estimada_en
        timestamptz entrega_comprometida_en
        timestamptz entrega_real_en
        uuid creado_por_id FK
        timestamptz actualizado_en
        bigint version
    }
    ENVIO ||--o{ TRAMO : "envio_id"
    UBICACION ||--o{ TRAMO : "origen_id"
    UBICACION ||--o{ TRAMO : "destino_id"
    TERCERO ||--o{ TRAMO : "operador_id"
    VEHICULO o|--o{ TRAMO : "vehiculo_id"
    TRAMO {
        uuid id PK
        uuid envio_id FK
        integer orden
        uuid origen_id FK
        uuid destino_id FK
        varchar medio
        uuid operador_id FK
        uuid vehiculo_id FK "NULL permitido"
        varchar estado
        timestamptz salida_estimada_en
        timestamptz llegada_estimada_en
        timestamptz salida_real_en
        timestamptz llegada_real_en
        numeric costo
        varchar moneda_codigo
        bigint version
    }
    ENVIO ||--o{ ENVIO_CONTENEDOR : "envio_id"
    CONTENEDOR ||--o{ ENVIO_CONTENEDOR : "contenedor_id"
    ENVIO_CONTENEDOR {
        uuid id PK
        uuid envio_id FK
        uuid contenedor_id FK
        varchar estado
        timestamptz reserva_desde
        timestamptz reserva_hasta
        timestamptz uso_desde
        timestamptz uso_hasta
        numeric peso_asignado_kg
        numeric volumen_asignado_m3
    }
```

### 1.3 Monitoreo (9 tablas)

Fuente: `db/postgres/030_monitoreo.sql`.

```mermaid
erDiagram
    SENSOR {
        uuid id PK
        varchar codigo UK
        varchar tipo
        varchar fabricante
        varchar modelo
        varchar estado
        integer intervalo_esperado_seg
        timestamptz creado_en
    }
    SENSOR ||--o{ SENSOR_CAPACIDAD : "sensor_id"
    SENSOR_CAPACIDAD {
        uuid sensor_id PK,FK
        varchar variable PK
    }
    SENSOR ||--o{ SENSOR_CONTENEDOR : "sensor_id"
    CONTENEDOR ||--o{ SENSOR_CONTENEDOR : "contenedor_id"
    USUARIO ||--o{ SENSOR_CONTENEDOR : "instalado_por_id"
    SENSOR_CONTENEDOR {
        uuid id PK
        uuid sensor_id FK
        uuid contenedor_id FK
        timestamptz instalado_en
        timestamptz retirado_en
        uuid instalado_por_id FK
        text observaciones
    }
    ENVIO ||--o| CONDICION_MONITOREO : "envio_id"
    CONDICION_MONITOREO {
        uuid id PK
        uuid envio_id FK,UK
        numeric temperatura_min_c
        numeric temperatura_max_c
        numeric humedad_min_pct
        numeric humedad_max_pct
        numeric vibracion_max_g
        numeric bateria_min_pct
        integer tiempo_max_sin_lecturas_seg
        varchar version_reglas
        jsonb reglas_evaluacion
        timestamptz creado_en
    }
    SENSOR ||--o| SENSOR_ESTADO_MONITOREO : "sensor_id"
    SENSOR_ESTADO_MONITOREO {
        uuid sensor_id PK,FK
        timestamptz ultima_recepcion_en
        timestamptz ultima_medicion_en
        uuid ultima_lectura_id
        timestamptz actualizado_en
        jsonb evaluacion_condiciones
    }
    ENVIO ||--o{ ALERTA : "envio_id"
    CONTENEDOR ||--o{ ALERTA : "contenedor_id"
    TRAMO o|--o{ ALERTA : "tramo_id"
    SENSOR o|--o{ ALERTA : "sensor_id"
    USUARIO o|--o{ ALERTA : "reconocida_por_id"
    USUARIO o|--o{ ALERTA : "cerrada_por_id"
    ALERTA {
        uuid id PK
        uuid envio_id FK
        uuid contenedor_id FK
        uuid tramo_id FK "NULL permitido"
        uuid sensor_id FK "NULL permitido"
        varchar tipo
        varchar severidad
        varchar estado
        timestamptz primera_deteccion_en
        timestamptz ultima_deteccion_en
        timestamptz normalizada_en
        timestamptz cerrada_en
        uuid reconocida_por_id FK "NULL permitido"
        timestamptz reconocida_en
        uuid cerrada_por_id FK "NULL permitido"
        text motivo_cierre
        varchar cierre_tipo
        varchar clave_deduplicacion
        jsonb detalle
    }
    ENVIO ||--o{ INCIDENTE : "envio_id"
    TRAMO o|--o{ INCIDENTE : "tramo_id"
    CONTENEDOR o|--o{ INCIDENTE : "contenedor_id"
    USUARIO o|--o{ INCIDENTE : "responsable_id"
    ALERTA o|--o| INCIDENTE : "alerta_origen_id"
    INCIDENTE {
        uuid id PK
        uuid envio_id FK
        uuid tramo_id FK "NULL permitido"
        uuid contenedor_id FK "NULL permitido"
        varchar tipo
        varchar severidad
        varchar estado
        text descripcion
        timestamptz ocurrido_en
        timestamptz abierto_en
        uuid responsable_id FK "NULL permitido"
        timestamptz resuelto_en
        text resolucion
        uuid alerta_origen_id FK,UK "NULL permitido"
    }
    INCIDENTE ||--o{ INCIDENTE_ALERTA : "incidente_id"
    ALERTA ||--o{ INCIDENTE_ALERTA : "alerta_id"
    INCIDENTE_ALERTA {
        uuid incidente_id PK,FK
        uuid alerta_id PK,FK
    }
    LECTURA_PROCESADA {
        uuid lectura_id PK
        varchar version_reglas PK
        timestamptz procesada_en
    }
```

### 1.4 Comercial (8 tablas)

Fuente: `db/postgres/040_comercial.sql`.

```mermaid
erDiagram
    ENVIO ||--o{ CARGO_ENVIO : "envio_id"
    CARGO_ENVIO {
        uuid id PK
        uuid envio_id FK
        varchar concepto
        numeric cantidad
        numeric precio_unitario
        varchar moneda_codigo
        varchar estado
        timestamptz creado_en
    }
    TERCERO ||--o{ FACTURA : "tercero_id"
    USUARIO ||--o{ FACTURA : "creada_por_id"
    USUARIO o|--o{ FACTURA : "anulada_por_id"
    FACTURA {
        uuid id PK
        uuid tercero_id FK
        varchar sentido
        varchar tipo_comprobante
        varchar serie
        varchar numero
        timestamptz emitida_en
        timestamptz vence_en
        varchar moneda_codigo
        numeric importe_total
        varchar estado
        uuid creada_por_id FK
        timestamptz creado_en
        timestamptz anulada_en
        uuid anulada_por_id FK "NULL permitido"
        text motivo_anulacion
    }
    FACTURA ||--o{ FACTURA_DETALLE : "factura_id"
    ENVIO o|--o{ FACTURA_DETALLE : "envio_id"
    CARGO_ENVIO o|--o| FACTURA_DETALLE : "cargo_envio_id"
    FACTURA_DETALLE {
        uuid id PK
        uuid factura_id FK
        integer renglon
        uuid envio_id FK "NULL permitido"
        uuid cargo_envio_id FK,UK "NULL permitido"
        varchar concepto
        numeric cantidad
        numeric precio_unitario
        numeric subtotal
    }
    TERCERO ||--o{ PAGO : "tercero_id"
    USUARIO ||--o{ PAGO : "registrado_por_id"
    USUARIO o|--o{ PAGO : "anulado_por_id"
    PAGO {
        uuid id PK
        uuid tercero_id FK
        varchar sentido
        timestamptz fecha
        numeric monto
        varchar moneda_codigo
        varchar medio
        varchar referencia
        varchar estado
        uuid registrado_por_id FK
        uuid clave_idempotencia UK
        timestamptz creado_en
        timestamptz anulado_en
        uuid anulado_por_id FK "NULL permitido"
        text motivo_anulacion
    }
    PAGO ||--o{ PAGO_APLICACION : "pago_id"
    FACTURA ||--o{ PAGO_APLICACION : "factura_id"
    PAGO_APLICACION {
        uuid id PK
        uuid pago_id FK
        uuid factura_id FK
        numeric monto_aplicado
        timestamptz creado_en
        timestamptz anulada_en
        text motivo_anulacion
    }
    TERCERO ||--o{ CUENTA_CORRIENTE : "tercero_id"
    CUENTA_CORRIENTE {
        uuid id PK
        uuid tercero_id FK
        varchar naturaleza
        varchar moneda_codigo
        boolean activo
        timestamptz creado_en
    }
    CUENTA_CORRIENTE ||--o{ MOVIMIENTO_CUENTA : "cuenta_id"
    FACTURA o|--o{ MOVIMIENTO_CUENTA : "factura_id"
    PAGO o|--o{ MOVIMIENTO_CUENTA : "pago_id"
    MOVIMIENTO_CUENTA o|--o| MOVIMIENTO_CUENTA : "reversa_de_id"
    MOVIMIENTO_CUENTA {
        uuid id PK
        uuid cuenta_id FK
        timestamptz fecha
        varchar sentido
        numeric importe
        uuid factura_id FK "NULL permitido"
        uuid pago_id FK "NULL permitido"
        uuid reversa_de_id FK,UK "NULL permitido"
        text descripcion
        varchar clave_origen UK
        timestamptz creado_en
    }
    CUENTA_CORRIENTE ||--o{ CIERRE_CUENTA : "cuenta_id"
    CIERRE_CUENTA {
        uuid id PK
        uuid cuenta_id FK
        date periodo
        numeric saldo_inicial
        numeric total_aumentos
        numeric total_disminuciones
        numeric saldo_final
        timestamptz cerrado_en
    }
```

### 1.5 Comunicación y procesos (6 tablas)

Fuente: `db/postgres/050_comunicacion_procesos.sql`.

```mermaid
erDiagram
    ENVIO o|--o{ CONVERSACION : "envio_id"
    USUARIO ||--o{ CONVERSACION : "creada_por_id"
    CONVERSACION {
        uuid id PK
        varchar tipo
        varchar nombre
        uuid envio_id FK "NULL permitido"
        uuid creada_por_id FK
        timestamptz creado_en
        timestamptz archivada_en
    }
    CONVERSACION ||--o{ CONVERSACION_MIEMBRO : "conversacion_id"
    USUARIO ||--o{ CONVERSACION_MIEMBRO : "usuario_id"
    CONVERSACION_MIEMBRO {
        uuid id PK
        uuid conversacion_id FK
        uuid usuario_id FK
        varchar rol
        timestamptz ingreso_en
        timestamptz salida_en
    }
    USUARIO ||--o{ REPORTE : "solicitado_por_id"
    REPORTE {
        uuid id PK
        uuid solicitado_por_id FK
        varchar tipo
        jsonb parametros
        varchar formato
        varchar estado
        timestamptz solicitado_en
        timestamptz iniciado_en
        timestamptz finalizado_en
        text archivo_clave
        varchar error_codigo
    }
    ENVIO ||--o{ PREDICCION_RIESGO : "envio_id"
    PREDICCION_RIESGO {
        uuid id PK
        uuid envio_id FK
        date fecha_evaluacion
        numeric probabilidad_demora
        varchar riesgo_climatico
        varchar riesgo_operativo
        varchar metodo_codigo
        varchar metodo_version
        jsonb datos_entrada
        text explicacion
        timestamptz calculado_en
    }
    OUTBOX_EVENTO {
        uuid id PK
        varchar agregado_tipo
        uuid agregado_id
        bigint agregado_version
        varchar evento_tipo
        integer esquema_version
        jsonb payload
        timestamptz creado_en
    }
    OUTBOX_EVENTO ||--o{ OUTBOX_ENTREGA : "evento_id"
    OUTBOX_ENTREGA {
        uuid evento_id PK,FK
        varchar destino PK
        varchar estado
        integer intentos
        timestamptz proximo_intento_en
        timestamptz bloqueada_hasta
        timestamptz procesada_en
        text ultimo_error
        uuid propietario_id
        bigint version_reclamo
    }
```

## 2. MongoDB — 3 colecciones (`db/mongo/setup_mongo.py`)

```mermaid
flowchart TB
    PG[("PostgreSQL<br/>envio / sensor / contenedor /<br/>tramo / conversacion")]

    subgraph LEC["lecturas_sensor (alto volumen, _id UUID estable)"]
        L1["_id, sensor_id, asignacion_sensor_id,<br/>contenedor_id, envio_id, tramo_id?<br/>fecha_medicion, fecha_recepcion<br/>mediciones: temperatura_c?, humedad_pct?,<br/>vibracion_g?, bateria_pct?<br/>posicion: GeoJSON Point [lon, lat]<br/>origen<br/>evaluacion: estado, version_reglas,<br/>intentos, reclamo…"]
    end

    subgraph EVT["eventos_logisticos (histórico)"]
        L2["_id, envio_id, tipo<br/>fecha_ocurrencia, fecha_registro<br/>detalles (validado por tipo:<br/>RETRASO exige motivo,<br/>ADUANA exige etapa;<br/>el resto es libre)"]
    end

    subgraph MSG["mensajes (chat)"]
        L3["_id, conversacion_id, remitente_id<br/>fecha_envio, contenido 1-4000 chars, tipo"]
    end

    PG -. "ids compartidos, sin FK real entre motores" .-> LEC
    PG -.-> EVT
    PG -.-> MSG
```

Índices clave: `ix_lectura_sensor_fecha`, `ix_lectura_posicion_2dsphere` (GeoJSON), `ix_evento_envio_fecha_id`,
`ix_mensaje_conversacion_fecha_id` (paginación estable por `(fecha, _id)`). `lecturas_sensor` está preparada
para sharding por `(sensor_id, fecha_medicion)` (no activado; ver `docs/informe-final.md` §8).

## 3. Neo4j — modelo de grafo (`db/neo4j/constraints.cypher`)

```mermaid
flowchart LR
    Cliente(("Tercero<br/>:Cliente")) -- SOLICITA --> Envio(("Envio"))
    Envio -- "UTILIZA<br/>{asignacion_id, desde, hasta}" --> Contenedor(("Contenedor"))
    Envio -- TIENE_TRAMO --> Tramo1(("Tramo"))
    Tramo1 -- "SIGUIENTE<br/>(mismo envío)" --> Tramo2(("Tramo"))
    Operador(("Tercero<br/>:Operador")) -- OPERA --> Tramo1
    Tramo1 -- SALE_DE --> UbicacionA(("Ubicacion"))
    Tramo1 -- LLEGA_A --> UbicacionB(("Ubicacion"))
```

Es una **proyección reconstruible desde PostgreSQL** (nunca la fuente de verdad de permisos, saldos o
estados): se recalcula por envío con `MERGE` por clave estable y una guarda de versión (un evento viejo
nunca pisa una proyección más nueva). Neo4j Community no admite constraints de existencia ni unicidad sobre
relaciones, por eso `UTILIZA.asignacion_id` y `(envio_id, orden)` de `Tramo` se garantizan en el repositorio,
no por el motor. 7 constraints de unicidad + 5 índices (listados en `db/neo4j/constraints.cypher`).
