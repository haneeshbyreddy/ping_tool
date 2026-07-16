"""Subscription paywall: plan catalog, month math, and the reminder sweeper.

The model is deliberately manual — there is no payment gateway. A paid-plan org
pays the platform admin by GPay every month and the admin marks that month paid
(``org_billing_months``) from the Organizations page, as far ahead as he likes
(pre-marking future months IS the "no warning this month" switch). The pure
status math lives here; enforcement is elsewhere:

- dashboard lock: ``server.py``'s 402 gate (``org_locked``) — edge ingest,
  monitoring and outage paging are NEVER gated (a lapsed bill must not silence
  an alarm; the dashboard is the paywall, not the alerting).
- device cap: ``api/devices.create`` refuses past ``device_cap(plan)``.
  Existing devices keep working after a downgrade — the cap only stops adds.

Reminders are transition-only via ``billing_notices`` (watchdog pattern): the
owner topic is paged once when the paid runway drops to ≤3 days, and once when
the month starts unpaid and the dashboard locks. A failed send retries next
sweep; only 'sent'/'skipped' suppress the retry.

All month keys are 'YYYY-MM' in UTC.
"""
from __future__ import annotations

import calendar
import logging
import threading
import time as _time
from datetime import date, datetime, timezone

from wisp.config import CONFIG, Config
from wisp.egress.notifiers import build_notifier

log = logging.getLogger("wisp.central.billing")

DEFAULT_GPAY_NUMBER = "6309671515"
DUE_SOON_DAYS = 3
SWEEP_INTERVAL_S = 1800

# The product catalog. `device_cap` and the monthly lock are the ENFORCED
# limits; `features` are the pitch the dashboard renders. None = unlimited.
PLANS: dict[str, dict] = {
    "free": {
        "label": "Free",
        "price_inr": 0,
        "device_cap": 5,
        "node_cap": 1,
        "features": [
            "Up to 5 monitored devices",
            "ICMP outage detection & ntfy alerts",
            "Topology, map & live dashboard",
            "1 edge probe",
            "Community support",
        ],
    },
    "pro": {
        "label": "Pro",
        "price_inr": 2000,
        "device_cap": 500,
        "node_cap": 10,
        "features": [
            "Up to 500 monitored devices",
            "Everything in Free",
            "SNMP port, bandwidth & device-health monitoring",
            "GPON/EPON optical monitoring & fiber-fault localization",
            "Analytics, reliability reports & 30-day trends",
            "Up to 10 edge probes with staged self-updates",
            "Priority support (business hours)",
        ],
    },
    "vip": {
        "label": "VIP",
        "price_inr": 3000,
        "device_cap": None,
        "node_cap": None,
        "features": [
            "Unlimited monitored devices & probes",
            "Everything in Pro",
            "24/7 priority support",
            "Onboarding & vendor SNMP/GPON profile assistance",
            "Priority feature requests",
        ],
    },
}

PAID_PLANS = ("pro", "vip")


def clean_plan(raw) -> str | None:
    plan = str(raw or "").strip().lower()
    return plan if plan in PLANS else None


def device_cap(plan: str) -> int | None:
    return PLANS.get(plan, PLANS["free"])["device_cap"]


def node_cap(plan: str) -> int | None:
    return PLANS.get(plan, PLANS["free"])["node_cap"]


def gpay_number(store) -> str:
    return store.get_setting("billing_gpay_number") or DEFAULT_GPAY_NUMBER


def month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def next_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + (m == 12)}-{(m % 12) + 1:02d}"


def month_start(month: str) -> date:
    return date(int(month[:4]), int(month[5:7]), 1)


def month_label(month: str) -> str:
    return f"{calendar.month_name[int(month[5:7])]} {month[:4]}"


def compute_status(plan: str, paid_months: set[str],
                   now: datetime | None = None) -> dict:
    """Pure paywall verdict for one org. No I/O.

    status: 'free' (never locks) | 'active' | 'due_soon' (≤3 days of paid
    runway left) | 'locked' (current month unpaid). `due_month` is the first
    unpaid month; `days_left` counts down to its 1st (0 when locked).
    """
    now = now or datetime.now(timezone.utc)
    current = month_key(now)
    if plan not in PAID_PLANS:
        return {"plan": plan, "status": "free", "locked": False,
                "current_month": current, "paid_through": None,
                "due_month": None, "days_left": None}
    if current not in paid_months:
        return {"plan": plan, "status": "locked", "locked": True,
                "current_month": current, "paid_through": None,
                "due_month": current, "days_left": 0}
    last = current
    while next_month(last) in paid_months:
        last = next_month(last)
    due = next_month(last)
    days_left = (month_start(due) - now.date()).days
    return {"plan": plan,
            "status": "due_soon" if days_left <= DUE_SOON_DAYS else "active",
            "locked": False, "current_month": current, "paid_through": last,
            "due_month": due, "days_left": days_left}


def months_to_pay(plan: str, paid_months: set[str], count: int,
                  now: datetime | None = None) -> list[str]:
    """The next `count` unpaid months for `plan` — what one checkout buys.
    Starts at the due month (for a plan the org isn't on yet that's simply the
    current month) and skips months already marked paid, so a prepaid island
    never gets double-billed."""
    now = now or datetime.now(timezone.utc)
    month = compute_status(plan, paid_months, now)["due_month"] or month_key(now)
    out: list[str] = []
    while len(out) < count:
        if month not in paid_months:
            out.append(month)
        month = next_month(month)
    return out


def org_status(store, org_id: str, now: datetime | None = None) -> dict:
    return compute_status(store.org_plan(org_id), store.paid_months(org_id), now)


def org_locked(store, org_id: str, now: datetime | None = None) -> bool:
    return org_status(store, org_id, now)["locked"]


class BillingSweeper:
    """Pages each paid-plan org's owner topic on billing transitions only."""

    def __init__(self, store, cfg: Config = CONFIG, notifier=None) -> None:
        self.store = store
        self.cfg = cfg
        self.notifier = notifier or build_notifier(cfg)

    def check(self, now: datetime | None = None) -> list[tuple[str, str, str]]:
        now = now or datetime.now(timezone.utc)
        sent: list[tuple[str, str, str]] = []
        for org in self.store.billing_orgs():
            st = compute_status(org["plan"], self.store.paid_months(org["org_id"]), now)
            if st["status"] == "due_soon":
                if self._notify(org, "due_soon", st["due_month"], st, now):
                    sent.append((org["org_id"], st["due_month"], "due_soon"))
            elif st["locked"]:
                if self._notify(org, "locked", st["due_month"], st, now):
                    sent.append((org["org_id"], st["due_month"], "locked"))
        return sent

    def _notify(self, org: dict, kind: str, month: str, st: dict,
                now: datetime) -> bool:
        prior = self.store.billing_notice(org["org_id"], month, kind)
        if prior in ("sent", "skipped"):
            return False
        price = PLANS[org["plan"]]["price_inr"]
        # with Razorpay configured the dashboard IS the checkout; the GPay
        # number only rides the page as the manual fallback
        if (self.store.get_setting("razorpay_key_id")
                and self.store.get_setting("razorpay_key_secret")):
            how = "pay online from the dashboard"
        else:
            how = f"GPay {gpay_number(self.store)}"
        name = org["name"] or org["org_id"]
        if kind == "due_soon":
            title = f"💳 {name}: payment due in {st['days_left']} day{'s' if st['days_left'] != 1 else ''}"
            body = f"Pay ₹{price} for {month_label(month)} · {how}"
            priority = 4
        else:
            title = f"🔒 {name}: dashboard locked, payment due"
            body = f"Pay ₹{price} for {month_label(month)} · {how}"
            priority = 5
        topic = org.get("ntfy_topic_owner") or org.get("ntfy_topic")
        status = "skipped"
        if topic:
            ok = False
            try:
                ok = self.notifier.send(topic, title, body, priority).ok
            except Exception:
                log.exception("billing page failed for %s", org["org_id"])
            status = "sent" if ok else "failed"
        self.store.record_billing_notice(
            org["org_id"], month, kind, status,
            now.isoformat(timespec="seconds"))
        return status == "sent"


def start_central_billing_thread(cfg: Config = CONFIG, store=None,
                                 notifier=None) -> threading.Thread:
    from wisp.central.store import CentralStore
    store = store or CentralStore(cfg.central_db)
    sweeper = BillingSweeper(store, cfg, notifier)

    def _loop() -> None:
        log.info("central billing sweeper started (every %ss)", SWEEP_INTERVAL_S)
        while True:
            try:
                for org, month, kind in sweeper.check():
                    log.info("billing %s page sent for %s (%s)", kind, org, month)
            except Exception:
                log.exception("billing sweep failed; will retry next tick")
            _time.sleep(SWEEP_INTERVAL_S)

    t = threading.Thread(target=_loop, name="wisp-central-billing", daemon=True)
    t.worker = sweeper
    t.start()
    return t
