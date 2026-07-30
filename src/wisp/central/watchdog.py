from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timezone

from wisp.central.assignment import PagingAudience
from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.egress.notifiers import WhatsAppFacts, build_notifier

log = logging.getLogger("wisp.central.watchdog")

STALE_MARK = "NODE_STALE"
OK_MARK = "NODE_OK"

class CentralWatchdog:
    def __init__(self, store, cfg: Config = CONFIG, notifier=None) -> None:
        self.store = store
        self.cfg = cfg
        self.notifier = notifier or build_notifier(cfg, store)
        self._alarm: dict[tuple[str, str], bool] = {}

    def _alarmed(self, key: tuple[str, str]) -> bool:
        if key not in self._alarm:
            self._alarm[key] = self.store.last_node_alarm(*key)
        return self._alarm[key]

    def check(self, now: datetime | None = None) -> list[tuple[str, str, str]]:
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        threshold = self.cfg.central_node_stale_s
        transitions: list[tuple[str, str, str]] = []
        for row in self.store.node_liveness():
            org, node = row["org_id"], row["node_id"]
            key = (org, node)
            age = max(0.0, (now - _parse(row["last_seen"])).total_seconds())
            stale = age > threshold
            if stale and not self._alarmed(key):
                if self._page(key, STALE_MARK, age, now):
                    self._alarm[key] = True
                    transitions.append((org, node, "alarm"))
            elif not stale and self._alarmed(key):
                self._page(key, OK_MARK, age, now)
                self._alarm[key] = False
                transitions.append((org, node, "recover"))
        return transitions

    def _page(self, key: tuple[str, str], mark: str, age: float, now: datetime) -> bool:
        org, node = key
        mins = int(age // 60)
        ts = now.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        if mark == STALE_MARK:
            title, status = "🚨 PROBE DOWN", "DOWN"
            body = f"{org}/{node} · silent ~{mins}m"
            priority = 5
        else:
            title, status = "✅ Probe back", "UP"
            body = f"{org}/{node}"
            priority = 3
        # A silent probe reaches owners plus whoever is responsible for the
        # devices that probe carries (central/assignment.py) — a probe is not a
        # device, so its audience is derived from what sits behind it. With
        # nothing assigned back there it stays the whole org audience: a dark
        # probe blinds a slice of the fleet and is the last alarm to narrow on a
        # guess. The superadmin ops number is NOT paged here (2026-07-25): a
        # probe-down is an org page like any other, and the admin can't be buried
        # under every org's fleet churn.
        #
        # A fresh resolver per page (not per tick): the watchdog thread is
        # long-lived, and a cached tree would go on paging a stale audience for
        # as long as central stays up.
        numbers = PagingAudience(self.store, org).for_node(node)
        ok = False
        if numbers:
            try:
                ok = self.notifier.send(
                    title, body, priority, whatsapp=numbers,
                    facts=WhatsAppFacts(subject=f"Probe {node} ({org})",
                                        status=status, detail=body,
                                        timestamp=ts)).ok
            except Exception:
                log.exception("central watchdog page failed for %s/%s", org, node)
        self.store.record_node_alert(org, node, mark, "sent" if ok else "failed",
                                     f"age={int(age)}s", ts)
        return ok

def start_central_watchdog_thread(cfg: Config = CONFIG, store=None,
                                  notifier=None) -> threading.Thread:
    from wisp.central.store import CentralStore
    store = store or CentralStore(cfg.central_db)
    wd = CentralWatchdog(store, cfg, notifier)
    interval = cfg.central_watchdog_interval_s or max(30, cfg.central_node_stale_s // 2)

    def _loop() -> None:
        log.info("central fleet watchdog started (node stale after %ss, every %ss)",
                 cfg.central_node_stale_s, interval)
        while True:
            try:
                for org, node, action in wd.check():
                    if action == "alarm":
                        log.warning("edge node %s/%s appears DOWN — paged the org", org, node)
                    else:
                        log.info("edge node %s/%s resumed — notified the org", org, node)
            except Exception:
                log.exception("central watchdog check failed; will retry next tick")
            _time.sleep(interval)

    t = threading.Thread(target=_loop, name="wisp-central-watchdog", daemon=True)
    t.worker = wd
    t.start()
    return t
