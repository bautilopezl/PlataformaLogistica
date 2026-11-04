"""Repositorios comerciales en PostgreSQL: cargos, facturas, pagos, aplicaciones, cuentas, movimientos y
cierres.

Bloqueos (para resistir la concurrencia, AGENTS.md §10.3), siempre en este orden para no cruzarse:
pago → factura → cuenta. Los movimientos de cuenta son inmutables por trigger: se corrigen con reversas.
`clave_origen` (UNIQUE) impide contabilizar dos veces la misma factura, pago o reversa.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from logistica.domain.comercial import CargoPendiente, Cierre, FacturaSaldo, PagoSaldo, Renglon
from logistica.domain.comercial_dtos import (
    AplicacionDTO,
    CargoDTO,
    CierreDTO,
    CuentaDTO,
    FacturaDTO,
    MovimientoDTO,
    PagoDTO,
    RenglonDTO,
)


@dataclass(frozen=True)
class AlcanceComercial:
    """Interno: `tercero_id` None (ve todo). Externo: solo su empresa y las naturalezas de su perfil
    (cliente → COBRAR/ventas; operador → PAGAR/compras)."""

    tercero_id: uuid.UUID | None
    naturalezas: frozenset[str] = frozenset({"COBRAR", "PAGAR"})

    @property
    def sentidos_factura(self) -> list[str]:
        return [s for n, s in (("COBRAR", "VENTA"), ("PAGAR", "COMPRA")) if n in self.naturalezas]

    @property
    def sentidos_pago(self) -> list[str]:
        return [s for n, s in (("COBRAR", "COBRO"), ("PAGAR", "PAGO")) if n in self.naturalezas]


# --------------------------------------------------------------------------- cargos
_SELECT_CARGO = """
    SELECT c.id, c.envio_id, e.codigo, c.concepto, c.cantidad, c.precio_unitario, c.moneda_codigo, c.estado,
           c.creado_en, fd.factura_id
    FROM cargo_envio c
    JOIN envio e ON e.id = c.envio_id
    LEFT JOIN factura_detalle fd ON fd.cargo_envio_id = c.id
"""


class CargoRepository:
    def crear(
        self,
        tx: Session,
        cargo_id: uuid.UUID,
        envio_id: uuid.UUID,
        concepto: str,
        cantidad: Decimal,
        precio_unitario: Decimal,
        moneda: str,
    ) -> None:
        tx.execute(
            text(
                "INSERT INTO cargo_envio (id, envio_id, concepto, cantidad, precio_unitario, moneda_codigo,"
                " creado_en) VALUES (:id, :e, :c, :q, :p, :m, clock_timestamp())"
            ),
            {"id": cargo_id, "e": envio_id, "c": concepto, "q": cantidad, "p": precio_unitario, "m": moneda},
        )

    def obtener(self, tx: Session, cargo_id: uuid.UUID, *, bloquear: bool = False) -> CargoDTO | None:
        if bloquear:
            tx.execute(text("SELECT 1 FROM cargo_envio WHERE id = :id FOR UPDATE"), {"id": cargo_id})
        r = tx.execute(text(_SELECT_CARGO + " WHERE c.id = :id"), {"id": cargo_id}).one_or_none()
        return CargoDTO(*r) if r else None

    def de_envio(self, tx: Session, envio_id: uuid.UUID) -> list[CargoDTO]:
        rows = tx.execute(
            text(_SELECT_CARGO + " WHERE c.envio_id = :e ORDER BY c.creado_en, c.id"), {"e": envio_id}
        )
        return [CargoDTO(*r) for r in rows]

    def pendientes(
        self, tx: Session, envio_id: uuid.UUID, ids: list[uuid.UUID] | None = None
    ) -> list[CargoPendiente]:
        """Cargos PENDIENTES bloqueados (FOR UPDATE): nadie más los factura mientras dura la transacción."""
        rows = tx.execute(
            text(
                "SELECT id, envio_id, concepto, cantidad, precio_unitario, moneda_codigo FROM cargo_envio"
                " WHERE envio_id = :e AND estado = 'PENDIENTE'"
                " AND (CAST(:ids AS uuid[]) IS NULL OR id = ANY(CAST(:ids AS uuid[])))"
                " ORDER BY creado_en, id FOR UPDATE"
            ),
            {"e": envio_id, "ids": [str(i) for i in ids] if ids is not None else None},
        )
        return [CargoPendiente(*r) for r in rows]

    def cambiar_estado(self, tx: Session, ids: list[uuid.UUID], desde: str, hacia: str) -> int:
        return tx.execute(
            text("UPDATE cargo_envio SET estado = :h WHERE id = ANY(CAST(:ids AS uuid[])) AND estado = :d"),
            {"ids": [str(i) for i in ids], "d": desde, "h": hacia},
        ).rowcount


# --------------------------------------------------------------------------- facturas
_SELECT_FACTURA = """
    SELECT f.id, f.tercero_id, t.razon_social, f.sentido, f.tipo_comprobante, f.serie, f.numero, f.emitida_en,
           f.vence_en, f.moneda_codigo, f.importe_total, f.estado, f.creado_en, f.anulada_en,
               f.motivo_anulacion,
           COALESCE((SELECT sum(a.monto_aplicado) FROM pago_aplicacion a
                     WHERE a.factura_id = f.id AND a.anulada_en IS NULL), 0) AS aplicado
    FROM factura f
    JOIN tercero t ON t.id = f.tercero_id
"""


class FacturaRepository:
    def ultimo_numero(self, tx: Session, serie: str) -> str | None:
        """Último número de ventas de la serie. Toma un advisory lock de la TRANSACCIÓN por serie: dos
        emisiones simultáneas se ordenan y nunca calculan el mismo número (el índice único es el respaldo)."""
        tx.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"factura_venta:{serie}"})
        return tx.execute(
            text(
                "SELECT max(numero) FROM factura WHERE sentido = 'VENTA' AND tipo_comprobante = 'FACTURA'"
                " AND serie = :s"
            ),
            {"s": serie},
        ).scalar_one()

    def crear_emitida(
        self,
        tx: Session,
        *,
        factura_id: uuid.UUID,
        tercero_id: uuid.UUID,
        sentido: str,
        tipo: str,
        serie: str,
        numero: str,
        emitida_en: datetime,
        vence_en: datetime,
        moneda: str,
        total: Decimal,
        creada_por_id: uuid.UUID,
        renglones: list[Renglon],
    ) -> None:
        tx.execute(
            text(
                "INSERT INTO factura (id, tercero_id, sentido, tipo_comprobante, serie, numero, emitida_en, "
                "vence_en,"
                " moneda_codigo, importe_total, estado, creada_por_id, creado_en)"
                " VALUES (:id, :t, :s, :tipo, :serie, :num, :em, :ve, :m, :tot, 'EMITIDA', :u, "
                "clock_timestamp())"
            ),
            {
                "id": factura_id,
                "t": tercero_id,
                "s": sentido,
                "tipo": tipo,
                "serie": serie,
                "num": numero,
                "em": emitida_en,
                "ve": vence_en,
                "m": moneda,
                "tot": total,
                "u": creada_por_id,
            },
        )
        for i, r in enumerate(renglones, start=1):
            tx.execute(
                text(
                    "INSERT INTO factura_detalle (id, factura_id, renglon, envio_id, cargo_envio_id, "
                    "concepto,"
                    " cantidad, precio_unitario, subtotal) VALUES (:id, :f, :n, :e, :c, :con, :q, :p, :sub)"
                ),
                {
                    "id": uuid.uuid4(),
                    "f": factura_id,
                    "n": i,
                    "e": r.envio_id,
                    "c": r.cargo_id,
                    "con": r.concepto,
                    "q": r.cantidad,
                    "p": r.precio_unitario,
                    "sub": r.subtotal,
                },
            )

    def obtener(
        self, tx: Session, factura_id: uuid.UUID, *, bloquear: bool = False, con_renglones: bool = False
    ) -> FacturaDTO | None:
        if bloquear:
            tx.execute(text("SELECT 1 FROM factura WHERE id = :id FOR UPDATE"), {"id": factura_id})
        r = tx.execute(text(_SELECT_FACTURA + " WHERE f.id = :id"), {"id": factura_id}).one_or_none()
        if r is None:
            return None
        renglones = self.renglones(tx, factura_id) if con_renglones else []
        return FacturaDTO(*r, renglones=renglones)

    def renglones(self, tx: Session, factura_id: uuid.UUID) -> list[RenglonDTO]:
        rows = tx.execute(
            text(
                "SELECT renglon, concepto, cantidad, precio_unitario, subtotal, envio_id, cargo_envio_id"
                " FROM factura_detalle WHERE factura_id = :f ORDER BY renglon"
            ),
            {"f": factura_id},
        )
        return [RenglonDTO(*r) for r in rows]

    def cargos(self, tx: Session, factura_id: uuid.UUID) -> list[uuid.UUID]:
        return list(
            tx.execute(
                text(
                    "SELECT cargo_envio_id FROM factura_detalle WHERE factura_id = :f AND cargo_envio_id IS "
                    "NOT NULL"
                    " ORDER BY renglon"
                ),
                {"f": factura_id},
            ).scalars()
        )

    def saldo(self, tx: Session, factura_id: uuid.UUID) -> FacturaSaldo | None:
        f = self.obtener(tx, factura_id)
        return (
            FacturaSaldo(
                f.id, f.tercero_id, f.sentido, f.moneda_codigo, f.estado, f.importe_total, f.aplicado
            )
            if f
            else None
        )

    def existe_compra(self, tx: Session, tercero_id: uuid.UUID, tipo: str, serie: str, numero: str) -> bool:
        return tx.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM factura WHERE sentido = 'COMPRA' AND tercero_id = :t"
                " AND tipo_comprobante = :tipo AND COALESCE(serie, '') = :s AND numero = :n)"
            ),
            {"t": tercero_id, "tipo": tipo, "s": serie, "n": numero},
        ).scalar_one()

    def anular(self, tx: Session, factura_id: uuid.UUID, usuario_id: uuid.UUID, motivo: str) -> bool:
        return (
            tx.execute(
                text(
                    "UPDATE factura SET estado = 'ANULADA', anulada_en = clock_timestamp(), anulada_por_id = "
                    ":u,"
                    " motivo_anulacion = :m WHERE id = :id AND estado = 'EMITIDA'"
                ),
                {"id": factura_id, "u": usuario_id, "m": motivo},
            ).rowcount
            == 1
        )

    def listar(
        self,
        tx: Session,
        *,
        alcance: AlcanceComercial,
        sentido: str | None = None,
        estado: str | None = None,
        envio_id: uuid.UUID | None = None,
        limite: int = 500,
    ) -> list[FacturaDTO]:
        rows = tx.execute(
            text(
                _SELECT_FACTURA
                + " WHERE (CAST(:t AS uuid) IS NULL OR f.tercero_id = :t) AND f.sentido = ANY(:sentidos)"
                " AND (CAST(:s AS varchar) IS NULL OR f.sentido = :s)"
                " AND (CAST(:estado AS varchar) IS NULL OR f.estado = :estado)"
                " AND (CAST(:e AS uuid) IS NULL OR EXISTS (SELECT 1 FROM factura_detalle d"
                "      WHERE d.factura_id = f.id AND d.envio_id = :e))"
                " ORDER BY f.creado_en DESC, f.id LIMIT :limite"
            ),
            {
                "t": alcance.tercero_id,
                "sentidos": alcance.sentidos_factura,
                "s": sentido,
                "estado": estado,
                "e": envio_id,
                "limite": limite,
            },
        )
        return [FacturaDTO(*r) for r in rows]


# --------------------------------------------------------------------------- pagos y aplicaciones
_SELECT_PAGO = """
    SELECT p.id, p.tercero_id, t.razon_social, p.sentido, p.fecha, p.monto, p.moneda_codigo, p.medio,
        p.referencia,
           p.estado, p.anulado_en, p.motivo_anulacion,
           COALESCE((SELECT sum(a.monto_aplicado) FROM pago_aplicacion a
                     WHERE a.pago_id = p.id AND a.anulada_en IS NULL), 0) AS aplicado
    FROM pago p
    JOIN tercero t ON t.id = p.tercero_id
"""


class PagoRepository:
    def crear(
        self,
        tx: Session,
        pago_id: uuid.UUID,
        *,
        tercero_id: uuid.UUID,
        sentido: str,
        fecha: datetime,
        monto: Decimal,
        moneda: str,
        medio: str,
        referencia: str | None,
        usuario_id: uuid.UUID,
        clave: uuid.UUID,
    ) -> bool:
        """False si la clave de idempotencia ya existe (el pago ya se registró)."""
        return (
            tx.execute(
                text(
                    "INSERT INTO pago (id, tercero_id, sentido, fecha, monto, moneda_codigo, medio, "
                    "referencia,"
                    " registrado_por_id, clave_idempotencia, creado_en)"
                    " VALUES (:id, :t, :s, :f, :m, :mon, :medio, :ref, :u, :clave, clock_timestamp())"
                    " ON CONFLICT (clave_idempotencia) DO NOTHING"
                ),
                {
                    "id": pago_id,
                    "t": tercero_id,
                    "s": sentido,
                    "f": fecha,
                    "m": monto,
                    "mon": moneda,
                    "medio": medio,
                    "ref": referencia,
                    "u": usuario_id,
                    "clave": clave,
                },
            ).rowcount
            == 1
        )

    def por_clave(self, tx: Session, clave: uuid.UUID) -> PagoDTO | None:
        r = tx.execute(text(_SELECT_PAGO + " WHERE p.clave_idempotencia = :c"), {"c": clave}).one_or_none()
        return PagoDTO(*r) if r else None

    def obtener(self, tx: Session, pago_id: uuid.UUID, *, bloquear: bool = False) -> PagoDTO | None:
        if bloquear:
            tx.execute(text("SELECT 1 FROM pago WHERE id = :id FOR UPDATE"), {"id": pago_id})
        r = tx.execute(text(_SELECT_PAGO + " WHERE p.id = :id"), {"id": pago_id}).one_or_none()
        return PagoDTO(*r) if r else None

    def saldo(self, tx: Session, pago_id: uuid.UUID) -> PagoSaldo | None:
        p = self.obtener(tx, pago_id)
        return (
            PagoSaldo(p.id, p.tercero_id, p.sentido, p.moneda_codigo, p.estado, p.monto, p.aplicado)
            if p
            else None
        )

    def anular(self, tx: Session, pago_id: uuid.UUID, usuario_id: uuid.UUID, motivo: str) -> bool:
        return (
            tx.execute(
                text(
                    "UPDATE pago SET estado = 'ANULADO', anulado_en = clock_timestamp(), anulado_por_id = :u,"
                    " motivo_anulacion = :m WHERE id = :id AND estado = 'CONFIRMADO'"
                ),
                {"id": pago_id, "u": usuario_id, "m": motivo},
            ).rowcount
            == 1
        )

    def listar(
        self,
        tx: Session,
        *,
        alcance: AlcanceComercial,
        tercero_id: uuid.UUID | None = None,
        limite: int = 500,
    ) -> list[PagoDTO]:
        rows = tx.execute(
            text(
                _SELECT_PAGO
                + " WHERE (CAST(:alc AS uuid) IS NULL OR p.tercero_id = :alc) AND p.sentido = ANY(:sentidos)"
                " AND (CAST(:t AS uuid) IS NULL OR p.tercero_id = :t)"
                " ORDER BY p.fecha DESC, p.id LIMIT :limite"
            ),
            {"alc": alcance.tercero_id, "sentidos": alcance.sentidos_pago, "t": tercero_id, "limite": limite},
        )
        return [PagoDTO(*r) for r in rows]


_SELECT_APLICACION = """
    SELECT a.id, a.pago_id, a.factura_id, f.serie || '-' || f.numero, a.monto_aplicado, a.creado_en,
        a.anulada_en,
           a.motivo_anulacion
    FROM pago_aplicacion a
    JOIN factura f ON f.id = a.factura_id
"""


class AplicacionRepository:
    def crear(
        self, tx: Session, aplicacion_id: uuid.UUID, pago_id: uuid.UUID, factura_id: uuid.UUID, monto: Decimal
    ) -> None:
        tx.execute(
            text(
                "INSERT INTO pago_aplicacion (id, pago_id, factura_id, monto_aplicado, creado_en)"
                " VALUES (:id, :p, :f, :m, clock_timestamp())"
            ),
            {"id": aplicacion_id, "p": pago_id, "f": factura_id, "m": monto},
        )

    def obtener(self, tx: Session, aplicacion_id: uuid.UUID) -> AplicacionDTO | None:
        r = tx.execute(text(_SELECT_APLICACION + " WHERE a.id = :id"), {"id": aplicacion_id}).one_or_none()
        return AplicacionDTO(*r) if r else None

    def anular(self, tx: Session, aplicacion_id: uuid.UUID, motivo: str) -> bool:
        return (
            tx.execute(
                text(
                    "UPDATE pago_aplicacion SET anulada_en = greatest(clock_timestamp(), creado_en),"
                    " motivo_anulacion = :m WHERE id = :id AND anulada_en IS NULL"
                ),
                {"id": aplicacion_id, "m": motivo},
            ).rowcount
            == 1
        )

    def de_pago(self, tx: Session, pago_id: uuid.UUID) -> list[AplicacionDTO]:
        rows = tx.execute(
            text(_SELECT_APLICACION + " WHERE a.pago_id = :p ORDER BY a.creado_en, a.id"), {"p": pago_id}
        )
        return [AplicacionDTO(*r) for r in rows]

    def de_factura(self, tx: Session, factura_id: uuid.UUID) -> list[AplicacionDTO]:
        rows = tx.execute(
            text(_SELECT_APLICACION + " WHERE a.factura_id = :f ORDER BY a.creado_en, a.id"),
            {"f": factura_id},
        )
        return [AplicacionDTO(*r) for r in rows]


# --------------------------------------------------------------------------- cuentas, movimientos y cierres
_SELECT_CUENTA = """
    SELECT c.id, c.tercero_id, t.razon_social, c.naturaleza, c.moneda_codigo,
           COALESCE((SELECT sum(CASE WHEN m.sentido = 'AUMENTA' THEN m.importe ELSE -m.importe END)
                     FROM movimiento_cuenta m WHERE m.cuenta_id = c.id), 0) AS saldo,
           (SELECT max(k.periodo) FROM cierre_cuenta k WHERE k.cuenta_id = c.id) AS ultimo_cierre
    FROM cuenta_corriente c
    JOIN tercero t ON t.id = c.tercero_id
"""


class CuentaRepository:
    def obtener_o_crear(self, tx: Session, tercero_id: uuid.UUID, naturaleza: str, moneda: str) -> uuid.UUID:
        """Cuenta única por (tercero, naturaleza, moneda), bloqueada para registrar movimientos o cerrar."""
        tx.execute(
            text(
                "INSERT INTO cuenta_corriente (id, tercero_id, naturaleza, moneda_codigo) VALUES (:id, :t, "
                ":n, :m)"
                " ON CONFLICT (tercero_id, naturaleza, moneda_codigo) DO NOTHING"
            ),
            {"id": uuid.uuid4(), "t": tercero_id, "n": naturaleza, "m": moneda},
        )
        return tx.execute(
            text(
                "SELECT id FROM cuenta_corriente WHERE tercero_id = :t AND naturaleza = :n AND moneda_codigo "
                "= :m"
                " FOR UPDATE"
            ),
            {"t": tercero_id, "n": naturaleza, "m": moneda},
        ).scalar_one()

    def bloquear(self, tx: Session, cuenta_id: uuid.UUID) -> bool:
        return (
            tx.execute(
                text("SELECT 1 FROM cuenta_corriente WHERE id = :id FOR UPDATE"), {"id": cuenta_id}
            ).first()
            is not None
        )

    def obtener(self, tx: Session, cuenta_id: uuid.UUID) -> CuentaDTO | None:
        r = tx.execute(text(_SELECT_CUENTA + " WHERE c.id = :id"), {"id": cuenta_id}).one_or_none()
        return CuentaDTO(*r) if r else None

    def listar(
        self, tx: Session, *, alcance: AlcanceComercial, tercero_id: uuid.UUID | None = None
    ) -> list[CuentaDTO]:
        rows = tx.execute(
            text(
                _SELECT_CUENTA
                + " WHERE (CAST(:alc AS uuid) IS NULL OR c.tercero_id = :alc) AND c.naturaleza = ANY(:nat)"
                " AND (CAST(:t AS uuid) IS NULL OR c.tercero_id = :t)"
                " ORDER BY t.razon_social, c.naturaleza, c.moneda_codigo"
            ),
            {"alc": alcance.tercero_id, "nat": sorted(alcance.naturalezas), "t": tercero_id},
        )
        return [CuentaDTO(*r) for r in rows]

    def registrar_movimiento(
        self,
        tx: Session,
        *,
        cuenta_id: uuid.UUID,
        sentido: str,
        importe: Decimal,
        clave_origen: str,
        descripcion: str,
        factura_id: uuid.UUID | None = None,
        pago_id: uuid.UUID | None = None,
        reversa_de_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Fecha = reloj de PostgreSQL al registrar, con la cuenta ya bloqueada: así un movimiento nunca cae
        en un período que se cerró antes (los cierres solo toman meses terminados)."""
        movimiento_id = uuid.uuid4()
        tx.execute(
            text(
                "INSERT INTO movimiento_cuenta (id, cuenta_id, fecha, sentido, importe, factura_id, pago_id,"
                " reversa_de_id, descripcion, clave_origen)"
                " VALUES (:id, :c, clock_timestamp(), :s, :i, :f, :p, :r, :d, :k)"
            ),
            {
                "id": movimiento_id,
                "c": cuenta_id,
                "s": sentido,
                "i": importe,
                "f": factura_id,
                "p": pago_id,
                "r": reversa_de_id,
                "d": descripcion,
                "k": clave_origen,
            },
        )
        return movimiento_id

    def movimiento_original(
        self, tx: Session, *, factura_id: uuid.UUID | None = None, pago_id: uuid.UUID | None = None
    ) -> tuple[uuid.UUID, uuid.UUID, str, Decimal] | None:
        """(id, cuenta, sentido, importe) del movimiento que generó una factura o un pago."""
        r = tx.execute(
            text(
                "SELECT id, cuenta_id, sentido, importe FROM movimiento_cuenta"
                " WHERE (factura_id = :f OR pago_id = :p) AND reversa_de_id IS NULL"
            ),
            {"f": factura_id, "p": pago_id},
        ).one_or_none()
        return tuple(r) if r else None

    def movimientos(
        self,
        tx: Session,
        cuenta_id: uuid.UUID,
        *,
        limite: int,
        antes: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[MovimientoDTO]:
        """Más recientes primero; `antes` es el cursor (fecha, id) de la última fila de la página anterior."""
        rows = tx.execute(
            text(
                "SELECT id, fecha, sentido, importe, descripcion, factura_id, pago_id, reversa_de_id"
                " FROM movimiento_cuenta WHERE cuenta_id = :c"
                " AND (CAST(:f AS timestamptz) IS NULL OR (fecha, id) < (CAST(:f AS timestamptz), CAST(:i AS "
                "uuid)))"
                " ORDER BY fecha DESC, id DESC LIMIT :l"
            ),
            {"c": cuenta_id, "f": antes[0] if antes else None, "i": antes[1] if antes else None, "l": limite},
        )
        return [MovimientoDTO(*r) for r in rows]

    def resumen_periodo(self, tx: Session, cuenta_id: uuid.UUID, desde: datetime, hasta: datetime) -> Cierre:
        """Saldo al inicio (todo lo anterior a `desde`) y totales de [desde, hasta)."""
        r = tx.execute(
            text(
                "SELECT"
                " COALESCE(sum(CASE WHEN fecha < :d THEN CASE WHEN sentido = 'AUMENTA' THEN importe ELSE "
                "-importe END"
                "     END), 0),"
                " COALESCE(sum(CASE WHEN fecha >= :d AND fecha < :h AND sentido = 'AUMENTA' THEN importe "
                "END), 0),"
                " COALESCE(sum(CASE WHEN fecha >= :d AND fecha < :h AND sentido = 'DISMINUYE' THEN importe "
                "END), 0)"
                " FROM movimiento_cuenta WHERE cuenta_id = :c AND fecha < :h"
            ),
            {"c": cuenta_id, "d": desde, "h": hasta},
        ).one()
        return Cierre(r[0], r[1], r[2])

    def ultimo_cierre(self, tx: Session, cuenta_id: uuid.UUID) -> date | None:
        return tx.execute(
            text("SELECT max(periodo) FROM cierre_cuenta WHERE cuenta_id = :c"), {"c": cuenta_id}
        ).scalar_one()

    def cierre(self, tx: Session, cuenta_id: uuid.UUID, periodo: date) -> CierreDTO | None:
        r = tx.execute(
            text(
                "SELECT id, cuenta_id, periodo, saldo_inicial, total_aumentos, total_disminuciones, "
                "saldo_final,"
                " cerrado_en FROM cierre_cuenta WHERE cuenta_id = :c AND periodo = :p"
            ),
            {"c": cuenta_id, "p": periodo},
        ).one_or_none()
        return CierreDTO(*r) if r else None

    def cierres(self, tx: Session, cuenta_id: uuid.UUID) -> list[CierreDTO]:
        rows = tx.execute(
            text(
                "SELECT id, cuenta_id, periodo, saldo_inicial, total_aumentos, total_disminuciones, "
                "saldo_final,"
                " cerrado_en FROM cierre_cuenta WHERE cuenta_id = :c ORDER BY periodo DESC"
            ),
            {"c": cuenta_id},
        )
        return [CierreDTO(*r) for r in rows]

    def crear_cierre(self, tx: Session, cuenta_id: uuid.UUID, periodo: date, c: Cierre) -> bool:
        """False si ese período ya estaba cerrado (UNIQUE cuenta + período: idempotente)."""
        return (
            tx.execute(
                text(
                    "INSERT INTO cierre_cuenta (id, cuenta_id, periodo, saldo_inicial, total_aumentos,"
                    " total_disminuciones, saldo_final) VALUES (:id, :c, :p, :si, :a, :d, :sf)"
                    " ON CONFLICT (cuenta_id, periodo) DO NOTHING"
                ),
                {
                    "id": uuid.uuid4(),
                    "c": cuenta_id,
                    "p": periodo,
                    "si": c.saldo_inicial,
                    "a": c.total_aumentos,
                    "d": c.total_disminuciones,
                    "sf": c.saldo_final,
                },
            ).rowcount
            == 1
        )
