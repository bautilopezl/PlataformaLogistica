"""Pruebas de integridad de PostgreSQL (restricciones, exclusiones, triggers).

Requieren una base con el DDL aplicado (scripts/setup_databases.py). Se activan con:

    LOGISTICA_TEST_PG="host=localhost port=5432 dbname=logistica user=... password=..." pytest tests

Cada prueba corre dentro de una transacción que se revierte al terminar: no deja datos.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest

CONNINFO = os.environ.get("LOGISTICA_TEST_PG")
pytestmark = pytest.mark.skipif(not CONNINFO, reason="definir LOGISTICA_TEST_PG para correr pruebas de integración")

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def h(n: int) -> datetime:
    """T0 + n horas."""
    return T0 + timedelta(hours=n)


@pytest.fixture()
def conn():
    c = psycopg.connect(CONNINFO)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def fails(conn, query: str, params=()):
    """True si la sentencia viola una restricción (se revierte a un savepoint)."""
    try:
        with conn.transaction():
            conn.execute(query, params)
    except psycopg.errors.DatabaseError as exc:
        # IntegrityError cubre CHECK/UNIQUE/EXCLUDE/FK, y los check_violation lanzados por triggers.
        if os.environ.get("LOGISTICA_DEBUG"):
            print(f"  rechazado por: {exc.diag.constraint_name or exc.diag.message_primary}")
        return isinstance(exc, psycopg.errors.IntegrityError)
    return False


class Seed:
    """Helpers mínimos para armar datos válidos."""

    def __init__(self, conn):
        self.c = conn

    def one(self, q, p=()):
        return self.c.execute(q, p).fetchone()[0]

    def tercero(self, roles=("CLIENTE",)):
        tid = uuid.uuid4()
        self.c.execute(
            "INSERT INTO tercero (id, razon_social, pais_codigo, tipo_identificacion, numero_identificacion)"
            " VALUES (%s, 'Empresa SA', 'AR', 'CUIT', %s)", (tid, str(uuid.uuid4())[:20]))
        for r in roles:
            self.c.execute("INSERT INTO tercero_rol (tercero_id, rol_comercial) VALUES (%s, %s)", (tid, r))
        return tid

    def usuario(self):
        uid = uuid.uuid4()
        self.c.execute(
            "INSERT INTO usuario (id, nombre, apellido, email, password_hash)"
            " VALUES (%s, 'Ana', 'Pérez', %s, 'hash')", (uid, f"{uid}@test.com"))
        return uid

    def ubicacion(self, pais="AR"):
        lid = uuid.uuid4()
        self.c.execute(
            "INSERT INTO ubicacion (id, nombre, tipo, pais_codigo, ciudad, latitud, longitud)"
            " VALUES (%s, 'Puerto', 'PUERTO', %s, 'Ciudad', -34.6, -58.4)", (lid, pais))
        return lid

    def envio(self, cliente=None, usuario=None, estado="BORRADOR", **extra):
        eid = uuid.uuid4()
        cliente = cliente or self.tercero()
        usuario = usuario or self.usuario()
        self.c.execute(
            "INSERT INTO envio (id, codigo, cliente_id, origen_id, destino_id, descripcion_carga, estado,"
            " peso_kg, volumen_m3, creado_en, creado_por_id) VALUES (%s,%s,%s,%s,%s,'carga',%s,100,10,%s,%s)",
            (eid, f"ENV-{eid}", cliente, self.ubicacion(), self.ubicacion("ES"), estado, T0, usuario))
        return eid

    def contenedor(self):
        cid = uuid.uuid4()
        self.c.execute(
            "INSERT INTO contenedor (id, codigo_internacional, tipo, capacidad_kg, capacidad_m3)"
            " VALUES (%s, %s, 'SECO', 20000, 33)", (cid, f"CONT{str(cid)[:10]}"))
        return cid

    def sensor(self):
        sid = uuid.uuid4()
        self.c.execute(
            "INSERT INTO sensor (id, codigo, tipo, fabricante) VALUES (%s, %s, 'AMBIENTAL', 'ACME')",
            (sid, f"S-{sid}"))
        return sid

    def reserva(self, envio, cont, desde, hasta, estado="RESERVADA"):
        self.c.execute(
            "INSERT INTO envio_contenedor (id, envio_id, contenedor_id, estado, reserva_desde, reserva_hasta,"
            " peso_asignado_kg, volumen_asignado_m3) VALUES (%s,%s,%s,%s,%s,%s,100,10)",
            (uuid.uuid4(), envio, cont, estado, desde, hasta))

    def alerta(self, envio, cont, clave, estado="ABIERTA"):
        aid = uuid.uuid4()
        cerrada = (h(5), "AUTOMATICO") if estado == "CERRADA" else (None, None)
        self.c.execute(
            "INSERT INTO alerta (id, envio_id, contenedor_id, tipo, severidad, estado, primera_deteccion_en,"
            " ultima_deteccion_en, clave_deduplicacion, cerrada_en, cierre_tipo)"
            " VALUES (%s,%s,%s,'TEMPERATURA','MEDIA',%s,%s,%s,%s,%s,%s)",
            (aid, envio, cont, estado, h(1), h(2), clave, *cerrada))
        return aid

    def cuenta(self, tercero=None, naturaleza="COBRAR"):
        cid = uuid.uuid4()
        self.c.execute(
            "INSERT INTO cuenta_corriente (id, tercero_id, naturaleza, moneda_codigo) VALUES (%s,%s,%s,'USD')",
            (cid, tercero or self.tercero(), naturaleza))
        return cid

    def factura(self, tercero, usuario, estado="BORRADOR"):
        fid = uuid.uuid4()
        self.c.execute(
            "INSERT INTO factura (id, tercero_id, sentido, tipo_comprobante, moneda_codigo, importe_total, estado,"
            " creada_por_id) VALUES (%s,%s,'VENTA','FA',%s,%s,%s,%s)",
            (fid, tercero, "USD", 0, estado, usuario))
        return fid


# --------------------------------------------------------------------------- esquema
def test_catalogo_tiene_38_tablas(conn):
    # Se excluyen las particiones: con el particionamiento opcional activado siguen siendo 38 tablas lógicas.
    n = Seed(conn).one(
        "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
        "AND relkind IN ('r', 'p') AND NOT relispartition")
    assert n == 38  # 37 + auditoria


def test_seed_roles_y_permisos(conn):
    s = Seed(conn)
    assert s.one("SELECT count(*) FROM rol") == 5
    assert s.one("SELECT count(*) FROM permiso") == 25  # + auditoria.consultar
    admin = s.one(
        "SELECT count(*) FROM rol_permiso rp JOIN rol r ON r.id = rp.rol_id WHERE r.codigo = 'ADMINISTRADOR'")
    assert admin == 25


# --------------------------------------------------------------------------- acceso
def test_email_unico_sin_distinguir_mayusculas(conn):
    s = Seed(conn)
    conn.execute("INSERT INTO usuario (id, nombre, apellido, email, password_hash)"
                 " VALUES (%s,'A','B','Mail@Test.com','h')", (uuid.uuid4(),))
    assert fails(conn, "INSERT INTO usuario (id, nombre, apellido, email, password_hash)"
                       " VALUES (%s,'C','D','mail@test.COM','h')", (uuid.uuid4(),))


def test_sesion_abierta_no_puede_tener_fin(conn):
    s = Seed(conn)
    u = s.usuario()
    assert fails(conn, "INSERT INTO sesion (id, usuario_id, inicio_en, vence_en, fin_en, estado, instancia_id)"
                       " VALUES (%s,%s,%s,%s,%s,'ABIERTA',%s)", (uuid.uuid4(), u, h(0), h(8), h(1), uuid.uuid4()))
    assert fails(conn, "INSERT INTO sesion (id, usuario_id, inicio_en, vence_en, estado, instancia_id)"
                       " VALUES (%s,%s,%s,%s,'CERRADA',%s)", (uuid.uuid4(), u, h(0), h(8), uuid.uuid4()))


# --------------------------------------------------------------------------- logística
def test_reservas_superpuestas_de_un_contenedor_se_rechazan(conn):
    s = Seed(conn)
    cont, e1, e2 = s.contenedor(), s.envio(), s.envio()
    s.reserva(e1, cont, h(0), h(10))
    assert fails(conn, "INSERT INTO envio_contenedor (id, envio_id, contenedor_id, estado, reserva_desde,"
                       " reserva_hasta, peso_asignado_kg, volumen_asignado_m3)"
                       " VALUES (%s,%s,%s,'RESERVADA',%s,%s,1,1)", (uuid.uuid4(), e2, cont, h(5), h(15)))


def test_reservas_contiguas_son_validas_intervalo_semiabierto(conn):
    s = Seed(conn)
    cont, e1, e2 = s.contenedor(), s.envio(), s.envio()
    s.reserva(e1, cont, h(0), h(10))
    s.reserva(e2, cont, h(10), h(20))  # empieza justo donde termina la anterior: no se solapa


def test_reserva_cancelada_no_bloquea_el_contenedor(conn):
    s = Seed(conn)
    cont, e1, e2 = s.contenedor(), s.envio(), s.envio()
    s.reserva(e1, cont, h(0), h(10), estado="CANCELADA")
    s.reserva(e2, cont, h(0), h(10))


def test_uso_real_abierto_se_extiende_a_infinito(conn):
    s = Seed(conn)
    cont, e1, e2 = s.contenedor(), s.envio(), s.envio()
    conn.execute(
        "INSERT INTO envio_contenedor (id, envio_id, contenedor_id, estado, reserva_desde, reserva_hasta,"
        " uso_desde, peso_asignado_kg, volumen_asignado_m3) VALUES (%s,%s,%s,'EN_USO',%s,%s,%s,1,1)",
        (uuid.uuid4(), e1, cont, h(0), h(10), h(0)))
    # Reserva posterior que ya no se solapa en reserva pero sí con el uso abierto
    assert fails(conn,
                 "INSERT INTO envio_contenedor (id, envio_id, contenedor_id, estado, reserva_desde, reserva_hasta,"
                 " uso_desde, peso_asignado_kg, volumen_asignado_m3) VALUES (%s,%s,%s,'EN_USO',%s,%s,%s,1,1)",
                 (uuid.uuid4(), e2, cont, h(20), h(30), h(25)))


def test_sensor_no_puede_estar_en_dos_contenedores_a_la_vez(conn):
    s = Seed(conn)
    sensor, c1, c2, u = s.sensor(), s.contenedor(), s.contenedor(), s.usuario()
    conn.execute("INSERT INTO sensor_contenedor (id, sensor_id, contenedor_id, instalado_en, instalado_por_id)"
                 " VALUES (%s,%s,%s,%s,%s)", (uuid.uuid4(), sensor, c1, h(0), u))
    assert fails(conn, "INSERT INTO sensor_contenedor (id, sensor_id, contenedor_id, instalado_en, instalado_por_id)"
                       " VALUES (%s,%s,%s,%s,%s)", (uuid.uuid4(), sensor, c2, h(5), u))


def test_sensor_reinstalable_despues_del_retiro(conn):
    s = Seed(conn)
    sensor, c1, c2, u = s.sensor(), s.contenedor(), s.contenedor(), s.usuario()
    conn.execute("INSERT INTO sensor_contenedor (id, sensor_id, contenedor_id, instalado_en, retirado_en,"
                 " instalado_por_id) VALUES (%s,%s,%s,%s,%s,%s)", (uuid.uuid4(), sensor, c1, h(0), h(5), u))
    conn.execute("INSERT INTO sensor_contenedor (id, sensor_id, contenedor_id, instalado_en, instalado_por_id)"
                 " VALUES (%s,%s,%s,%s,%s)", (uuid.uuid4(), sensor, c2, h(5), u))


def test_envio_entregado_exige_entrega_real(conn):
    s = Seed(conn)
    e = s.envio()
    assert fails(conn, "UPDATE envio SET estado='ENTREGADO', entrega_estimada_en=%s, entrega_comprometida_en=%s"
                       " WHERE id=%s", (h(10), h(10), e))


def test_compromiso_de_entrega_es_inmutable(conn):
    s = Seed(conn)
    e = s.envio()
    conn.execute("UPDATE envio SET estado='EN_TRANSITO', entrega_estimada_en=%s, entrega_comprometida_en=%s"
                 " WHERE id=%s", (h(10), h(10), e))
    # el pronóstico puede cambiar...
    conn.execute("UPDATE envio SET entrega_estimada_en=%s WHERE id=%s", (h(12), e))
    # ...el compromiso no
    assert fails(conn, "UPDATE envio SET entrega_comprometida_en=%s WHERE id=%s", (h(12), e))


def test_tramo_en_curso_exige_vehiculo_y_salida_real(conn):
    s = Seed(conn)
    e, op = s.envio(), s.tercero(("OPERADOR",))
    o, d = s.ubicacion(), s.ubicacion("ES")
    base = ("INSERT INTO tramo (id, envio_id, orden, origen_id, destino_id, medio, operador_id, estado,"
            " salida_estimada_en, llegada_estimada_en, salida_real_en) VALUES (%s,%s,1,%s,%s,'MARITIMO',%s,"
            "'EN_CURSO',%s,%s,%s)")
    assert fails(conn, base, (uuid.uuid4(), e, o, d, op, h(0), h(10), h(0)))  # sin vehículo


def test_orden_de_tramo_unico_por_envio(conn):
    s = Seed(conn)
    e, op = s.envio(), s.tercero(("OPERADOR",))
    o, d = s.ubicacion(), s.ubicacion("ES")
    q = ("INSERT INTO tramo (id, envio_id, orden, origen_id, destino_id, medio, operador_id,"
         " salida_estimada_en, llegada_estimada_en) VALUES (%s,%s,1,%s,%s,'TERRESTRE',%s,%s,%s)")
    conn.execute(q, (uuid.uuid4(), e, o, d, op, h(0), h(5)))
    assert fails(conn, q, (uuid.uuid4(), e, o, d, op, h(6), h(9)))


# --------------------------------------------------------------------------- monitoreo
def test_una_sola_alerta_activa_por_clave(conn):
    s = Seed(conn)
    e, cont = s.envio(), s.contenedor()
    s.alerta(e, cont, "temp:sensor1", estado="ABIERTA")
    assert fails(conn, "INSERT INTO alerta (id, envio_id, contenedor_id, tipo, severidad, primera_deteccion_en,"
                       " ultima_deteccion_en, clave_deduplicacion) VALUES (%s,%s,%s,'TEMPERATURA','MEDIA',%s,%s,%s)",
                 (uuid.uuid4(), e, cont, h(1), h(2), "temp:sensor1"))


def test_clave_reutilizable_si_la_anterior_esta_cerrada(conn):
    s = Seed(conn)
    e, cont = s.envio(), s.contenedor()
    s.alerta(e, cont, "temp:sensor1", estado="CERRADA")
    s.alerta(e, cont, "temp:sensor1", estado="ABIERTA")  # nuevo episodio


def test_cierre_manual_exige_usuario_y_motivo(conn):
    s = Seed(conn)
    e, cont = s.envio(), s.contenedor()
    a = s.alerta(e, cont, "k1")
    assert fails(conn, "UPDATE alerta SET estado='CERRADA', cerrada_en=%s, cierre_tipo='MANUAL' WHERE id=%s",
                 (h(3), a))


def test_alerta_con_tramo_de_otro_envio_se_rechaza(conn):
    s = Seed(conn)
    e1, e2, cont, op = s.envio(), s.envio(), s.contenedor(), s.tercero(("OPERADOR",))
    o, d = s.ubicacion(), s.ubicacion("ES")
    tramo = uuid.uuid4()
    conn.execute("INSERT INTO tramo (id, envio_id, orden, origen_id, destino_id, medio, operador_id,"
                 " salida_estimada_en, llegada_estimada_en) VALUES (%s,%s,1,%s,%s,'TERRESTRE',%s,%s,%s)",
                 (tramo, e1, o, d, op, h(0), h(5)))
    assert fails(conn, "INSERT INTO alerta (id, envio_id, contenedor_id, tramo_id, tipo, severidad,"
                       " primera_deteccion_en, ultima_deteccion_en, clave_deduplicacion)"
                       " VALUES (%s,%s,%s,%s,'TEMPERATURA','MEDIA',%s,%s,'k')",
                 (uuid.uuid4(), e2, cont, tramo, h(1), h(2)))


def test_incidente_resuelto_exige_resolucion(conn):
    s = Seed(conn)
    e = s.envio()
    iid = uuid.uuid4()
    conn.execute("INSERT INTO incidente (id, envio_id, tipo, severidad, descripcion, ocurrido_en, abierto_en)"
                 " VALUES (%s,%s,'DEMORA','ALTA','retraso en aduana',%s,%s)", (iid, e, h(1), h(1)))
    assert fails(conn, "UPDATE incidente SET estado='RESUELTO', resuelto_en=%s WHERE id=%s", (h(3), iid))
    conn.execute("UPDATE incidente SET estado='RESUELTO', resuelto_en=%s, resolucion='liberado' WHERE id=%s",
                 (h(3), iid))


def test_alerta_origen_unica_por_incidente(conn):
    s = Seed(conn)
    e, cont = s.envio(), s.contenedor()
    a = s.alerta(e, cont, "k2")
    q = ("INSERT INTO incidente (id, envio_id, tipo, severidad, descripcion, ocurrido_en, alerta_origen_id)"
         " VALUES (%s,%s,'TEMP','ALTA','desviación',%s,%s)")
    conn.execute(q, (uuid.uuid4(), e, h(1), a))
    assert fails(conn, q, (uuid.uuid4(), e, h(1), a))


# --------------------------------------------------------------------------- comercial
def test_cargo_no_se_factura_dos_veces(conn):
    s = Seed(conn)
    cli, usr, e = s.tercero(), s.usuario(), s.envio()
    cargo = uuid.uuid4()
    conn.execute("INSERT INTO cargo_envio (id, envio_id, concepto, cantidad, precio_unitario, moneda_codigo)"
                 " VALUES (%s,%s,'Flete',1,100,'USD')", (cargo, e))
    f1, f2 = s.factura(cli, usr), s.factura(cli, usr)
    q = ("INSERT INTO factura_detalle (id, factura_id, renglon, cargo_envio_id, concepto, cantidad,"
         " precio_unitario, subtotal) VALUES (%s,%s,1,%s,'Flete',1,100,100)")
    conn.execute(q, (uuid.uuid4(), f1, cargo))
    assert fails(conn, q, (uuid.uuid4(), f2, cargo))


def test_numero_de_factura_de_venta_unico(conn):
    s = Seed(conn)
    cli, usr = s.tercero(), s.usuario()
    q = ("INSERT INTO factura (id, tercero_id, sentido, tipo_comprobante, serie, numero, emitida_en, vence_en,"
         " moneda_codigo, importe_total, estado, creada_por_id)"
         " VALUES (%s,%s,'VENTA','FA','0001','00000001',%s,%s,'USD',100,'EMITIDA',%s)")
    conn.execute(q, (uuid.uuid4(), cli, h(0), h(24), usr))
    assert fails(conn, q, (uuid.uuid4(), cli, h(0), h(24), usr))


def test_factura_emitida_exige_numero_y_total_positivo(conn):
    s = Seed(conn)
    cli, usr = s.tercero(), s.usuario()
    assert fails(conn, "INSERT INTO factura (id, tercero_id, sentido, tipo_comprobante, moneda_codigo,"
                       " importe_total, estado, creada_por_id) VALUES (%s,%s,'VENTA','FA','USD',100,'EMITIDA',%s)",
                 (uuid.uuid4(), cli, usr))


def test_clave_de_idempotencia_de_pago_unica(conn):
    s = Seed(conn)
    cli, usr = s.tercero(), s.usuario()
    clave = uuid.uuid4()
    q = ("INSERT INTO pago (id, tercero_id, sentido, fecha, monto, moneda_codigo, medio, registrado_por_id,"
         " clave_idempotencia) VALUES (%s,%s,'COBRO',%s,50,'USD','TRANSFERENCIA',%s,%s)")
    conn.execute(q, (uuid.uuid4(), cli, h(0), usr, clave))
    assert fails(conn, q, (uuid.uuid4(), cli, h(0), usr, clave))


def test_aplicacion_activa_unica_pero_reaplicable_tras_anular(conn):
    s = Seed(conn)
    cli, usr = s.tercero(), s.usuario()
    pago, fac = uuid.uuid4(), s.factura(cli, usr)
    conn.execute("INSERT INTO pago (id, tercero_id, sentido, fecha, monto, moneda_codigo, medio,"
                 " registrado_por_id, clave_idempotencia) VALUES (%s,%s,'COBRO',%s,50,'USD','EFECTIVO',%s,%s)",
                 (pago, cli, h(0), usr, uuid.uuid4()))
    q = "INSERT INTO pago_aplicacion (id, pago_id, factura_id, monto_aplicado) VALUES (%s,%s,%s,20)"
    a1 = uuid.uuid4()
    conn.execute(q, (a1, pago, fac))
    assert fails(conn, q, (uuid.uuid4(), pago, fac))
    conn.execute("UPDATE pago_aplicacion SET anulada_en = CURRENT_TIMESTAMP, motivo_anulacion='error' WHERE id=%s",
                 (a1,))
    conn.execute(q, (uuid.uuid4(), pago, fac))  # nueva fila, la anulada se conserva


def _movimiento(conn, cuenta, factura, sentido="AUMENTA", importe=100, clave=None, reversa=None):
    mid = uuid.uuid4()
    conn.execute("INSERT INTO movimiento_cuenta (id, cuenta_id, fecha, sentido, importe, factura_id,"
                 " reversa_de_id, clave_origen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                 (mid, cuenta, h(0), sentido, importe, factura, reversa, clave or f"mov:{mid}"))
    return mid


def test_movimientos_son_inmutables(conn):
    s = Seed(conn)
    cli, usr = s.tercero(), s.usuario()
    cuenta, fac = s.cuenta(cli), s.factura(cli, usr)
    mid = _movimiento(conn, cuenta, fac)
    assert fails(conn, "UPDATE movimiento_cuenta SET importe = 1 WHERE id = %s", (mid,))
    assert fails(conn, "DELETE FROM movimiento_cuenta WHERE id = %s", (mid,))


def test_reversa_valida_y_unica(conn):
    s = Seed(conn)
    cli, usr = s.tercero(), s.usuario()
    cuenta, fac = s.cuenta(cli), s.factura(cli, usr)
    orig = _movimiento(conn, cuenta, fac)
    # misma cuenta, mismo importe, sentido opuesto: válida
    rev = uuid.uuid4()
    conn.execute("INSERT INTO movimiento_cuenta (id, cuenta_id, fecha, sentido, importe, reversa_de_id, clave_origen)"
                 " VALUES (%s,%s,%s,'DISMINUYE',100,%s,%s)", (rev, cuenta, h(1), orig, f"rev:{rev}"))
    # segunda reversa del mismo original: rechazada (UNIQUE)
    assert fails(conn, "INSERT INTO movimiento_cuenta (id, cuenta_id, fecha, sentido, importe, reversa_de_id,"
                       " clave_origen) VALUES (%s,%s,%s,'DISMINUYE',100,%s,'otra')", (uuid.uuid4(), cuenta, h(2), orig))
    # revertir una reversa: rechazado
    assert fails(conn, "INSERT INTO movimiento_cuenta (id, cuenta_id, fecha, sentido, importe, reversa_de_id,"
                       " clave_origen) VALUES (%s,%s,%s,'AUMENTA',100,%s,'rr')", (uuid.uuid4(), cuenta, h(3), rev))


def test_reversa_con_importe_distinto_se_rechaza(conn):
    s = Seed(conn)
    cli, usr = s.tercero(), s.usuario()
    cuenta, fac = s.cuenta(cli), s.factura(cli, usr)
    orig = _movimiento(conn, cuenta, fac)
    assert fails(conn, "INSERT INTO movimiento_cuenta (id, cuenta_id, fecha, sentido, importe, reversa_de_id,"
                       " clave_origen) VALUES (%s,%s,%s,'DISMINUYE',99,%s,'x')", (uuid.uuid4(), cuenta, h(1), orig))


def test_movimiento_exige_exactamente_un_origen(conn):
    s = Seed(conn)
    cuenta = s.cuenta()
    assert fails(conn, "INSERT INTO movimiento_cuenta (id, cuenta_id, fecha, sentido, importe, clave_origen)"
                       " VALUES (%s,%s,%s,'AUMENTA',10,'sin-origen')", (uuid.uuid4(), cuenta, h(0)))


def test_cierre_de_cuenta_reconciliable_y_unico_por_periodo(conn):
    s = Seed(conn)
    cuenta = s.cuenta()
    q = ("INSERT INTO cierre_cuenta (id, cuenta_id, periodo, saldo_inicial, total_aumentos, total_disminuciones,"
         " saldo_final) VALUES (%s,%s,%s,%s,%s,%s,%s)")
    assert fails(conn, q, (uuid.uuid4(), cuenta, "2026-01-01", 0, 100, 30, 99))     # saldo no reconcilia
    assert fails(conn, q, (uuid.uuid4(), cuenta, "2026-01-15", 0, 100, 30, 70))     # no es primer día de mes
    conn.execute(q, (uuid.uuid4(), cuenta, "2026-01-01", 0, 100, 30, 70))
    assert fails(conn, q, (uuid.uuid4(), cuenta, "2026-01-01", 70, 0, 0, 70))       # período repetido


def test_cuenta_unica_por_tercero_naturaleza_moneda(conn):
    s = Seed(conn)
    t = s.tercero()
    s.cuenta(t, "COBRAR")
    assert fails(conn, "INSERT INTO cuenta_corriente (id, tercero_id, naturaleza, moneda_codigo)"
                       " VALUES (%s,%s,'COBRAR','USD')", (uuid.uuid4(), t))
    s.cuenta(t, "PAGAR")  # misma empresa, otra naturaleza: permitido


# --------------------------------------------------------------------------- comunicación y procesos
def test_grupo_exige_nombre(conn):
    s = Seed(conn)
    u = s.usuario()
    assert fails(conn, "INSERT INTO conversacion (id, tipo, creada_por_id) VALUES (%s,'GRUPAL',%s)",
                 (uuid.uuid4(), u))
    conn.execute("INSERT INTO conversacion (id, tipo, creada_por_id) VALUES (%s,'PRIVADA',%s)", (uuid.uuid4(), u))


def test_membresia_simultanea_duplicada_se_rechaza(conn):
    s = Seed(conn)
    u = s.usuario()
    conv = uuid.uuid4()
    conn.execute("INSERT INTO conversacion (id, tipo, nombre, creada_por_id) VALUES (%s,'GRUPAL','Ops',%s)",
                 (conv, u))
    q = ("INSERT INTO conversacion_miembro (id, conversacion_id, usuario_id, ingreso_en, salida_en)"
         " VALUES (%s,%s,%s,%s,%s)")
    conn.execute(q, (uuid.uuid4(), conv, u, h(0), h(5)))
    assert fails(conn, q, (uuid.uuid4(), conv, u, h(3), None))   # solapada
    conn.execute(q, (uuid.uuid4(), conv, u, h(5), None))         # reingreso después de salir


def test_prediccion_sin_probabilidad_es_sin_datos(conn):
    s = Seed(conn)
    e = s.envio()
    q = ("INSERT INTO prediccion_riesgo (id, envio_id, fecha_evaluacion, probabilidad_demora, riesgo_operativo,"
         " metodo_codigo, metodo_version) VALUES (%s,%s,'2026-01-01',%s,%s,'FRECUENCIA_HISTORICA','1.0')")
    assert fails(conn, q, (uuid.uuid4(), e, None, "ALTO"))
    assert fails(conn, q, (uuid.uuid4(), e, Decimal("1.5"), "ALTO"))
    conn.execute(q, (uuid.uuid4(), e, None, "SIN_DATOS"))
    conn.execute(q.replace("'2026-01-01'", "'2026-01-02'"), (uuid.uuid4(), e, Decimal("0.35"), "MEDIO"))


def test_outbox_evento_inmutable_y_entrega_por_destino(conn):
    ev = uuid.uuid4()
    conn.execute("INSERT INTO outbox_evento (id, agregado_tipo, agregado_id, agregado_version, evento_tipo, payload)"
                 " VALUES (%s,'envio',%s,1,'ENVIO_CREADO','{}')", (ev, uuid.uuid4()))
    assert fails(conn, "UPDATE outbox_evento SET evento_tipo = 'OTRO' WHERE id = %s", (ev,))
    conn.execute("INSERT INTO outbox_entrega (evento_id, destino, estado, procesada_en)"
                 " VALUES (%s,'MONGO_EVENTOS','COMPLETADA',CURRENT_TIMESTAMP)", (ev,))
    conn.execute("INSERT INTO outbox_entrega (evento_id, destino) VALUES (%s,'NEO4J_LOGISTICA')", (ev,))
    assert fails(conn, "INSERT INTO outbox_entrega (evento_id, destino) VALUES (%s,'NEO4J_LOGISTICA')", (ev,))
    # COMPLETADA exige procesada_en
    assert fails(conn, "UPDATE outbox_entrega SET estado='COMPLETADA' WHERE evento_id=%s AND destino='NEO4J_LOGISTICA'",
                 (ev,))


# --------------------------------------------------------------------------- auditoría
def _auditoria(conn, accion="crear_tercero", resultado="EXITO"):
    aid = uuid.uuid4()
    conn.execute("INSERT INTO auditoria (id, accion, resultado) VALUES (%s, %s, %s)", (aid, accion, resultado))
    return aid


def test_auditoria_es_inmutable_incluso_para_administracion(conn):
    aid = _auditoria(conn)
    assert fails(conn, "UPDATE auditoria SET accion = 'otra' WHERE id = %s", (aid,))
    assert fails(conn, "DELETE FROM auditoria WHERE id = %s", (aid,))


def test_auditoria_valida_resultado_y_accion(conn):
    assert fails(conn, "INSERT INTO auditoria (id, accion, resultado) VALUES (%s, 'x', 'QUIZAS')", (uuid.uuid4(),))
    assert fails(conn, "INSERT INTO auditoria (id, accion, resultado) VALUES (%s, '  ', 'EXITO')", (uuid.uuid4(),))
    assert fails(conn, "INSERT INTO auditoria (id, accion, resultado, detalle) VALUES (%s, 'x', 'EXITO', '[]')",
                 (uuid.uuid4(),))  # el detalle es siempre un objeto JSON
