from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timezone

from wisp.central.store_history import DAY_S, HOUR_S
from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse

log = logging.getLogger("wisp.central.history")

# The maintenance thread folds complete UTC days and prunes. 6h, not 24h, so
# yesterday's fold lands within hours of midnight instead of up to a day late;
# folding and pruning are cheap (a few thousand rows) and idempotent.
MAINTENANCE_INTERVAL_S = 6 * 3600

# Catch-up folds after central downtime reach back at most this far: days
# older than the shortest hour-tier retention would fold from partially
# pruned hours and silently understate. Beyond it, the missed days stay gaps
# — which is the record.
CATCHUP_MAX_DAYS = 7


def epoch_s(ts: str) -> int:
    return int(_parse(ts).replace(tzinfo=timezone.utc).timestamp())


def day_floor(sec: int) -> int:
    return (int(sec) // DAY_S) * DAY_S


def rx_stats(values: list[float]) -> tuple[float | None, float | None, float | None]:
    # Nearest-rank percentiles — deterministic, no interpolation to explain.
    if not values:
        return None, None, None
    vs = sorted(values)
    n = len(vs)
    med = vs[round(0.5 * (n - 1))]
    p10 = vs[round(0.1 * (n - 1))]
    return med, p10, vs[0]


class OpticsAccumulator:
    # Rides along inside CentralOpticsMonitor.sync_device's existing loop, so
    # the historian samples exactly the numbers the badge and the rows get —
    # one path, after sane_rx and the web-optics merge. Percentiles are over
    # ONLINE ONUs whose walk carried a usable Rx; `measured` is that
    # population's size. ONUs with no pon_port count in the OLT totals and are
    # skipped from the per-PON rows (a PON that can't be named can't be
    # charted).

    def __init__(self) -> None:
        self.onus = 0
        self.online = 0
        self.warn = 0
        self.crit = 0
        self._rx: list[float] = []
        self._pons: dict[str, dict] = {}

    def add(self, pon_port, state: str, rx: float | None, sev: str) -> None:
        self.onus += 1
        is_online = state == "online"
        if is_online:
            self.online += 1
        if sev == "warn":
            self.warn += 1
        elif sev == "crit":
            self.crit += 1
        rxv = rx if (is_online and rx is not None) else None
        if rxv is not None:
            self._rx.append(rxv)
        pon = str(pon_port or "").strip()
        if pon:
            p = self._pons.setdefault(pon, {"onus": 0, "online": 0, "crit": 0,
                                            "rx": []})
            p["onus"] += 1
            if is_online:
                p["online"] += 1
            if sev == "crit":
                p["crit"] += 1
            if rxv is not None:
                p["rx"].append(rxv)

    def olt_row(self) -> dict:
        med, p10, mn = rx_stats(self._rx)
        return {"onus": self.onus, "online": self.online, "warn": self.warn,
                "crit": self.crit, "measured": len(self._rx), "rx_med": med,
                "rx_p10": p10, "rx_min": mn}

    def pon_rows(self) -> list[dict]:
        out = []
        for pon, p in self._pons.items():
            med, _, mn = rx_stats(p["rx"])
            out.append({"pon_port": pon, "onus": p["onus"],
                        "online": p["online"], "crit": p["crit"],
                        "rx_med": med, "rx_min": mn})
        return out


def record_optics(store, cfg: Config, org_id: str, device_id: int, ts: str,
                  acc: OpticsAccumulator) -> None:
    # Called once per optics walk that actually arrived — a missed sweep and a
    # down OLT write nothing, which is the gap grammar at the storage layer.
    # Never allowed to disturb the report cycle.
    if not cfg.hist_enabled:
        return
    try:
        store.record_olt_sweep(org_id, device_id, epoch_s(ts), acc.olt_row(),
                               acc.pon_rows())
    except Exception:
        log.exception("history: optics sample failed for %s/device=%d",
                      org_id, device_id)


def port_eligible(prior) -> bool:
    # The prior switch_ports row carries the operator columns a walk never
    # writes. A port's first-ever walk has no prior and is not eligible —
    # correct, since nobody has marked it yet.
    if prior is None:
        return False
    return bool(prior["monitored"]
                or prior["feeds_device_id"] is not None
                or prior["uplink_device_id"] is not None
                or prior["bw_threshold_mbps"] is not None
                or prior["bw_max_mbps"] is not None)


def record_ports(store, cfg: Config, org_id: str, device_id: int, ts: str,
                 rows: list[tuple]) -> None:
    if not cfg.hist_enabled or not rows:
        return
    try:
        store.record_port_sweeps(org_id, device_id, epoch_s(ts), rows)
    except Exception:
        log.exception("history: port sample failed for %s/device=%d",
                      org_id, device_id)


def record_radius_day(store, cfg: Config, org_ids, now: datetime | None = None) -> None:
    # One row per org per UTC day, only when EVERY enabled panel's latest read
    # was fully 'ok' — a partial export may be missing the status/expiry
    # columns themselves, and a runway trend built on that is garbage. The
    # counts reuse the customers page's own derivation (count agreement).
    if not cfg.hist_enabled:
        return
    from wisp.central import radius
    from wisp.central.customers import _date_formats
    from wisp.egress.notifiers import _display_zone

    now = now or datetime.now(timezone.utc)
    today_local = now.astimezone(_display_zone(cfg.display_tz)).date()
    day_s = day_floor(int(now.timestamp()))
    for org_id in sorted(set(org_ids)):
        try:
            accounts = store.org_radius_accounts(org_id, enabled_only=True)
            if not accounts:
                continue
            states = {s["account_id"]: s["state"]
                      for s in store.org_radius_status(org_id)}
            if any(states.get(int(a["id"])) != "ok" for a in accounts):
                continue
            formats = _date_formats(store, org_id)
            customers = active = expired = expiring7 = 0
            for c in store.org_radius_customer_rows(org_id):
                if c.get("seen_seq") != c.get("account_seq"):
                    continue
                customers += 1
                status = str(c.get("status") or "unknown")
                if status == "expired":
                    expired += 1
                elif status == "active":
                    active += 1
                    expiry_at = radius.parse_expiry(
                        c.get("expiry"), formats.get(int(c["account_id"]), ""))
                    days_left = radius.days_until(expiry_at, today_local)
                    if days_left is not None and 0 <= days_left <= 7:
                        expiring7 += 1
            store.upsert_radius_day(org_id, day_s, {
                "customers": customers, "active": active, "expired": expired,
                "expiring7": expiring7,
                "linked": len(store.org_radius_links(org_id))})
        except Exception:
            log.exception("history: radius day sample failed for org=%s", org_id)


def run_maintenance(store, cfg: Config = CONFIG, now_s: int | None = None) -> None:
    # Fold every complete UTC day the covered-through stamp hasn't reached,
    # then prune by age and enforce the caps. Idempotent at every step.
    now_s = int(now_s if now_s is not None else _time.time())
    today = day_floor(now_s)
    folded = store.hist_folded_through()
    if folded is None:
        folded = today - DAY_S
    start = max(folded + DAY_S, today - CATCHUP_MAX_DAYS * DAY_S)
    d = start
    while d < today:
        written = store.fold_history_day(d)
        if written:
            log.info("history: folded day %s (%d row(s))",
                     datetime.fromtimestamp(d, tz=timezone.utc).date(), written)
        d += DAY_S
    if today - DAY_S > folded:
        store.set_hist_folded_through(today - DAY_S)

    removed = store.prune_history({
        "hist_olt_sweep": now_s - cfg.hist_raw_hours * HOUR_S,
        "hist_port_sweep": now_s - cfg.hist_raw_hours * HOUR_S,
        "hist_olt_hour": now_s - cfg.hist_olt_hour_days * DAY_S,
        "hist_pon_hour": now_s - cfg.hist_pon_hour_days * DAY_S,
        "hist_port_hour": now_s - cfg.hist_port_hour_days * DAY_S,
        "hist_olt_day": now_s - cfg.hist_day_days * DAY_S,
        "hist_pon_day": now_s - cfg.hist_day_days * DAY_S,
        "hist_port_day": now_s - cfg.hist_day_days * DAY_S,
        "hist_device_day": now_s - cfg.hist_day_days * DAY_S,
        "hist_radius_day": now_s - cfg.hist_day_days * DAY_S,
    })
    if removed:
        log.info("history: pruned %s",
                 ", ".join(f"{t}={n}" for t, n in sorted(removed.items())))


def start_history_thread(cfg: Config = CONFIG, store=None) -> threading.Thread | None:
    if not cfg.hist_enabled:
        return None
    from wisp.central.store import CentralStore
    store = store or CentralStore(cfg.central_db)

    def _loop() -> None:
        log.info("history maintenance started (fold + prune every %ds)",
                 MAINTENANCE_INTERVAL_S)
        while True:
            try:
                run_maintenance(store, cfg)
            except Exception:
                log.exception("history maintenance failed; will retry next tick")
            _time.sleep(MAINTENANCE_INTERVAL_S)

    t = threading.Thread(target=_loop, name="wisp-central-history", daemon=True)
    t.start()
    return t
