from __future__ import annotations

import calendar
import logging
import threading
import time as _time
from datetime import date, datetime, timezone

from wisp.config import CONFIG, Config
from wisp.egress.notifiers import WhatsAppFacts, build_notifier

log = logging.getLogger("wisp.central.billing")

DEFAULT_GPAY_NUMBER = "6309671515"
DUE_SOON_DAYS = 3
SWEEP_INTERVAL_S = 1800

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


def org_status(store, org_id: str, now: datetime | None = None) -> dict:
    return compute_status(store.org_plan(org_id), store.paid_months(org_id), now)


def org_locked(store, org_id: str, now: datetime | None = None) -> bool:
    return org_status(store, org_id, now)["locked"]


class BillingSweeper:
    def __init__(self, store, cfg: Config = CONFIG, notifier=None) -> None:
        self.store = store
        self.cfg = cfg
        self.notifier = notifier or build_notifier(cfg, store)

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
        plan = PLANS[org["plan"]]
        price, label = plan["price_inr"], plan["label"]
        how = f"GPay {gpay_number(self.store)}."
        name = org["name"] or org["org_id"]
        if kind == "due_soon":
            days = st["days_left"]
            title = f"💳 {name}: {label} renews in {days} day{'s' if days != 1 else ''}"
            body = f"₹{price} keeps your full dashboard live for {month_label(month)}. {how}"
            priority = 4
        else:
            title = f"🔒 {name}: unlock your dashboard"
            body = f"Renew {month_label(month)} and you're back in. {how}"
            priority = 5
        numbers = list(self.store.org_alert_recipients(org["org_id"]))
        status = "skipped"
        if numbers:
            ok = False
            try:
                ok = self.notifier.send(
                    title, body, priority, whatsapp=numbers,
                    facts=WhatsAppFacts(
                        subject=name,
                        status="RENEWAL DUE" if kind == "due_soon" else "LOCKED",
                        detail=body,
                        timestamp=now.isoformat(timespec="seconds"))).ok
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
