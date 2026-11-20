#!/usr/bin/env python3
"""Datos de demostración reproducibles (AGENTS.md §14 y §12, fase 9).

Crea terceros con roles, usuarios de cada perfil, ubicaciones reales, un vehículo y contenedores por
operador, una RUTA HISTÓRICA de envíos entregados (para que la predicción de atrasos tenga muestra),
envíos en otros estados, un incidente manual, facturación/cobros/una compra, una demo de monitoreo IoT
(reutilizando `MonitoreoService.preparar_demo`, ya probada) y conversaciones de chat.

Principios (ver AGENTS.md §14 "Datos de prueba/demostración"):
- Pasa por los SERVICIOS, igual que la aplicación: sesión → permiso → alcance → regla → transacción →
  auditoría. No hay SQL en este script salvo UNA excepción documentada (`_retrasar_creacion`, más abajo),
  necesaria para poder reproducir una historia de entregas de los últimos meses sin esperar meses reales,
  y acotada estrictamente al único envío que la misma función acaba de crear (nunca por patrón ni rango).
- IDEMPOTENTE por REGISTRO, no solo "por bloque": terceros/ubicaciones/usuarios/vehículos/contenedores se
  reconocen por su identificación, nombre, email o código antes de crear; cada envío de la ruta histórica
  y cada conversación de chat se reconoce INDIVIDUALMENTE (ver `asegurar_ruta_historica`/`asegurar_chat`),
  así una corrida interrumpida a la mitad y repetida después no duplica lo que ya quedó completo.
  Recuperación de una interrupción a mitad de un envío: ver el docstring de `asegurar_ruta_historica`.
- SIN EFECTOS FUERA DE LO QUE ESTE SCRIPT CREÓ: la sincronización (Neo4j/Mongo/facturación) se acota por
  envío (`agregado_id`), nunca por lote general — así nunca procesa entregas pendientes de otros envíos
  (reales, del equipo) que puedan estar en la cola. La predicción de atrasos (`PrediccionService.calcular`)
  es, por diseño, un proceso de ALCANCE TODO EL SISTEMA (evalúa cualquier envío PLANIFICADO/EN_TRANSITO,
  no solo los de esta siembra): por eso este script NO la dispara; queda para la próxima vuelta del
  planificador de procesos (una instancia interna abierta, o `scripts/procesos.py`).
- Todo queda marcado con el prefijo "DEMO-SEED" (identificaciones, nombres, emails), igual criterio que
  "DEMO-MON-"/"DEMO-CHAT" ya usados en el proyecto — pero el prefijo por sí solo NO es lo que garantiza el
  aislamiento: lo que lo garantiza es que cada creación se busca antes por un campo propio verificable
  (identificación, email, código, o el número de ocurrencia embebido en la descripción de cada envío de la
  ruta histórica) y que la sincronización y la recuperación de huérfanos se acotan por id de envío, nunca
  por un patrón de texto que pudiera coincidir con datos ajenos.
  Nunca toca terceros, usuarios, envíos, sesiones o el administrador que ya existan sin ese prefijo (en
  particular, no modifica `ENV-20261103-GHP2TD`).
- Requiere un ADMINISTRADOR YA EXISTENTE (no crea ni modifica otro); la clave se pide sin eco.
- Los usuarios de demostración (OPERACIONES, COMERCIAL, dos CLIENTE y un OPERADOR_LOGISTICO) comparten una
  clave fija de DEMOSTRACIÓN (se puede cambiar con la variable de entorno SEED_DEMO_PASSWORD); no son
  credenciales reales y no deben usarse fuera de este entorno de prueba.
- `Faker` (semilla fija) solo se usa para texto decorativo (direcciones, descripciones, mensajes de chat)
  que no hace falta volver a encontrar en una corrida posterior; todo lo que el script necesita reconocer
  en una corrida posterior (identificaciones, nombres, emails, códigos) es texto fijo, no generado.

Uso:
    PYTHONPATH=src python scripts/seed_demo.py --email admin@empresa.com
    PYTHONPATH=src python scripts/seed_demo.py --email admin@empresa.com --omitir-monitoreo
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faker import Faker  # noqa: E402
from sqlalchemy import text  # noqa: E402

from logistica.core.config import load_config  # noqa: E402
from logistica.core.errors import LogisticaError  # noqa: E402
from logistica.core.logging_setup import setup_logging  # noqa: E402
from logistica.domain.acceso import Rol, RolComercial, TerceroDatos, UsuarioDatos  # noqa: E402
from logistica.domain.comercial import (  # noqa: E402
    CargoDatos,
    FacturaCompraDatos,
    PagoDatos,
    Renglon,
    SentidoPago,
)
from logistica.domain.logistica import (  # noqa: E402
    AsignacionDatos,
    ContenedorDatos,
    EnvioDatos,
    Medio,
    Prioridad,
    TipoContenedor,
    TipoUbicacion,
    TipoVehiculo,
    TramoPlan,
    UbicacionDatos,
    VehiculoDatos,
)
from logistica.domain.logistica_dtos import EnvioDTO  # noqa: E402
from logistica.repositories.connections import Connections  # noqa: E402
from logistica.repositories.postgres import unit_of_work  # noqa: E402
from logistica.repositories.postgres.outbox import FACTURACION, MONGO, NEO4J  # noqa: E402
from logistica.services.contenedor import Servicios, crear_servicios  # noqa: E402

log = logging.getLogger("seed_demo")

PASSWORD_DEMO = os.environ.get("SEED_DEMO_PASSWORD", "DemoSeed-2026!")  # solo para estas cuentas de prueba
PREFIJO = "DEMO-SEED"
OBJETIVO_RUTA_HISTORICA = 12  # AGENTS.md §10.5: mínimo 10 para la población preferida (misma ruta)
FAKE = Faker("es_AR")
FAKE.seed_instance(20261104)  # semilla fija: reproducible


def log_paso(msg: str) -> None:
    print(f"— {msg}")
    log.info(msg)


# ============================================================================ terceros, usuarios, catálogo
def asegurar_tercero(
    servicios: Servicios,
    sesion: uuid.UUID,
    *,
    razon_social: str,
    numero_identificacion: str,
    roles: set[str],
    pais_codigo: str = "AR",
    tipo_identificacion: str = "CUIT",
) -> uuid.UUID:
    existentes = servicios.terceros.listar(sesion, texto=numero_identificacion, incluir_inactivos=True)
    for t in existentes:
        if t.numero_identificacion == numero_identificacion:
            return t.id
    tercero = servicios.terceros.crear(
        sesion,
        TerceroDatos(
            razon_social, pais_codigo, tipo_identificacion, numero_identificacion,
            direccion=FAKE.address().replace("\n", ", "),
        ),
        roles,
    )
    log_paso(f"Tercero creado: {razon_social} ({numero_identificacion})")
    return tercero.id


def asegurar_ubicacion(
    servicios: Servicios, sesion: uuid.UUID, *, nombre: str, tipo: str, pais_codigo: str, ciudad: str,
    latitud: float, longitud: float,
) -> uuid.UUID:
    for u in servicios.catalogo.listar_ubicaciones(sesion, texto=nombre, incluir_inactivas=True):
        if u.nombre == nombre:
            return u.id
    ub = servicios.catalogo.crear_ubicacion(
        sesion, UbicacionDatos(nombre, tipo, pais_codigo, ciudad, f"{ciudad} ({PREFIJO})", latitud, longitud)
    )
    log_paso(f"Ubicación creada: {nombre}")
    return ub.id


def asegurar_usuario(
    servicios: Servicios, sesion: uuid.UUID, *, nombre: str, apellido: str, email: str, roles: set[str],
    tercero_id: uuid.UUID | None = None,
) -> uuid.UUID:
    # El servicio normaliza el email a minúsculas (UsuarioDatos.normalizado); comparar sin normalizar
    # tendría el mismo problema que en asegurar_vehiculo/asegurar_contenedor si alguien llama con mayúsculas.
    email_norm = email.strip().lower()
    for u in servicios.usuarios.listar(sesion):
        if u.email == email_norm:
            return u.id
    usuario = servicios.usuarios.crear(
        sesion, UsuarioDatos(nombre, apellido, email, tercero_id), roles, PASSWORD_DEMO
    )
    log_paso(f"Usuario creado: {email} ({', '.join(sorted(roles))})")
    return usuario.id


def asegurar_vehiculo(
    servicios: Servicios, sesion: uuid.UUID, *, operador_id: uuid.UUID, tipo: str, identificacion: str,
) -> uuid.UUID:
    # El servicio normaliza la identificación a MAYÚSCULAS (VehiculoDatos.normalizado); comparar sin
    # normalizar acá rompía la idempotencia si quien llama pasa minúsculas (encontrado por
    # tests/test_fase9_seed_demo.py): la búsqueda no encontraba el existente y la creación fallaba con
    # "vehículo duplicado" en vez de devolver el id ya creado.
    identificacion_norm = identificacion.strip().upper()
    for v in servicios.catalogo.listar_vehiculos(sesion, operador_id=operador_id):
        if v.identificacion == identificacion_norm:
            return v.id
    vehiculo = servicios.catalogo.crear_vehiculo(sesion, VehiculoDatos(operador_id, tipo, identificacion))
    log_paso(f"Vehículo creado: {identificacion} ({tipo})")
    return vehiculo.id


def asegurar_contenedor(
    servicios: Servicios, sesion: uuid.UUID, *, codigo: str, tipo: str, capacidad_kg: int, capacidad_m3: int,
) -> uuid.UUID:
    # Mismo motivo que en asegurar_vehiculo: ContenedorDatos.normalizado() pasa el código a MAYÚSCULAS y
    # saca espacios antes de guardarlo.
    codigo_norm = codigo.strip().upper().replace(" ", "")
    for c in servicios.catalogo.listar_contenedores(sesion, texto=codigo):
        if c.codigo_internacional == codigo_norm:
            return c.id
    contenedor = servicios.catalogo.crear_contenedor(
        sesion, ContenedorDatos(codigo, tipo, capacidad_kg, capacidad_m3)
    )
    log_paso(f"Contenedor creado: {codigo}")
    return contenedor.id


# ============================================================================ excepción documentada: reloj
def _retrasar_creacion(connections: Connections, envio_id: uuid.UUID, creado_en: datetime) -> None:
    """ÚNICA excepción a "sin SQL fuera de los repositorios" en todo el proyecto de aplicación.

    El servicio de envíos fija `envio.creado_en` en el momento real de la llamada (columna con
    DEFAULT now(), sin parámetro para elegirlo) y el CHECK `ck_envio_fechas` exige que las fechas de
    entrega sean posteriores a esa creación. Para reproducir una historia de entregas de los últimos
    meses (AGENTS.md §10.5: 180 días) sin esperar esos meses, este script mueve ÚNICAMENTE `creado_en`
    hacia atrás, apenas creado el envío y ANTES de fijar cualquier fecha de entrega — nunca se tocan
    tramos, asignaciones, auditoría, outbox ni movimientos, que siguen con el reloj real de cada acción.
    El rol de ejecución (`logistica_app_rw`) tiene UPDATE sobre `envio` (no está en la lista de tablas
    inmutables de `900_grants.sql`), así que no hace falta una identidad distinta.

    Alcance estrictamente limitado a UN envío: el `UPDATE` filtra por `id = :id` (la clave primaria, no un
    patrón ni un rango) y SOLO se llama con el `envio.id` que `servicios.envios.crear(...)` acaba de
    devolver en la misma función que llama a esta — nunca con un id leído de otro lado. Se verifica que la
    sentencia afectó EXACTAMENTE una fila (ni cero, por un id inexistente o ya no vigente, ni más de una,
    que no podría pasar por ser la clave primaria pero se comprueba de todas formas para no confiar en eso
    silenciosamente).
    """
    with unit_of_work(connections) as tx:
        resultado = tx.execute(
            text("UPDATE envio SET creado_en = :c WHERE id = :id"), {"c": creado_en, "id": envio_id}
        )
        if resultado.rowcount != 1:
            raise RuntimeError(
                f"_retrasar_creacion debía afectar exactamente 1 envío ({envio_id}); afectó "
                f"{resultado.rowcount}. Se aborta: no se confía en un resultado inesperado."
            )



# ============================================================================ ruta histórica (predicción)
def crear_envio_ruta(
    servicios: Servicios,
    connections: Connections,
    sesion_ops: uuid.UUID,
    sesion_com: uuid.UUID,
    *,
    cliente_id: uuid.UUID,
    origen_id: uuid.UUID,
    destino_id: uuid.UUID,
    operador_id: uuid.UUID,
    vehiculo_id: uuid.UUID,
    contenedor_id: uuid.UUID,
    salida_real: datetime,
    duracion_dias: int,
    atraso_dias: int,
    peso_kg: int,
    volumen_m3: int,
    descripcion_carga: str,
) -> tuple[uuid.UUID, bool]:
    """Crea, planifica, ejecuta y entrega un envío con fechas HISTÓRICAS. Devuelve (envio_id, llegó_tarde)."""
    creado_en = salida_real - timedelta(days=5)
    llegada_estimada = salida_real + timedelta(days=duracion_dias)
    llegada_real = llegada_estimada + timedelta(days=atraso_dias)

    envio = servicios.envios.crear(
        sesion_ops,
        EnvioDatos(cliente_id, origen_id, destino_id, descripcion_carga, peso_kg, volumen_m3, Prioridad.NORMAL),
    )
    _retrasar_creacion(connections, envio.id, creado_en)  # ver _retrasar_creacion

    servicios.comercial.crear_cargo(
        sesion_com, envio.id,
        CargoDatos("Flete marítimo Buenos Aires-Rotterdam", 1, 4500, "USD"),
    )

    detalle = servicios.envios.definir_tramos(
        sesion_ops, envio.id,
        [TramoPlan(origen_id, destino_id, Medio.MARITIMO, operador_id, salida_real, llegada_estimada,
                   vehiculo_id, 3800, "USD")],
    )
    tramo_id = detalle.tramos[0].id
    servicios.envios.asignar_contenedor(
        sesion_ops, envio.id,
        AsignacionDatos(
            contenedor_id, peso_kg, volumen_m3,
            salida_real - timedelta(days=1), llegada_real + timedelta(days=10),
        ),
    )
    servicios.envios.planificar(sesion_ops, envio.id)
    servicios.envios.iniciar_tramo(sesion_ops, tramo_id, salida_real)
    servicios.envios.finalizar_tramo(sesion_ops, tramo_id, llegada_real)
    servicios.envios.confirmar_entrega(sesion_ops, envio.id, llegada_real)
    return envio.id, atraso_dias > 0


def _dias_atraso(indice: int) -> int:
    """3 de 12 ocurrencias llegan tarde (índices 3, 7 y 11): ~25 % de atrasos, un riesgo no trivial para la
    predicción (AGENTS.md §10.5) pero siempre el mismo según el índice, nunca al azar — así una recuperación
    reproduce exactamente lo que habría hecho la corrida original."""
    return 5 if indice in (3, 7, 11) else 0


_MARCADOR_OCURRENCIA = re.compile(r"ocurrencia (\d+)/(\d+)\b")


def asegurar_ruta_historica(
    servicios: Servicios, connections: Connections, sesion_ops: uuid.UUID, sesion_com: uuid.UUID, *,
    cliente_id: uuid.UUID, cliente_razon_social: str, origen_id: uuid.UUID, destino_id: uuid.UUID,
    operador_id: uuid.UUID, vehiculo_id: uuid.UUID, contenedores: tuple[uuid.UUID, uuid.UUID],
) -> list[uuid.UUID]:
    """Siembra `OBJETIVO_RUTA_HISTORICA` entregas en la misma ruta (para que la predicción de atrasos tenga
    muestra). Cada envío se identifica por el número de OCURRENCIA que lleva en `descripcion_carga` — el
    único campo de envío que el script puede usar como marca propia estable: el código es aleatorio
    (`generar_codigo_envio`) y el servicio nunca deja elegir el id (`uuid.uuid4()` interno).

    Recuperación tras una corrida interrumpida, por ocurrencia:
    - ENTREGADO: ya está completa, no se toca, cuenta para el objetivo.
    - BORRADOR/PLANIFICADO (se interrumpió antes de zarpar): se CANCELA (S8 lo permite en estos estados,
      libera la reserva del contenedor) y se reemplaza por una ocurrencia nueva con las mismas fechas.
    - EN_TRANSITO (se interrumpió después de zarpar, antes de confirmar la entrega): NO se puede cancelar
      (S8: "un envío en tránsito no se cancela"). En vez de abandonarlo, se TERMINA en el mismo lugar donde
      quedó (el estado del tramo dice si falta `finalizar_tramo` y/o `confirmar_entrega`), usando su propia
      `salida_real_en` ya registrada — nunca una fecha recalculada — para que la entrega real coincida con
      la que le correspondía a esa ocurrencia.
    Ninguna ocurrencia existente se duplica ni se recrea con otro índice.
    """
    todos = servicios.envios.listar(sesion_ops, texto=cliente_razon_social)
    por_indice: dict[int, EnvioDTO] = {}
    for e in todos:
        m = _MARCADOR_OCURRENCIA.search(e.descripcion_carga)
        if m and int(m.group(2)) == OBJETIVO_RUTA_HISTORICA:
            indice = int(m.group(1)) - 1
            actual = por_indice.get(indice)
            # Si por algún motivo hay más de un envío con el mismo índice (no debería pasar: cada índice
            # faltante se crea una sola vez por corrida), nos quedamos con el ENTREGADO si lo hay.
            if actual is None or (e.estado == "ENTREGADO" and actual.estado != "ENTREGADO"):
                por_indice[indice] = e

    entregadas = {i: e for i, e in por_indice.items() if e.estado == "ENTREGADO"}
    if len(entregadas) >= OBJETIVO_RUTA_HISTORICA:
        log_paso(
            f"Ruta histórica Buenos Aires-Rotterdam: ya hay {len(entregadas)} entregas identificadas por "
            f"ocurrencia; se omite (idempotente)."
        )
        return [entregadas[i].id for i in sorted(entregadas)]

    # Huérfanos de una corrida anterior interrumpida.
    for i in sorted(por_indice):
        e = por_indice[i]
        if e.estado in ("BORRADOR", "PLANIFICADO"):
            for cargo in servicios.comercial.cargos(sesion_com, e.id):
                if cargo.estado == "PENDIENTE":
                    servicios.comercial.cancelar_cargo(sesion_com, cargo.id)
            servicios.envios.cancelar(
                sesion_ops, e.id, "Demo: recuperación de una siembra interrumpida; se reemplaza."
            )
            log_paso(f"Huérfano de una corrida anterior cancelado (ocurrencia {i + 1}): {e.codigo}")
            del por_indice[i]
        elif e.estado == "EN_TRANSITO":
            llegada_real = _completar_huerfano_en_transito(servicios, sesion_ops, e, i)
            entregadas[i] = replace(e, estado="ENTREGADO", entrega_real_en=llegada_real)
            log_paso(f"Huérfano de una corrida anterior completado (ocurrencia {i + 1}): {e.codigo}")

    faltan = [i for i in range(OBJETIVO_RUTA_HISTORICA) if i not in entregadas]
    if not faltan:
        return [entregadas[i].id for i in sorted(entregadas)]
    log_paso(f"Sembrando {len(faltan)} entrega(s) histórica(s) Buenos Aires-Rotterdam que faltan...")

    # Ancla de fechas: si ya hay alguna ocurrencia (de esta corrida o de una interrumpida), se reconstruye
    # "ahora" a partir de SU fecha real de salida, para continuar la MISMA secuencia sin solapar reservas
    # de contenedor, sin importar cuánto tiempo real pasó entre corridas. Si no hay ninguna, es la primera
    # corrida: se ancla a "ahora" (174 días atrás la más antigua, dentro de la ventana de 180 días de la
    # predicción, AGENTS.md §10.5).
    referencia = next(iter(entregadas.items()), None) or next(iter(por_indice.items()), None)
    if referencia is not None:
        j, e_ref = referencia
        salida_j = servicios.envios.obtener(sesion_ops, e_ref.id).tramos[0].salida_estimada_en
        ahora = salida_j + timedelta(days=174 - j * 14)
    else:
        ahora = datetime.now(timezone.utc)

    ids: list[uuid.UUID] = [entregadas[i].id for i in sorted(entregadas)]
    atrasados = 0
    for i in faltan:
        # Más antiguo primero, alternando DOS contenedores (AGENTS.md §10.2: sin reservas superpuestas). Con
        # 18 días de travesía + hasta 5 de atraso, una separación de 14 días entre salidas NO alcanza para
        # reutilizar el MISMO contenedor (el anterior seguiría EN_USO); alternar dos contenedores deja 28
        # días de ciclo por contenedor, de sobra.
        dias_atras = 174 - i * 14
        salida = (ahora - timedelta(days=dias_atras)).replace(hour=9, minute=0, second=0, microsecond=0)
        atraso = _dias_atraso(i)
        envio_id, tarde = crear_envio_ruta(
            servicios, connections, sesion_ops, sesion_com,
            cliente_id=cliente_id, origen_id=origen_id, destino_id=destino_id, operador_id=operador_id,
            vehiculo_id=vehiculo_id, contenedor_id=contenedores[i % 2], salida_real=salida, duracion_dias=18,
            atraso_dias=atraso, peso_kg=18_000, volumen_m3=25,
            descripcion_carga=f"{PREFIJO} ruta histórica BA-Rotterdam, ocurrencia {i + 1:02d}/"
                               f"{OBJETIVO_RUTA_HISTORICA} ({FAKE.bs()})",
        )
        ids.append(envio_id)
        if tarde:
            atrasados += 1
    log_paso(f"Ruta histórica sembrada: {len(ids)} entregas en total, {len(faltan)} nuevas "
             f"({atrasados} con atraso de las nuevas).")
    return ids


def _completar_huerfano_en_transito(
    servicios: Servicios, sesion_ops: uuid.UUID, envio: EnvioDTO, indice: int
) -> datetime:
    """Termina en el lugar exacto donde quedó un envío EN_TRANSITO huérfano (S8: no se puede cancelar en
    tránsito). Usa la `salida_real_en` YA REGISTRADA del tramo (nunca una recalculada) más la duración y el
    atraso fijos de esta ocurrencia, así la entrega resulta igual a la que esa ocurrencia habría tenido."""
    detalle = servicios.envios.obtener(sesion_ops, envio.id)
    tramo = detalle.tramos[0]
    llegada_real = tramo.salida_real_en + timedelta(days=18 + _dias_atraso(indice))
    if tramo.estado == "EN_CURSO":
        servicios.envios.finalizar_tramo(sesion_ops, tramo.id, llegada_real)
    servicios.envios.confirmar_entrega(sesion_ops, envio.id, llegada_real)
    return llegada_real



# ============================================================================ variedad de estados
def asegurar_envio_variedad(
    servicios: Servicios, sesion_ops: uuid.UUID, sesion_com: uuid.UUID, *, cliente_id: uuid.UUID,
    cliente_razon_social: str, origen_id: uuid.UUID, destino_id: uuid.UUID, operador_id: uuid.UUID,
    vehiculo_id: uuid.UUID, contenedor_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Un BORRADOR, un CANCELADO (ruta terrestre) y un PLANIFICADO en la MISMA ruta marítima (para que la
    predicción tenga un objetivo con historial)."""
    resultado: dict[str, uuid.UUID] = {}

    def _existe(estado: str) -> uuid.UUID | None:
        hallados = servicios.envios.listar(sesion_ops, estado=estado, texto=cliente_razon_social)
        return hallados[0].id if hallados else None

    # BORRADOR: Zárate -> San Pablo, terrestre, con tramo definido pero sin contenedor ni planificar.
    existente = _existe("BORRADOR")
    if existente:
        resultado["borrador"] = existente
    else:
        envio = servicios.envios.crear(
            sesion_ops,
            EnvioDatos(
                cliente_id, origen_id, destino_id,
                f"{PREFIJO} envío en preparación, recorrido terrestre AR-BR ({FAKE.catch_phrase()})",
                8_000, 15, Prioridad.NORMAL,
            ),
        )
        ahora = datetime.now(timezone.utc)
        servicios.envios.definir_tramos(
            sesion_ops, envio.id,
            [TramoPlan(origen_id, destino_id, Medio.TERRESTRE, operador_id,
                       ahora + timedelta(days=3), ahora + timedelta(days=9), vehiculo_id, 1200, "ARS")],
        )
        resultado["borrador"] = envio.id
        log_paso(f"Envío BORRADOR creado: {envio.codigo}")

    # CANCELADO: mismo recorrido terrestre, pero se cancela con motivo.
    existente = _existe("CANCELADO")
    if existente:
        resultado["cancelado"] = existente
    else:
        envio = servicios.envios.crear(
            sesion_ops,
            EnvioDatos(
                cliente_id, origen_id, destino_id,
                f"{PREFIJO} envío cancelado por el cliente ({FAKE.catch_phrase()})", 6_500, 12,
                Prioridad.BAJA,
            ),
        )
        ahora = datetime.now(timezone.utc)
        servicios.envios.definir_tramos(
            sesion_ops, envio.id,
            [TramoPlan(origen_id, destino_id, Medio.TERRESTRE, operador_id,
                       ahora + timedelta(days=4), ahora + timedelta(days=10), vehiculo_id, 1100, "ARS")],
        )
        servicios.envios.cancelar(sesion_ops, envio.id, "Demo: el cliente desistió del envío.")
        resultado["cancelado"] = envio.id
        log_paso(f"Envío CANCELADO creado: {envio.codigo}")

    return resultado


def asegurar_envio_planificado_en_ruta(
    servicios: Servicios, sesion_ops: uuid.UUID, *, cliente_id: uuid.UUID, cliente_razon_social: str,
    origen_id: uuid.UUID, destino_id: uuid.UUID, operador_id: uuid.UUID, vehiculo_id: uuid.UUID,
    contenedor_id: uuid.UUID,
) -> uuid.UUID:
    """PLANIFICADO en la ruta Buenos Aires-Rotterdam: es el "objetivo" que la predicción de atrasos evalúa
    con el historial de la ruta histórica (AGENTS.md §10.5)."""
    hallados = servicios.envios.listar(sesion_ops, estado="PLANIFICADO", texto=cliente_razon_social)
    if hallados:
        return hallados[0].id
    ahora = datetime.now(timezone.utc)
    salida = ahora + timedelta(days=5)
    envio = servicios.envios.crear(
        sesion_ops,
        EnvioDatos(
            cliente_id, origen_id, destino_id,
            f"{PREFIJO} próximo envío Buenos Aires-Rotterdam ({FAKE.catch_phrase()})", 17_500, 24,
            Prioridad.NORMAL,
        ),
    )
    detalle = servicios.envios.definir_tramos(
        sesion_ops, envio.id,
        [TramoPlan(origen_id, destino_id, Medio.MARITIMO, operador_id, salida,
                   salida + timedelta(days=18), vehiculo_id, 3800, "USD")],
    )
    servicios.envios.asignar_contenedor(
        sesion_ops, envio.id,
        AsignacionDatos(contenedor_id, 17_500, 24, salida - timedelta(days=1), salida + timedelta(days=35)),
    )
    servicios.envios.planificar(sesion_ops, envio.id)
    log_paso(f"Envío PLANIFICADO creado en la ruta histórica: {envio.codigo}")
    return envio.id


# ============================================================================ incidente manual
def asegurar_incidente(servicios: Servicios, sesion_ops: uuid.UUID, envio_id: uuid.UUID) -> None:
    # solo_abiertos=False: el incidente de esta demo queda RESUELTO al terminar, así que en una corrida
    # posterior hay que verlo igual para no crear otro.
    incidentes = servicios.monitoreo.incidentes(sesion_ops, envio_id=envio_id, solo_abiertos=False)
    if incidentes:
        return
    incidente = servicios.monitoreo.crear_incidente(
        sesion_ops, envio_id, tipo="OTRO", severidad="MEDIA",
        descripcion=f"{PREFIJO}: demora en la llegada; se avisó al cliente y se revisó con el operador.",
    )
    servicios.monitoreo.tomar_incidente(sesion_ops, incidente.id)
    servicios.monitoreo.resolver_incidente(
        sesion_ops, incidente.id, "Demo: se confirmó con el operador que fue un desvío por mal tiempo."
    )
    log_paso("Incidente manual creado y resuelto sobre una entrega con atraso.")


# ============================================================================ comercial: cobros y compra
def asegurar_cobros(servicios: Servicios, sesion_com: uuid.UUID, *, cliente_id: uuid.UUID) -> None:
    if servicios.comercial.pagos(sesion_com, tercero_id=cliente_id):
        log_paso("Cobros de demostración: ya existen pagos para el cliente; se omite.")
        return
    facturas = [
        f for f in servicios.comercial.facturas(sesion_com, sentido="VENTA", estado="EMITIDA")
        if f.tercero_id == cliente_id
    ]
    if len(facturas) < 2:
        log_paso("Cobros de demostración: todavía no hay (o faltan) facturas emitidas; se omite por ahora.")
        return
    facturas.sort(key=lambda f: f.emitida_en or datetime.min.replace(tzinfo=timezone.utc))
    primera, segunda = facturas[0], facturas[1]
    ahora = datetime.now(timezone.utc)

    pago_total = servicios.comercial.registrar_pago(
        sesion_com,
        PagoDatos(cliente_id, SentidoPago.COBRO, ahora, primera.importe_total, primera.moneda_codigo,
                   "TRANSFERENCIA", f"{PREFIJO}-COBRO-01"),
        uuid.uuid4(),
    )
    servicios.comercial.aplicar_pago(sesion_com, pago_total.id, primera.id, primera.importe_total)
    log_paso(f"Cobro total registrado y aplicado a la factura {primera.codigo}.")

    mitad = (segunda.importe_total / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    pago_parcial = servicios.comercial.registrar_pago(
        sesion_com,
        PagoDatos(cliente_id, SentidoPago.COBRO, ahora, mitad, segunda.moneda_codigo, "TRANSFERENCIA",
                   f"{PREFIJO}-COBRO-02"),
        uuid.uuid4(),
    )
    servicios.comercial.aplicar_pago(sesion_com, pago_parcial.id, segunda.id, mitad)
    log_paso(f"Cobro parcial registrado y aplicado a la factura {segunda.codigo} (demuestra saldo pendiente).")


def asegurar_compra(servicios: Servicios, sesion_com: uuid.UUID, *, proveedor_id: uuid.UUID) -> None:
    if servicios.comercial.pagos(sesion_com, tercero_id=proveedor_id):
        log_paso("Factura de compra de demostración: ya existe; se omite.")
        return
    ahora = datetime.now(timezone.utc)
    try:
        factura = servicios.comercial.registrar_factura_compra(
            sesion_com,
            FacturaCompraDatos(
                proveedor_id, "A", f"{PREFIJO}-0001", ahora, ahora + timedelta(days=30), "ARS",
                [Renglon(f"{PREFIJO} repuestos para mantenimiento de contenedores", 1, 85_000)],
            ),
        )
    except LogisticaError as exc:
        log.warning("No se pudo registrar la factura de compra de demostración: %s", exc.user_message)
        return
    pago = servicios.comercial.registrar_pago(
        sesion_com,
        PagoDatos(proveedor_id, SentidoPago.PAGO, ahora, factura.importe_total, "ARS", "TRANSFERENCIA",
                   f"{PREFIJO}-PAGO-01"),
        uuid.uuid4(),
    )
    servicios.comercial.aplicar_pago(sesion_com, pago.id, factura.id, factura.importe_total)
    log_paso(f"Factura de compra {factura.codigo} registrada, pagada y aplicada.")


# ============================================================================ chat
def asegurar_chat(
    servicios: Servicios, *, sesion_ops: uuid.UUID, sesion_com: uuid.UUID, sesion_cli1: uuid.UUID,
    sesion_cli2: uuid.UUID, usuario_ops: uuid.UUID, usuario_com: uuid.UUID, usuario_cli1: uuid.UUID,
    usuario_cli2: uuid.UUID,
) -> None:
    """Cada conversación se asegura por separado (no hay un único "ya existe" para todo el bloque): una
    corrida interrumpida entre dos conversaciones no debe recrear la primera."""

    def _buscar(vista, tipo: str, *, nombre: str | None = None, creada_por_id: uuid.UUID | None = None):
        for c in vista.conversaciones:
            if c.tipo != tipo:
                continue
            if nombre is not None and c.nombre != nombre:
                continue
            if creada_por_id is not None and c.creada_por_id != creada_por_id:
                continue
            return c
        return None

    vista_ops = servicios.comunicacion.vista(sesion_ops)
    privado_interno = _buscar(vista_ops, "PRIVADA", creada_por_id=usuario_ops)
    if privado_interno is None:
        ident = servicios.comunicacion.crear(sesion_ops, "PRIVADA", "", (usuario_com,))
        servicios.comunicacion.enviar(
            sesion_ops, ident, uuid.uuid4(),
            f"{PREFIJO}: hola, ¿cómo va la facturación de la ruta Buenos Aires-Rotterdam?",
        )
        servicios.comunicacion.enviar(
            sesion_com, ident, uuid.uuid4(),
            f"{PREFIJO}: al día, ya salieron las facturas de las últimas entregas.",
        )
        log_paso("Chat privado interno (Operaciones-Comercial) creado.")
    else:
        log_paso("Chat privado interno: ya existe; se omite.")

    nombre_grupo = f"{PREFIJO} Equipo Operativo"
    grupo = _buscar(vista_ops, "GRUPAL", nombre=nombre_grupo)
    if grupo is None:
        ident = servicios.comunicacion.crear(sesion_ops, "GRUPAL", nombre_grupo, (usuario_com,))
        servicios.comunicacion.enviar(
            sesion_ops, ident, uuid.uuid4(), f"{PREFIJO}: equipo, arrancamos el seguimiento de la demo."
        )
        servicios.comunicacion.enviar(sesion_com, ident, uuid.uuid4(), f"{PREFIJO}: recibido, gracias.")
        log_paso(f"Grupo {nombre_grupo!r} creado.")
    else:
        log_paso(f"Grupo {nombre_grupo!r}: ya existe; se omite.")

    nombre_canal = f"{PREFIJO} Canal Interno"
    canal = _buscar(vista_ops, "PUBLICA", nombre=nombre_canal) or next(
        (c for c in vista_ops.publicas if c.nombre == nombre_canal), None
    )
    if canal is None:
        ident = servicios.comunicacion.crear(sesion_ops, "PUBLICA", nombre_canal, ())
        servicios.comunicacion.enviar(
            sesion_ops, ident, uuid.uuid4(), f"{PREFIJO}: mensaje ANTES de que Comercial se incorpore."
        )
        log_paso(f"Canal público {nombre_canal!r} creado.")
    else:
        ident = canal.id
        log_paso(f"Canal público {nombre_canal!r}: ya existe; se omite la creación.")
    vista_com = servicios.comunicacion.vista(sesion_com)
    if not any(c.id == ident for c in vista_com.conversaciones):
        servicios.comunicacion.unirse(sesion_com, ident)
        servicios.comunicacion.enviar(
            sesion_com, ident, uuid.uuid4(),
            f"{PREFIJO}: mensaje DESPUÉS de incorporarme (no debería verse el anterior).",
        )
        log_paso("Comercial incorporado al canal público.")
    else:
        log_paso("Comercial ya estaba incorporado al canal público; se omite.")

    vista_cli1 = servicios.comunicacion.vista(sesion_cli1)
    privado_cliente = _buscar(vista_cli1, "PRIVADA", creada_por_id=usuario_cli1)
    if privado_cliente is None:
        ident = servicios.comunicacion.crear(sesion_cli1, "PRIVADA", "", (usuario_cli2,))
        servicios.comunicacion.enviar(
            sesion_cli1, ident, uuid.uuid4(), f"{PREFIJO}: ¿viste el estado del envío en Trazabilidad?"
        )
        servicios.comunicacion.enviar(sesion_cli2, ident, uuid.uuid4(), f"{PREFIJO}: sí, llegó bien.")
        log_paso("Chat privado entre clientes creado.")
    else:
        log_paso("Chat privado entre clientes: ya existe; se omite.")



# ============================================================================ main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="administrador YA EXISTENTE (la clave se pide sin eco)")
    parser.add_argument("--omitir-monitoreo", action="store_true",
                        help="no llama a MonitoreoService.preparar_demo (por si ya corriste esa demo a mano)")
    args = parser.parse_args()

    config = load_config()
    setup_logging(config.log_dir, config.log_level)
    connections = Connections(config)
    sesiones_abiertas: list[uuid.UUID] = []
    try:
        servicios = crear_servicios(config, connections, uuid.uuid4())
        principal_admin = servicios.acceso.login(
            args.email, getpass.getpass("Contraseña del administrador: "), "seed_demo"
        )
        sesion_admin = principal_admin.sesion_id
        sesiones_abiertas.append(sesion_admin)
        log_paso(f"Conectado como {principal_admin.email} ({', '.join(sorted(principal_admin.roles))}).")

        # --- terceros -----------------------------------------------------------------------------------
        cli01 = asegurar_tercero(
            servicios, sesion_admin, razon_social="Exportadora Demo Seed Norte SA",
            numero_identificacion="30-71111111-1", roles={RolComercial.CLIENTE},
        )
        cli02 = asegurar_tercero(
            servicios, sesion_admin, razon_social="Importadora Demo Seed Sur SRL",
            numero_identificacion="30-71111112-2", roles={RolComercial.CLIENTE},
        )
        ope01 = asegurar_tercero(
            servicios, sesion_admin, razon_social="Transportes Maritimos Demo Seed SA",
            numero_identificacion="30-72222221-1", roles={RolComercial.OPERADOR},
        )
        ope02 = asegurar_tercero(
            servicios, sesion_admin, razon_social="Logistica Terrestre Demo Seed SRL",
            numero_identificacion="30-72222222-2", roles={RolComercial.OPERADOR},
        )
        pro01 = asegurar_tercero(
            servicios, sesion_admin, razon_social="Repuestos Demo Seed SRL",
            numero_identificacion="30-73333331-1", roles={RolComercial.PROVEEDOR},
        )

        # --- ubicaciones ----------------------------------------------------------------------------------
        ub_ba = asegurar_ubicacion(
            servicios, sesion_admin, nombre="Puerto de Buenos Aires (Demo Seed)", tipo=TipoUbicacion.PUERTO,
            pais_codigo="AR", ciudad="Buenos Aires", latitud=-34.603722, longitud=-58.381592,
        )
        ub_rt = asegurar_ubicacion(
            servicios, sesion_admin, nombre="Puerto de Rotterdam (Demo Seed)", tipo=TipoUbicacion.PUERTO,
            pais_codigo="NL", ciudad="Rotterdam", latitud=51.9225, longitud=4.47917,
        )
        ub_zarate = asegurar_ubicacion(
            servicios, sesion_admin, nombre="Depósito Demo Seed Zárate", tipo=TipoUbicacion.DEPOSITO,
            pais_codigo="AR", ciudad="Zárate", latitud=-34.0944, longitud=-59.0279,
        )
        ub_sanpablo = asegurar_ubicacion(
            servicios, sesion_admin, nombre="Depósito Demo Seed San Pablo", tipo=TipoUbicacion.DEPOSITO,
            pais_codigo="BR", ciudad="São Paulo", latitud=-23.5505, longitud=-46.6333,
        )

        # --- usuarios por perfil ----------------------------------------------------------------------
        email_ops = "seed.operaciones@demo.local"
        email_com = "seed.comercial@demo.local"
        email_cli1 = "seed.cliente.norte@demo.local"
        email_cli2 = "seed.cliente.norte2@demo.local"
        usuario_ops = asegurar_usuario(
            servicios, sesion_admin, nombre="Operaciones", apellido="Demo Seed",
            email=email_ops, roles={Rol.OPERACIONES},
        )
        usuario_com = asegurar_usuario(
            servicios, sesion_admin, nombre="Comercial", apellido="Demo Seed",
            email=email_com, roles={Rol.COMERCIAL},
        )
        usuario_cli1 = asegurar_usuario(
            servicios, sesion_admin, nombre="Cliente Uno", apellido="Demo Seed",
            email=email_cli1, roles={Rol.CLIENTE}, tercero_id=cli01,
        )
        usuario_cli2 = asegurar_usuario(
            servicios, sesion_admin, nombre="Cliente Dos", apellido="Demo Seed",
            email=email_cli2, roles={Rol.CLIENTE}, tercero_id=cli01,
        )
        asegurar_usuario(
            servicios, sesion_admin, nombre="Operador", apellido="Demo Seed",
            email="seed.operador.maritimo@demo.local", roles={Rol.OPERADOR_LOGISTICO}, tercero_id=ope01,
        )

        # --- catálogo: vehículos y contenedores -----------------------------------------------------------
        buque = asegurar_vehiculo(
            servicios, sesion_admin, operador_id=ope01, tipo=TipoVehiculo.BARCO,
            identificacion=f"{PREFIJO}-BUQ-01",
        )
        camion = asegurar_vehiculo(
            servicios, sesion_admin, operador_id=ope02, tipo=TipoVehiculo.CAMION,
            identificacion=f"{PREFIJO}-CAM-01",
        )
        cont_maritimo = asegurar_contenedor(
            servicios, sesion_admin, codigo=f"{PREFIJO}CONT0001", tipo=TipoContenedor.SECO,
            capacidad_kg=20_000, capacidad_m3=30,
        )
        cont_maritimo2 = asegurar_contenedor(
            servicios, sesion_admin, codigo=f"{PREFIJO}CONT0003", tipo=TipoContenedor.SECO,
            capacidad_kg=20_000, capacidad_m3=30,
        )
        cont_terrestre = asegurar_contenedor(
            servicios, sesion_admin, codigo=f"{PREFIJO}CONT0002", tipo=TipoContenedor.SECO,
            capacidad_kg=12_000, capacidad_m3=20,
        )

        # --- ruta histórica + variedad de estados (para la predicción de atrasos) -----------------------
        ids_ruta = asegurar_ruta_historica(
            servicios, connections, sesion_admin, sesion_admin, cliente_id=cli01,
            cliente_razon_social="Exportadora Demo Seed Norte SA", origen_id=ub_ba, destino_id=ub_rt,
            operador_id=ope01, vehiculo_id=buque, contenedores=(cont_maritimo, cont_maritimo2),
        )
        id_planificado = asegurar_envio_planificado_en_ruta(
            servicios, sesion_admin, cliente_id=cli01, cliente_razon_social="Exportadora Demo Seed Norte SA",
            origen_id=ub_ba, destino_id=ub_rt, operador_id=ope01, vehiculo_id=buque,
            contenedor_id=cont_maritimo,
        )
        ids_variedad = asegurar_envio_variedad(
            servicios, sesion_admin, sesion_admin, cliente_id=cli02,
            cliente_razon_social="Importadora Demo Seed Sur SRL", origen_id=ub_zarate,
            destino_id=ub_sanpablo, operador_id=ope02, vehiculo_id=camion, contenedor_id=cont_terrestre,
        )
        envios_sembrados: list[uuid.UUID] = [*ids_ruta, id_planificado, *ids_variedad.values()]

        # --- incidente manual sobre una entrega con atraso -------------------------------------------------
        atrasados = [
            e.id for e in servicios.envios.listar(sesion_admin, estado="ENTREGADO",
                                                   texto="Exportadora Demo Seed Norte SA")
            if e.entrega_real_en and e.entrega_comprometida_en and e.entrega_real_en > e.entrega_comprometida_en
        ]
        if atrasados:
            asegurar_incidente(servicios, sesion_admin, atrasados[0])

        # --- monitoreo IoT (reutiliza la demo ya probada de la fase 4) -----------------------------------
        if args.omitir_monitoreo:
            log_paso("Monitoreo: se omite por --omitir-monitoreo.")
        elif any(e.codigo.startswith("DEMO-MON-") for e in servicios.envios.listar(sesion_admin, texto="DEMO-MON-")):
            log_paso("Monitoreo: ya existe un envío DEMO-MON-...; se omite (idempotente).")
        else:
            envio_mon = servicios.monitoreo.preparar_demo(sesion_admin, "completo")
            envios_sembrados.append(envio_mon)
            log_paso(f"Demo de monitoreo creada: envío {envio_mon} (escenario 'completo').")

        # --- sincronización: SOLO los envíos de esta siembra, nunca el resto de la cola ------------------
        # `agregado_id` acota `procesar()` a un envío puntual (ver OutboxRepository.reclamar): así nunca se
        # tocan entregas pendientes de otros envíos (reales, del equipo) que puedan estar en la cola. Sin
        # esto, un `procesar(destino, limite=N)` sin acotar reclamaría CUALQUIER entrega pendiente del
        # sistema, no solo las de esta siembra.
        log_paso(f"Procesando la outbox de los {len(envios_sembrados)} envío(s) sembrados (Neo4j, Mongo y "
                 f"facturación; acotado por envío)...")
        for envio_id in envios_sembrados:
            for destino in (NEO4J, MONGO, FACTURACION):
                for _ in range(5):
                    resultado = servicios.sincronizacion.procesar(destino, limite=10, agregado_id=envio_id)
                    if resultado.procesados == 0 and resultado.pospuestos == 0:
                        break
        log_paso("Outbox de la siembra procesada.")

        # --- comercial: cobros sobre el cliente y una compra al proveedor --------------------------------
        asegurar_cobros(servicios, sesion_admin, cliente_id=cli01)
        asegurar_compra(servicios, sesion_admin, proveedor_id=pro01)

        # --- predicción de atrasos: NO se dispara desde este script ---------------------------------------
        # `PrediccionService.calcular()` es un proceso TÉCNICO de alcance TODO EL SISTEMA (evalúa todos los
        # envíos PLANIFICADO/EN_TRANSITO existentes, no solo los de esta siembra; así está diseñado, es el
        # mismo método que corre la fase 8 una vez al día). En la base de desarrollo real puede haber otros
        # envíos del equipo en ese estado: dispararlo desde acá les agregaría una fila de predicción que
        # nadie pidió. Es un efecto aditivo e idempotente (nunca modifica el envío), pero igual queda fuera
        # de este script por alcance: la predicción del envío PLANIFICADO que sembramos aparece sola la
        # próxima vez que una instancia interna esté abierta (cada 60 s) o con:
        #     PYTHONPATH=src python scripts/procesos.py
        log_paso("Predicción de atrasos: no se calcula desde este script (ver comentario; es un proceso de "
                 "alcance todo el sistema). Se calculará sola con una instancia abierta o con "
                 "scripts/procesos.py.")

        # --- chat: requiere sesión propia de cada participante -------------------------------------------
        p_ops = servicios.acceso.login(email_ops, PASSWORD_DEMO, "seed_demo")
        p_com = servicios.acceso.login(email_com, PASSWORD_DEMO, "seed_demo")
        p_cli1 = servicios.acceso.login(email_cli1, PASSWORD_DEMO, "seed_demo")
        p_cli2 = servicios.acceso.login(email_cli2, PASSWORD_DEMO, "seed_demo")
        for p in (p_ops, p_com, p_cli1, p_cli2):
            sesiones_abiertas.append(p.sesion_id)
        asegurar_chat(
            servicios, sesion_ops=p_ops.sesion_id, sesion_com=p_com.sesion_id, sesion_cli1=p_cli1.sesion_id,
            sesion_cli2=p_cli2.sesion_id, usuario_ops=usuario_ops, usuario_com=usuario_com,
            usuario_cli1=usuario_cli1, usuario_cli2=usuario_cli2,
        )

        print()
        log_paso("Datos de demostración listos. Iniciá sesión con los usuarios seed.*@demo.local (o el "
                 "administrador) para recorrerlos desde la ventana.")
        return 0
    except LogisticaError as exc:
        print(f"No se pudo completar la siembra: {exc.user_message}", file=sys.stderr)
        log.exception("seed_demo falló")
        return 1
    finally:
        for sesion_id in sesiones_abiertas:
            try:
                servicios.acceso.logout(sesion_id)  # type: ignore[possibly-undefined]
            except Exception:  # noqa: BLE001 — no interrumpir el cierre por una sesión ya vencida
                pass
        connections.close()


if __name__ == "__main__":
    sys.exit(main())
