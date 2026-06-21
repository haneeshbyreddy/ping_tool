"""Dead-monitor watchdog — the piece that watches the watcher.

The polling daemon and this dashboard are decoupled processes. If the daemon
crashes or wedges, it stops writing `poll_results`, and a naive dashboard would
still render an all-green, 100%-healthy network — the single most dangerous
failure mode a monitor has (a dead monitor looks identical to a healthy one).

This watchdog runs inside the always-on dashboard process, notices when the
newest poll has gone stale, and pages the owner that *the monitor itself is
down* — then once more, when polling resumes. It is deliberately conservative:

  * it never alarms before the daemon has polled at least once (fresh install),
    nor when there are no active devices (nothing to watch);
  * a one-off slow cycle can't trip it (the threshold is a few cadences, see
    `Config.stale_threshold_s`);
  * it rehydrates its alarm state from `alert_log`, so a dashboard restart never
    re-pages an already-known outage;
  * a failed page is not marked as delivered, so it is retried on the next tick.

It is the dashboard watching the daemon. If the whole box is off, neither runs —
that case is covered by the operator noticing their own office is dark (and,
optionally, systemd `WatchdogSec=` on the units).
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timezone

from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.database.client import connect, write_with_retry
from wisp.egress.notifiers import build_notifier, role_topic

log = logging.getLogger("wisp.watchdog")

# alert_log payload sentinels for the two transitions (also drive rehydration).
STALE_MARK = "MONITOR_STALE"
OK_MARK = "MONITOR_OK"


class MonitorWatchdog:
    """Stateful but tiny: holds whether the owner currently believes the monitor
    is down. `check()` is pure-ish (clock injectable) so it unit-tests with a
    recording notifier and a temp DB."""

    def __init__(self, cfg: Config = CONFIG, notifier=None) -> None:
        self.cfg = cfg
        self.notifier = notifier or build_notifier(cfg)
        self._alarm_active = self._last_alarm_state(cfg)

    @staticmethod
    def _last_alarm_state(cfg: Config) -> bool:
        """Restart safety: was the last *delivered* watchdog page a stale alarm?
        Only 'sent' rows count, so failed attempts don't strand us in 'alarmed'."""
        with connect(cfg) as conn:
            row = conn.execute(
                "SELECT payload FROM alert_log WHERE payload IN (?, ?)"
                " AND status = 'sent' ORDER BY id DESC LIMIT 1",
                (STALE_MARK, OK_MARK),
            ).fetchone()
        return bool(row and row["payload"] == STALE_MARK)

    def _snapshot(self, now: datetime) -> tuple[bool, float | None]:
        """(has_active_devices, seconds_since_last_poll). age None == never polled."""
        with connect(self.cfg) as conn:
            devices = conn.execute(
                "SELECT COUNT(*) FROM devices WHERE is_active = 1").fetchone()[0]
            last = conn.execute(
                "SELECT MAX(timestamp) AS t FROM poll_results").fetchone()["t"]
        if not last:
            return devices > 0, None
        return devices > 0, max(0.0, (now - _parse(last)).total_seconds())

    def check(self, now: datetime | None = None) -> str | None:
        """One evaluation. Returns 'alarm' | 'recover' | None — it only acts on a
        transition, so it is safe to call on a fixed timer."""
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        has_devices, age = self._snapshot(now)
        if not has_devices or age is None:
            return None  # nothing to watch / daemon hasn't started — never alarm
        stale = age > self.cfg.stale_threshold_s()

        if stale and not self._alarm_active:
            if self._page(STALE_MARK, age, now):
                self._alarm_active = True
                return "alarm"
            return None  # send failed — leave un-alarmed so the next tick retries
        if not stale and self._alarm_active:
            self._page(OK_MARK, age, now)
            self._alarm_active = False  # informational; clear regardless of send
            return "recover"
        return None

    def _page(self, mark: str, age: float, now: datetime) -> bool:
        mins = int(age // 60)
        if mark == STALE_MARK:
            title = "🚨 MONITOR DOWN"
            body = (f"No poll in ~{mins}m — the polling daemon may have crashed. "
                    "Outage detection is BLIND until it restarts.")
            priority = 5
        else:
            title = "✅ Monitor resumed"
            body = "Polling is back — outage detection is live again."
            priority = 3
        ok = self._broadcast(title, body, priority)

        ts = now.isoformat(timespec="seconds")
        recipient = role_topic("owner", self.cfg)

        def _do():
            with connect(self.cfg) as conn:
                conn.execute(
                    "INSERT INTO alert_log (outage_id, device_id, channel, recipient,"
                    " sent_at, status, payload) VALUES (?,?,?,?,?,?,?)",
                    (None, None, self.notifier.channel, recipient, ts,
                     "sent" if ok else "failed", mark),
                )
                conn.commit()
        write_with_retry(_do)
        return ok

    def _broadcast(self, title: str, body: str, priority: int) -> bool:
        """Owner channel + a copy to the operator (full visibility), mirroring the
        dispatcher. Returns whether the owner send succeeded."""
        owner = role_topic("owner", self.cfg)
        operator = role_topic("operator", self.cfg)
        res = self.notifier.send(owner, title, body, priority)
        if operator and operator != owner:
            self.notifier.send(operator, title, body, priority)
        return res.ok


def start_watchdog_thread(cfg: Config = CONFIG) -> threading.Thread:
    """Run the watchdog on a daemon thread inside the dashboard process. Checks at
    the poll cadence (floored at 30s), so it notices a dead daemon promptly without
    hammering the DB."""
    wd = MonitorWatchdog(cfg)
    interval = max(30, min(cfg.poll_interval_s, cfg.stale_threshold_s()))

    def _loop() -> None:
        log.info("monitor watchdog started (stale after %ss, checking every %ss)",
                 cfg.stale_threshold_s(), interval)
        while True:
            try:
                action = wd.check()
                if action == "alarm":
                    log.warning("monitor appears DOWN — paged the owner")
                elif action == "recover":
                    log.info("monitor resumed — notified the owner")
            except Exception:
                log.exception("watchdog check failed; will retry next tick")
            _time.sleep(interval)

    t = threading.Thread(target=_loop, name="wisp-watchdog", daemon=True)
    t.start()
    return t
