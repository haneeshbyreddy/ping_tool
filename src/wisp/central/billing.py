"""Billing v2: the metered postpaid ledger engine (operator decision
2026-08-17 — plans, paid months, GPay and the manual paywall are GONE).

Division of labour: central/metering.py is the pure math (counts + rates in,
an accrual row out — the FSM discipline); store_billing.py is the persistence
mixin; central/dunning.py is the paging policy; THIS module is the glue — the
sweep thread and the status/gate reads server.py and the API compose.

THE METER IS THE ONU ROSTER (operator decision 2026-08-17): the billable
connection is a subscriber ONU seen online in the last 7 days, counted by
distinct MAC. RADIUS is not a metering input — `radius_conn_count` and
`radius_source_health` were deleted with the rung, so there is no dormant path
for a bill to fall back onto.

Three invariants survive from v1 and are load-bearing:
  * edge ingest, monitoring and WhatsApp paging are NEVER gated by billing;
  * the sweep loop never dies on one bad tick;
  * deactivation is a superadmin CLICK, never automatic — a lapsed bill must
    not silence an alarm.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone

from wisp.central import metering
from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.egress.notifiers import _display_zone, build_notifier

log = logging.getLogger("wisp.central.billing")

SWEEP_INTERVAL_S = 1800

# Re-exported for the API layer (one definition of month arithmetic).
month_key = metering.month_key
next_month = metering.next_month
month_start = metering.month_start
month_label = metering.month_label


def operator_today(cfg: Config = CONFIG, now: datetime | None = None):
    """The operator's calendar date (WISP_DISPLAY_TZ) — the billing day
    boundary, same precedent as worker tracking's "today"."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(_display_zone(cfg.display_tz)).date()


def _age_days(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        then = _parse(str(stamp))
    except (ValueError, TypeError):
        return None
    ref = now.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0.0, (ref - then).total_seconds() / 86400.0)


# ------------------------------------------------------------------- status

def org_status(store, org_id: str, now: datetime | None = None,
               cfg: Config = CONFIG) -> dict:
    """The billing document the SPA hero, the banner and the locked screen
    all read. One shape for every state — the honest headline is composed
    client-side from these facts, never guessed."""
    now = now or datetime.now(timezone.utc)
    today = operator_today(cfg, now)
    b = store.org_billing(org_id)
    open_inv = store.oldest_open_invoice(org_id)
    ladder = metering.ladder_stage(
        open_inv["month"] if open_inv else None, today,
        exempt=b["exempt"], deactivated=b["deactivated"])
    outstanding = store.outstanding_paise(org_id)
    credit = max(0, -outstanding)
    today_row = store.accrual_on(org_id, today.isoformat())
    conn_rate, floor = store.org_billing_rates(org_id)
    lasts = None
    if credit and today_row and today_row["paise"] > 0:
        lasts = metering.credit_lasts_until(credit, today_row["paise"], today)
    return {
        "org_id": org_id,
        "exempt": b["exempt"],
        "deactivated": b["deactivated"],
        "outstanding_paise": outstanding,
        "credit_paise": credit,
        "credit_lasts_until": lasts.isoformat() if lasts else None,
        "open_invoice": open_inv,
        "stage": ladder["stage"],
        "locked": ladder["locked"],
        "days_overdue": ladder["days_overdue"],
        "deactivation_candidate": ladder["deactivation_candidate"],
        "today": today_row,
        "rates": {
            "conn_paise": conn_rate,
            "floor_paise": floor,
            "conn_override": b["conn_rate_paise"] is not None,
            "floor_override": b["floor_paise"] is not None,
        },
    }


def org_locked(store, org_id: str, now: datetime | None = None,
               cfg: Config = CONFIG) -> bool:
    """server.py's 402 gate. Anchored to the OLDEST OPEN INVOICE, never to
    outstanding (a new org is NEVER locked before its first invoice). Runs on
    every /api request — two indexed point reads, keep it that way."""
    b = store.org_billing(org_id)
    # DEACTIVATED outranks EXEMPT, and the order must match
    # metering.ladder_stage's or the gate and the document disagree: an org
    # carrying both flags rendered as switched off while every /api route
    # still answered 200. Deactivation is not a billing concession at all,
    # it is the superadmin's off switch, so it wins.
    if b["deactivated"]:
        return True
    if b["exempt"]:
        return False
    open_inv = store.oldest_open_invoice(org_id)
    if not open_inv:
        return False
    today = operator_today(cfg, now)
    return metering.days_overdue(open_inv["month"], today) > metering.BANNER_DAYS


# -------------------------------------------------------------- the sweeper

class BillingSweeper:
    """Idempotent daily engine, run every SWEEP_INTERVAL_S: (a) accrue every
    org up to the operator's today (backfilling downtime gaps, flagged and
    bounded), (b) close finished months into invoices, (c) settle invoice
    statuses against the payment total, (d) hand the dunning ladder to
    central/dunning.py. Every step tolerates re-running."""

    def __init__(self, store, cfg: Config = CONFIG, notifier=None) -> None:
        self.store = store
        self.cfg = cfg
        self.notifier = notifier or build_notifier(cfg, store)

    # -- (a) accrual ------------------------------------------------------

    def onu_source(self, org_id: str, now: datetime) -> metering.Source:
        """The ONE metering read: distinct ONUs online inside the window, plus
        the health of the walk that measured them. The bill is per ONU
        (2026-08-17) — there is no second feed to fall back on, which is why
        the latch in metering.resolve_count is load-bearing rather than
        belt-and-braces.

        The cutoff is computed HERE, from the sweep's own `now`, so a backfill
        and tonight's live tick use the same window arithmetic."""
        present, stamp = self.store.onu_source_health(org_id)
        cutoff = (now.astimezone(timezone.utc)
                  - timedelta(days=metering.ONU_ONLINE_WINDOW_DAYS))
        return metering.Source(
            present=present,
            count=(self.store.onu_conn_count(
                org_id, cutoff.isoformat(timespec="seconds")) if present else 0),
            age_days=_age_days(stamp, now))

    def accrue_org(self, org: dict, now: datetime) -> list[str]:
        org_id = org["org_id"]
        today = operator_today(self.cfg, now)
        last = self.store.last_accrual_day(org_id)
        prior = self.store.last_accrual(org_id)

        # The catch-up window: the day after the last row, bounded by the
        # anchor (days spent exempt/deactivated are a deliberate hole) and by
        # BACKFILL_MAX_DAYS (a longer gap stays an honest hole).
        start = today
        if last:
            try:
                nxt = (datetime.strptime(last, "%Y-%m-%d").date()
                       + timedelta(days=1))
                start = min(today, nxt)
            except ValueError:
                start = today
        floor_day = today - timedelta(days=metering.BACKFILL_MAX_DAYS)
        if start < floor_day:
            start = floor_day
        anchor = org.get("billing_anchor_day")
        if anchor:
            try:
                a = datetime.strptime(anchor, "%Y-%m-%d").date()
                if start < a:
                    start = min(a, today)
            except ValueError:
                pass

        written: list[str] = []
        day = start
        while day < today:
            # Backfilled days carry the prior row forward; with no prior row
            # there is nothing honest to carry — the gap stays.
            if prior is not None:
                row = metering.carry_forward(metering.AccrualRow(
                    day=prior["day"], paise=prior["paise"],
                    conn_count=prior["conn_count"],
                    conn_source=prior["conn_source"],
                    device_count=prior["device_count"],
                    winning_side=prior["winning_side"],
                    conn_rate_paise=prior["conn_rate_paise"],
                    floor_paise=prior["floor_paise"],
                    flags=prior["flags"]), day.isoformat())
                if self.store.insert_accrual(org_id, row):
                    written.append(row.day)
            day += timedelta(days=1)

        if self.store.accrual_on(org_id, today.isoformat()) is None:
            prior_eff = None
            if prior is not None:
                prior_eff = metering.effective_source(
                    prior["conn_source"], prior["flags"])
                if prior_eff == "none":
                    prior_eff = None
            count, source, flags, _eff = metering.resolve_count(
                self.onu_source(org_id, now), prior_eff)
            conn_rate, floor = self.store.org_billing_rates(org_id)
            devices = self.store.org_monitored_device_count(
                org_id, self._passive_types())
            row = metering.accrue_day(today.isoformat(), count, source, flags,
                                      devices, conn_rate, floor)
            if self.store.insert_accrual(org_id, row):
                written.append(row.day)
        return written

    @staticmethod
    def _passive_types() -> tuple[str, ...]:
        from wisp.central import inventory
        return inventory.PASSIVE_TYPES

    # -- (b)+(c) invoices -------------------------------------------------

    def close_invoices(self, org_id: str, now: datetime) -> list[str]:
        current = metering.month_key(operator_today(self.cfg, now))
        closed: list[str] = []
        for m in self.store.uninvoiced_months(org_id, current):
            if int(m["paise"]) <= 0:
                continue  # a zero month owes nothing; no invoice, no ladder
            if self.store.ensure_invoice(org_id, m["month"], int(m["paise"])):
                closed.append(m["month"])
        self.store.settle_invoices(org_id)
        return closed

    # -- the sweep --------------------------------------------------------

    def sweep(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        accrued: dict[str, list[str]] = {}
        invoiced: dict[str, list[str]] = {}
        for org in self.store.billing_org_rows():
            org_id = org["org_id"]
            try:
                # Exempt/deactivated orgs accrue NOTHING (no phantom debt),
                # but months already accrued still close and settle — usage
                # that happened before the flag flipped stays owed.
                if not (org.get("billing_exempt") or org.get("deactivated")):
                    days = self.accrue_org(org, now)
                    if days:
                        accrued[org_id] = days
                months = self.close_invoices(org_id, now)
                if months:
                    invoiced[org_id] = months
            except Exception:
                log.exception("billing sweep failed for %s; next org", org_id)
        try:
            from wisp.central import dunning
            dunning.sweep(self.store, self.cfg, self.notifier, now)
        except Exception:
            log.exception("dunning pass failed; will retry next tick")
        return {"accrued": accrued, "invoiced": invoiced}

    # Kept as the thread's entry point name from v1 (tests poke t.worker).
    def check(self, now: datetime | None = None) -> dict:
        return self.sweep(now)


def start_central_billing_thread(cfg: Config = CONFIG, store=None,
                                 notifier=None) -> threading.Thread:
    from wisp.central.store import CentralStore
    store = store or CentralStore(cfg.central_db)
    sweeper = BillingSweeper(store, cfg, notifier)

    def _loop() -> None:
        log.info("central billing sweeper started (every %ss)", SWEEP_INTERVAL_S)
        while True:
            try:
                out = sweeper.sweep()
                if out["accrued"] or out["invoiced"]:
                    log.info("billing sweep: accrued %s, invoiced %s",
                             out["accrued"] or "-", out["invoiced"] or "-")
            except Exception:
                log.exception("billing sweep failed; will retry next tick")
            _time.sleep(SWEEP_INTERVAL_S)

    t = threading.Thread(target=_loop, name="wisp-central-billing", daemon=True)
    t.worker = sweeper
    t.start()
    return t
