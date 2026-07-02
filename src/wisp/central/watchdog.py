"""Cross-edge fleet watchdog (Phase 10 Part B) — the dead-monitor watchdog, one level up.

The edge's `server/watchdog.py` pages when *its own* polling goes stale. Central runs the
same idea across the fleet: when a node's **heartbeat** goes silent — the edge box is dead OR
its WAN is cut — central pages that node's **org** ("edge-a1 is dark; central is blind to it").
The edge still alarms locally if it can; this is the org-wide safety net for the case where it
can't reach anyone, or the whole box is gone.

It mirrors the edge watchdog's discipline exactly:
  * acts only on a transition (stale→page once, resumed→page once), safe on a fixed timer;
  * restart-safe — per-node alarm state is rehydrated from `node_alerts` (only *sent* rows
    count), so a central restart never re-pages a node that was already known down;
  * conservative — a node whose `last_seen` is recent is never alarmed (one missed beat can't
    trip it; `central_node_stale_s` is a multiple of the heartbeat interval);
  * a failed page is logged 'failed' and retried next tick, never stranding the alarm.

The page is per-org: the org's `ntfy_topic` if set, else `cfg.central_ntfy_topic`. Like the
edge notifier, the ntfy send lazy-imports httpx — central core + the test suite stay stdlib,
and tests inject a recording-notifier double (no real network).
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timezone

from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.egress.notifiers import build_notifier

log = logging.getLogger("wisp.central.watchdog")

STALE_MARK = "NODE_STALE"
OK_MARK = "NODE_OK"


class CentralWatchdog:
    def __init__(self, store, cfg: Config = CONFIG, notifier=None) -> None:
        self.store = store
        self.cfg = cfg
        self.notifier = notifier or build_notifier(cfg)
        # (org, node) -> currently-alarmed?  Seeded lazily from the DB (restart safety).
        self._alarm: dict[tuple[str, str], bool] = {}

    def _alarmed(self, key: tuple[str, str]) -> bool:
        if key not in self._alarm:
            self._alarm[key] = self.store.last_node_alarm(*key)
        return self._alarm[key]

    def check(self, now: datetime | None = None) -> list[tuple[str, str, str]]:
        """One evaluation across every node. Returns the (org, node, 'alarm'|'recover')
        transitions it acted on — so it's safe to call on a fixed timer."""
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
                # send failed -> leave un-alarmed so the next tick retries
            elif not stale and self._alarmed(key):
                self._page(key, OK_MARK, age, now)
                self._alarm[key] = False  # clear regardless; OK is informational
                transitions.append((org, node, "recover"))
        return transitions

    def _page(self, key: tuple[str, str], mark: str, age: float, now: datetime) -> bool:
        org, node = key
        topic = self.store.org_topic(org) or self.cfg.central_ntfy_topic
        mins = int(age // 60)
        if mark == STALE_MARK:
            title = "🚨 EDGE NODE DOWN"
            body = (f"{org}/{node}: no heartbeat in ~{mins}m — the edge box may be down "
                    f"or its WAN cut. Central is blind to this site until it returns.")
            priority = 5
        else:
            title = "✅ Edge node back"
            body = f"{org}/{node}: heartbeats resumed — central is in sync again."
            priority = 3
        ok = False
        if topic:
            try:
                ok = self.notifier.send(topic, title, body, priority).ok
            except Exception:
                log.exception("central watchdog page failed for %s/%s", org, node)
        self.store.record_node_alert(org, node, mark, "sent" if ok else "failed",
                                     f"age={int(age)}s",
                                     now.replace(tzinfo=timezone.utc).isoformat(timespec="seconds"))
        return ok


def start_central_watchdog_thread(cfg: Config = CONFIG, store=None,
                                  notifier=None) -> threading.Thread:
    """Run the fleet watchdog on a daemon thread inside the central server process."""
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
    t.worker = wd  # type: ignore[attr-defined]
    t.start()
    return t
