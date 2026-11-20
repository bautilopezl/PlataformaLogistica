"""Consultas de solo lectura para reconciliación (AGENTS.md §13/§14). Nunca modifica nada."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session


class ReconciliacionRepository:
    def envios_recientes(self, tx: Session, *, limite: int) -> list[tuple[uuid.UUID, str, int]]:
        """(id, código, versión) de los envíos más nuevos, acotado para que la corrida sea rápida
        (volumen académico: revisar todo no hace falta para detectar un patrón de desincronización)."""
        filas = tx.execute(
            text("SELECT id, codigo, version FROM envio ORDER BY creado_en DESC LIMIT :limite"),
            {"limite": limite},
        )
        return [(r.id, r.codigo, r.version) for r in filas]

    def entregas_fallidas(self, tx: Session) -> list[tuple[uuid.UUID, uuid.UUID, str, str, int]]:
        """(evento_id, agregado_id, destino, evento_tipo, intentos) de las entregas que agotaron sus
        reintentos (AGENTS.md §9): la fase 3 ya las deja en FALLIDA; acá se listan para el reporte."""
        filas = tx.execute(
            text(
                "SELECT x.evento_id, e.agregado_id, x.destino, e.evento_tipo, x.intentos"
                " FROM outbox_entrega x JOIN outbox_evento e ON e.id = x.evento_id"
                " WHERE x.estado = 'FALLIDA' ORDER BY e.agregado_id, x.destino"
            )
        )
        return [(r.evento_id, r.agregado_id, r.destino, r.evento_tipo, r.intentos) for r in filas]

    def completadas_mongo_por_envio(
        self, tx: Session, envio_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Cuántas entregas MONGO_EVENTOS quedaron COMPLETADA por envío, en una sola consulta (evita N+1
        al cruzar contra los documentos de Mongo en el servicio)."""
        if not envio_ids:
            return {}
        filas = tx.execute(
            text(
                "SELECT e.agregado_id, count(*) FROM outbox_evento e JOIN outbox_entrega x"
                " ON x.evento_id = e.id"
                " WHERE x.destino = 'MONGO_EVENTOS' AND x.estado = 'COMPLETADA'"
                " AND e.agregado_id = ANY(CAST(:ids AS uuid[]))"
                " GROUP BY e.agregado_id"
            ),
            {"ids": envio_ids},
        )
        return {r.agregado_id: r.count for r in filas}


    def pagos_sobreaplicados(self, tx: Session) -> list[dict]:
        """Pagos CONFIRMADOS cuyas aplicaciones vigentes superan el monto (no debería poder pasar: los
        servicios lo impiden con bloqueos; esto es una verificación independiente, defensa en profundidad)."""
        filas = tx.execute(
            text(
                """
                SELECT p.id, p.monto,
                       COALESCE(SUM(pa.monto_aplicado) FILTER (WHERE pa.anulada_en IS NULL), 0) AS aplicado
                FROM pago p LEFT JOIN pago_aplicacion pa ON pa.pago_id = p.id
                WHERE p.estado = 'CONFIRMADO'
                GROUP BY p.id, p.monto
                HAVING COALESCE(SUM(pa.monto_aplicado) FILTER (WHERE pa.anulada_en IS NULL), 0) > p.monto
                """
            )
        )
        return [{"id": r.id, "monto": r.monto, "aplicado": r.aplicado} for r in filas]

    def facturas_sobreaplicadas(self, tx: Session) -> list[dict]:
        filas = tx.execute(
            text(
                """
                SELECT f.id, f.importe_total,
                       COALESCE(SUM(pa.monto_aplicado) FILTER (WHERE pa.anulada_en IS NULL), 0) AS aplicado
                FROM factura f LEFT JOIN pago_aplicacion pa ON pa.factura_id = f.id
                WHERE f.estado = 'EMITIDA'
                GROUP BY f.id, f.importe_total
                HAVING COALESCE(SUM(pa.monto_aplicado) FILTER (WHERE pa.anulada_en IS NULL), 0)
                       > f.importe_total
                """
            )
        )
        return [{"id": r.id, "importe_total": r.importe_total, "aplicado": r.aplicado} for r in filas]

    def cierres_inconsistentes(self, tx: Session) -> list[dict]:
        """Recalcula cada cierre desde `movimiento_cuenta` (nunca desde `cierre_cuenta` mismo) y lo compara
        contra lo que quedó guardado: el saldo inicial debe coincidir con el saldo final del cierre
        anterior (o ser 0 si es el primero) y los totales deben coincidir con la suma real del período."""
        filas = tx.execute(
            text(
                """
                SELECT cc.id, cc.cuenta_id, cc.periodo, cc.saldo_inicial, cc.total_aumentos,
                       cc.total_disminuciones, cc.saldo_final,
                       COALESCE(prev.saldo_final, 0) AS saldo_inicial_esperado,
                       COALESCE(SUM(m.importe) FILTER (WHERE m.sentido = 'AUMENTA'), 0) AS aumentos_calc,
                       COALESCE(SUM(m.importe) FILTER (WHERE m.sentido = 'DISMINUYE'), 0) AS disminuciones_calc
                FROM cierre_cuenta cc
                LEFT JOIN cierre_cuenta prev
                    ON prev.cuenta_id = cc.cuenta_id AND prev.periodo = (cc.periodo - interval '1 month')::date
                LEFT JOIN movimiento_cuenta m
                    ON m.cuenta_id = cc.cuenta_id
                    AND m.fecha >= cc.periodo AND m.fecha < (cc.periodo + interval '1 month')
                GROUP BY cc.id, cc.cuenta_id, cc.periodo, cc.saldo_inicial, cc.total_aumentos,
                         cc.total_disminuciones, cc.saldo_final, prev.saldo_final
                """
            )
        )
        resultado = []
        for r in filas:
            if (
                r.saldo_inicial != r.saldo_inicial_esperado
                or r.total_aumentos != r.aumentos_calc
                or r.total_disminuciones != r.disminuciones_calc
            ):
                resultado.append(
                    {
                        "id": r.id,
                        "cuenta_id": r.cuenta_id,
                        "periodo": r.periodo,
                        "guardado": (r.saldo_inicial, r.total_aumentos, r.total_disminuciones),
                        "recalculado": (r.saldo_inicial_esperado, r.aumentos_calc, r.disminuciones_calc),
                    }
                )
        return resultado

    def cuentas_activas(self, tx: Session) -> list[uuid.UUID]:
        return list(tx.execute(text("SELECT id FROM cuenta_corriente WHERE activo")).scalars())
