from __future__ import annotations

import json

from wisp.central import metering
from wisp.central.store_util import _now_iso

# Closed vocabularies, enforced on the WRITE (the radius_status discipline:
# an unknown state silently dropped is how a ledger lies).
INVOICE_STATES = ("open", "paid", "void")
PAYMENT_KINDS = ("gateway", "manual", "adjustment")

_ACCRUAL_COLS = ("org_id, day, paise, conn_count, conn_source, device_count,"
                 " winning_side, conn_rate_paise, floor_paise, flags, created_at")

# Distinct ONUs online inside the metering window — THE billable count since
# 2026-08-17 (the RADIUS username count it replaced is deleted, rung and
# query together: a dormant feed is a bill waiting to move by itself).
#
# Keyed on the normalised MAC, never the slot: these OLTs keep every slot an
# ONU ever occupied, so a re-registered box appears on two slots and a slot
# count double-bills it — DISTINCT on the MAC collapses the pair (the
# onu_places/onu_drops key rule).
_ONU_COUNT_SQL = (
    "SELECT COUNT(DISTINCT wisp_norm_mac(o.serial))"
    "  FROM onu_optics o JOIN org_devices d ON d.id = o.device_id"
    " WHERE o.org_id=? AND d.org_id=? AND d.is_active=1"
    "   AND COALESCE(o.serial,'') <> ''"
    "   AND o.last_online_at >= ?")


class BillingStoreMixin:

    # -------------------------------------------------- org flags and rates

    def org_billing(self, org_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT billing_exempt, deactivated, conn_rate_paise,"
                " floor_paise, self_declared_conns, self_declared_by,"
                " self_declared_at, billing_anchor_day"
                " FROM orgs WHERE org_id=?", (org_id,)).fetchone()
        if row is None:
            return {"exempt": False, "deactivated": False,
                    "conn_rate_paise": None, "floor_paise": None,
                    "self_declared_conns": None, "self_declared_by": None,
                    "self_declared_at": None, "billing_anchor_day": None}
        return {"exempt": bool(row["billing_exempt"]),
                "deactivated": bool(row["deactivated"]),
                "conn_rate_paise": row["conn_rate_paise"],
                "floor_paise": row["floor_paise"],
                "self_declared_conns": row["self_declared_conns"],
                "self_declared_by": row["self_declared_by"],
                "self_declared_at": row["self_declared_at"],
                "billing_anchor_day": row["billing_anchor_day"]}

    def set_org_billing_flags(self, org_id: str, *, exempt: bool | None = None,
                              deactivated: bool | None = None,
                              resume_day: str | None = None) -> None:
        """Superadmin toggles. `resume_day` re-anchors accrual when a flag
        that was suppressing it flips OFF — the days an org spent exempt or
        deactivated are a deliberate hole in the ledger, and the backfill
        pass must never charge across it as if central had merely been down."""
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, _now_iso())
            if exempt is not None:
                conn.execute("UPDATE orgs SET billing_exempt=? WHERE org_id=?",
                             (1 if exempt else 0, org_id))
            if deactivated is not None:
                conn.execute("UPDATE orgs SET deactivated=? WHERE org_id=?",
                             (1 if deactivated else 0, org_id))
            if resume_day is not None:
                conn.execute("UPDATE orgs SET billing_anchor_day=? WHERE org_id=?",
                             (resume_day, org_id))
            conn.commit()

    def set_org_billing_rates(self, org_id: str, *,
                              conn_rate_paise: int | None,
                              floor_paise: int | None) -> None:
        """Per-org overrides; NULL clears back to the global default. Applies
        forward only by construction — accrual rows store the rate they were
        charged at and are never rewritten."""
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, _now_iso())
            conn.execute(
                "UPDATE orgs SET conn_rate_paise=?, floor_paise=? WHERE org_id=?",
                (conn_rate_paise, floor_paise, org_id))
            conn.commit()

    def set_self_declared_conns(self, org_id: str, count: int | None,
                                by: str | None) -> None:
        with self._write_lock, self._connect() as conn:
            self._ensure_org(conn, org_id, _now_iso())
            conn.execute(
                "UPDATE orgs SET self_declared_conns=?, self_declared_by=?,"
                " self_declared_at=? WHERE org_id=?",
                (count, by if count is not None else None,
                 _now_iso() if count is not None else None, org_id))
            conn.commit()

    def global_billing_rates(self) -> tuple[int, int]:
        """(conn_rate_paise, device_floor_paise), read FRESH each use like the
        WhatsApp settings — no restart to change a price."""
        def _int(key: str, fallback: int) -> int:
            raw = self.get_setting(key)
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                return fallback
        return (_int("billing_conn_paise", metering.DEFAULT_CONN_PAISE),
                _int("billing_device_floor_paise", metering.DEFAULT_FLOOR_PAISE))

    def org_billing_rates(self, org_id: str) -> tuple[int, int]:
        g_conn, g_floor = self.global_billing_rates()
        b = self.org_billing(org_id)
        conn_rate = b["conn_rate_paise"] if b["conn_rate_paise"] is not None else g_conn
        floor = b["floor_paise"] if b["floor_paise"] is not None else g_floor
        return int(conn_rate), int(floor)

    # ------------------------------------------------------------- accruals

    def last_accrual_day(self, org_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(day) AS d FROM billing_accruals WHERE org_id=?",
                (org_id,)).fetchone()
        return row["d"] if row and row["d"] else None

    def insert_accrual(self, org_id: str, row) -> bool:
        """Idempotent: a day once written is never rewritten (the invoice is
        the sum of these rows — an accrual that changed after the fact would
        detach the chart from the bill). Returns True only on a fresh row."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO billing_accruals ({_ACCRUAL_COLS})"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (org_id, row.day, int(row.paise), int(row.conn_count),
                 row.conn_source, int(row.device_count), row.winning_side,
                 int(row.conn_rate_paise), int(row.floor_paise),
                 json.dumps(row.flags) if row.flags else None, _now_iso()))
            conn.commit()
        return bool(cur.rowcount)

    def clear_month_accruals(self, org_id: str, month: str) -> int:
        """Remove one month's accrual rows so the month can be re-priced.

        The ONLY writer in the ledger that deletes an accrual, and it exists
        for exactly one job: a change of BASIS on a month nobody has been
        billed for yet (2026-08-17, per-RADIUS-username to per-ONU). Refuses
        outright once an invoice exists for that month — an invoice is the SUM
        of its stored rows and is never recomputed, so rewriting the rows
        underneath an issued bill would detach the bill from the chart it is
        supposed to equal, which is the one thing this ledger promises.

        Returns the rows removed. tools/billing_reprice_month.py is the only
        caller; this must never become sweep behaviour.
        """
        with self._write_lock, self._connect() as conn:
            if conn.execute("SELECT 1 FROM billing_invoices"
                            " WHERE org_id=? AND month=?",
                            (org_id, month)).fetchone():
                raise ValueError(
                    f"{org_id} {month} is already invoiced. Accrual rows under"
                    " an issued invoice are never rewritten.")
            cur = conn.execute(
                "DELETE FROM billing_accruals WHERE org_id=? AND"
                " substr(day,1,7)=?", (org_id, month))
            conn.commit()
        return int(cur.rowcount)

    @staticmethod
    def _accrual_dict(r) -> dict:
        d = dict(r)
        d["flags"] = json.loads(d["flags"]) if d.get("flags") else {}
        return d

    def accrual_on(self, org_id: str, day: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_ACCRUAL_COLS} FROM billing_accruals"
                " WHERE org_id=? AND day=?", (org_id, day)).fetchone()
        return self._accrual_dict(row) if row else None

    def last_accrual(self, org_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_ACCRUAL_COLS} FROM billing_accruals"
                " WHERE org_id=? ORDER BY day DESC LIMIT 1",
                (org_id,)).fetchone()
        return self._accrual_dict(row) if row else None

    def accruals_since(self, org_id: str, first_day: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_ACCRUAL_COLS} FROM billing_accruals"
                " WHERE org_id=? AND day>=? ORDER BY day",
                (org_id, first_day)).fetchall()
        return [self._accrual_dict(r) for r in rows]

    def accruals_for_month(self, org_id: str, month: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_ACCRUAL_COLS} FROM billing_accruals"
                " WHERE org_id=? AND substr(day,1,7)=? ORDER BY day",
                (org_id, month)).fetchall()
        return [self._accrual_dict(r) for r in rows]

    def uninvoiced_months(self, org_id: str, before_month: str) -> list[dict]:
        """Accrued months strictly before `before_month` with no invoice row
        yet — the close pass's worklist (handles multi-month catch-up after
        downtime, not just "last month")."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT substr(day,1,7) AS month, SUM(paise) AS paise"
                "  FROM billing_accruals WHERE org_id=? AND substr(day,1,7) < ?"
                " GROUP BY substr(day,1,7)"
                " HAVING NOT EXISTS (SELECT 1 FROM billing_invoices i"
                "   WHERE i.org_id=billing_accruals.org_id"
                "     AND i.month=substr(billing_accruals.day,1,7))"
                " ORDER BY month", (org_id, before_month)).fetchall()
        return [dict(r) for r in rows]

    def sum_accrued(self, org_id: str) -> int:
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT COALESCE(SUM(paise),0) FROM billing_accruals"
                " WHERE org_id=?", (org_id,)).fetchone()[0])

    # ------------------------------------------------------------- invoices

    def ensure_invoice(self, org_id: str, month: str, paise: int,
                       issued_at: str | None = None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO billing_invoices"
                " (org_id, month, paise, issued_at, status)"
                " VALUES (?,?,?,?,'open')",
                (org_id, month, int(paise), issued_at or _now_iso()))
            conn.commit()
        return bool(cur.rowcount)

    def org_invoices(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT org_id, month, paise, issued_at, status"
                " FROM billing_invoices WHERE org_id=? ORDER BY month DESC",
                (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def org_invoice(self, org_id: str, month: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT org_id, month, paise, issued_at, status"
                " FROM billing_invoices WHERE org_id=? AND month=?",
                (org_id, month)).fetchone()
        return dict(row) if row else None

    def oldest_open_invoice(self, org_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT org_id, month, paise, issued_at, status"
                " FROM billing_invoices WHERE org_id=? AND status='open'"
                " ORDER BY month LIMIT 1", (org_id,)).fetchone()
        return dict(row) if row else None

    def set_invoice_status(self, org_id: str, month: str, status: str) -> None:
        if status not in INVOICE_STATES:
            raise ValueError(f"invoice status must be one of {INVOICE_STATES}")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE billing_invoices SET status=? WHERE org_id=? AND month=?",
                (status, org_id, month))
            conn.commit()

    def settle_invoices(self, org_id: str) -> None:
        """Allocate the payment total across invoices OLDEST FIRST and flip
        open<->paid to match. Idempotent, re-run after every payment and every
        sweep; 'void' rows are outside the allocation entirely (forgiving a
        month is the superadmin's adjustment to make, not this pass's)."""
        with self._write_lock, self._connect() as conn:
            paid_total = int(conn.execute(
                "SELECT COALESCE(SUM(paise),0) FROM billing_payments"
                " WHERE org_id=?", (org_id,)).fetchone()[0])
            rows = conn.execute(
                "SELECT month, paise, status FROM billing_invoices"
                " WHERE org_id=? AND status!='void' ORDER BY month",
                (org_id,)).fetchall()
            cum = 0
            for r in rows:
                cum += int(r["paise"])
                want = "paid" if paid_total >= cum else "open"
                if r["status"] != want:
                    conn.execute(
                        "UPDATE billing_invoices SET status=?"
                        " WHERE org_id=? AND month=?", (want, org_id, r["month"]))
            conn.commit()

    # ------------------------------------------------------------- payments

    def record_payment(self, org_id: str, paise: int, kind: str, *,
                       provider: str | None = None,
                       provider_payment_id: str | None = None,
                       provider_order_id: str | None = None,
                       note: str | None = None,
                       recorded_by: str | None = None) -> int | None:
        """Returns the new row id, or None when a provider_payment_id replay
        was ignored (webhook idempotency: the partial unique index makes a
        re-delivered event a no-op, never a double credit)."""
        if kind not in PAYMENT_KINDS:
            raise ValueError(f"payment kind must be one of {PAYMENT_KINDS}")
        paise = int(paise)
        if kind != "adjustment" and paise <= 0:
            raise ValueError("a payment must be a positive amount")
        if kind == "adjustment" and paise == 0:
            raise ValueError("an adjustment of zero records nothing")
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO billing_payments (org_id, paise, kind,"
                " provider, provider_payment_id, provider_order_id, note,"
                " recorded_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (org_id, paise, kind, provider, provider_payment_id or None,
                 provider_order_id, note, recorded_by, _now_iso()))
            conn.commit()
        return int(cur.lastrowid) if cur.rowcount else None

    def org_payments(self, org_id: str, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, org_id, paise, kind, provider, provider_payment_id,"
                " provider_order_id, note, recorded_by, created_at"
                " FROM billing_payments WHERE org_id=?"
                " ORDER BY id DESC LIMIT ?", (org_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def sum_paid(self, org_id: str) -> int:
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT COALESCE(SUM(paise),0) FROM billing_payments"
                " WHERE org_id=?", (org_id,)).fetchone()[0])

    def outstanding_paise(self, org_id: str) -> int:
        """SUM(accruals) - SUM(payments); computed, never stored. Negative is
        credit — advance payment IS the credit mechanism."""
        return self.sum_accrued(org_id) - self.sum_paid(org_id)

    # ---------------------------------------------------------- count feeds

    def onu_conn_count(self, org_id: str, since_iso: str) -> int:
        with self._connect() as conn:
            return int(self._with_norm_mac(conn).execute(
                _ONU_COUNT_SQL, (org_id, org_id, since_iso)).fetchone()[0])

    def onu_source_health(self, org_id: str) -> tuple[bool, str | None]:
        """(present, newest walk stamp). Present = any roster row under a
        live OLT; the newest onu_optics.updated_at is when an OLT last
        answered a walk — when every OLT is stale the latch holds."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(o.updated_at) AS t FROM onu_optics o"
                " JOIN org_devices d ON d.id=o.device_id AND d.is_active=1"
                " WHERE o.org_id=?", (org_id,)).fetchone()
        t = row["t"] if row else None
        return (t is not None), t

    # ----------------------------------------------------- superadmin views

    def billing_org_rows(self) -> list[dict]:
        """Every org with its billing flags — the sweep's roster (the old
        plan-filtered billing_orgs() died with the plans)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT org_id, name, billing_exempt, deactivated,"
                " conn_rate_paise, floor_paise,"
                " billing_anchor_day FROM orgs ORDER BY org_id").fetchall()
        return [dict(r) for r in rows]
