"""Notification governor — the tier + digest policy that sits between every
paging shell and the notifier.

Central used to fire one ntfy message per finding, immediately, at priority 3,
straight to the operator topic. On a fleet whose C-Data/DBC EPON agents can't
report dying-gasp/LOS, an area power cut darkens many PONs across many OLTs at
once and every one misclassifies as a fiber cut — so a single DISCOM outage
produced dozens of pages, ntfy's free quota 429'd ~half of them, and the drops
took *real* device-down pages down with them (they share one quota).

The fix is two tiers, not a new transport:

  * PUSH   — buzz the phone now: ICMP device/uplink/port down and their
             recoveries, PLUS port bandwidth floor/ceiling crossings and their
             clears (operator ask 2026-07-18 — a link maxing out or going dark
             is time-sensitive, not summary material). Already transition- and
             hysteresis-gated, low volume.
  * DIGEST — the rest of the SNMP-derived stream (PON faults, ONU cap/dup-MAC,
             perf, on-backup) plus the hourly escalation re-nag. Queued to
             `alert_digest` and rolled into ONE summary per org every
             `cfg.digest_interval_min`.

Unknown kinds default to PUSH — a new alert type must never be silently buried.
State rows are still written by the shells regardless of tier/gate, so the
dashboard stays fully live; this module only governs the *notification*.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.egress.notifiers import NotifyResult

log = logging.getLogger(__name__)

PUSH = "push"
DIGEST = "digest"

# Kinds that roll into the hourly digest instead of buzzing the phone. Anything
# not listed here is PUSH (fail loud, not silent). Recoveries of PUSH alerts
# (DEVICE_RESTORED / UPLINK_RESTORED / PORT_RESTORED, and the PORT_BW_* clears)
# are intentionally absent so they push too — a page without its "all clear"
# leaves the operator hanging. Port bandwidth (PORT_BW_LOW/OK/HIGH/NORMAL) is
# PUSH by operator ask 2026-07-18 — a saturated or dark uplink can't wait for
# the hourly roll-up.
_DIGEST_KINDS = frozenset({
    "PON_FAULT", "PON_RECOVERED",
    "ONU_LIMIT", "ONU_DUP_MAC",
    "PERF_DEGRADED", "PERF_RECOVERED",
    "ON_BACKUP", "BACKUP_CLEARED",
    "HOURLY_ESCALATION",
})

# Friendly section headers for the digest body, longest-lived first.
_KIND_LABELS = {
    "PON_FAULT": "🔦 PON faults",
    "PON_RECOVERED": "✅ PON recovered",
    "PORT_BW_LOW": "📉 Low bandwidth",
    "PORT_BW_OK": "📈 Bandwidth recovered",
    "PORT_BW_HIGH": "📈 High bandwidth",
    "PORT_BW_NORMAL": "📉 Bandwidth normalized",
    "ONU_LIMIT": "🔴 PON at capacity",
    "ONU_DUP_MAC": "⚠️ Duplicate ONU MAC",
    "PERF_DEGRADED": "🐌 Slow links",
    "PERF_RECOVERED": "✅ Performance recovered",
    "ON_BACKUP": "🔁 On backup",
    "BACKUP_CLEARED": "✅ Primary restored",
    "HOURLY_ESCALATION": "⏰ Still down",
}


def tier_for(kind: str) -> str:
    return DIGEST if kind in _DIGEST_KINDS else PUSH


class AlertRouter:
    """One choke point replacing the `send + status + log_alert` trio every
    paging shell used to run inline. Constructed per sweep (cheap wrapper)."""

    def __init__(self, store, org_id: str, notifier, cfg: Config = CONFIG) -> None:
        self.store = store
        self.org_id = org_id
        self.notifier = notifier
        self.cfg = cfg

    def emit(self, kind: str, *, topic: str | None, title: str, body: str,
             priority: int, ts: str, outage_id: int | None = None,
             device_id: int | None = None, payload: str | None = None,
             gate: bool = True, cooldown_min: int | None = None) -> NotifyResult:
        payload = payload if payload is not None else kind
        channel = self.notifier.channel

        def _log(status: str) -> None:
            self.store.log_alert(self.org_id, outage_id, device_id, channel,
                                 topic, status, payload, ts, kind=kind)

        # Gate off (or no channel) — the shell still wrote its state row.
        if not (gate and topic):
            _log("suppressed")
            return NotifyResult(False, "gated")

        if tier_for(kind) == DIGEST:
            self.store.queue_digest(self.org_id, device_id, kind, title, body, ts)
            _log("digest")
            return NotifyResult(True, "queued for digest")

        # PUSH — optional per-(device, kind) cooldown backstop against a flap.
        cd = self.cfg.alert_cooldown_min if cooldown_min is None else cooldown_min
        if cd > 0 and self.store.recently_pushed(
                self.org_id, device_id, kind, ts, cd):
            _log("suppressed")
            return NotifyResult(False, "cooldown")

        res = self.notifier.send(topic, title, body, priority)
        _log("sent" if res.ok else "failed")
        return res


def compose_digest(rows: list[dict]) -> tuple[str, str]:
    """Pure: roll queued digest rows into one (title, body). Grouped by kind,
    biggest group first, at most a few example titles each."""
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r)
    total = len(rows)
    lines: list[str] = []
    for kind in sorted(by_kind, key=lambda k: (-len(by_kind[k]), k)):
        items = by_kind[kind]
        lines.append(f"{_KIND_LABELS.get(kind, kind)} ({len(items)})")
        for it in items[:3]:
            lines.append(f"  • {it.get('title') or ''}")
        if len(items) > 3:
            lines.append(f"  … +{len(items) - 3} more")
    title = f"📥 Summary — {total} event{'s' if total != 1 else ''}"
    return title, "\n".join(lines)


def flush_digests(store, org_id: str, notifier, cfg: Config, now_ts: str) -> None:
    """Send one digest for `org_id` if the oldest queued row is at least
    `cfg.digest_interval_min` old. Anchoring on the oldest row means no per-org
    clock state is needed. A failed send is NOT marked flushed (retries next
    cycle); a missing topic is (nothing to retry, and the rows live on the
    dashboard). Rides the full `/report` sweep, like escalation sweeping."""
    rows = store.pending_digest(org_id)
    if not rows:
        return
    age_s = (_parse(now_ts) - _parse(rows[0]["created_at"])).total_seconds()
    if age_s < cfg.digest_interval_min * 60:
        return

    topic = store.org_role_topic(org_id, "operator")
    title, body = compose_digest(rows)
    if topic:
        res = notifier.send(topic, title, body, 2)
        status = "sent" if res.ok else "failed"
    else:
        status = "suppressed"
    store.log_alert(org_id, None, None, notifier.channel, topic, status,
                    "DIGEST", now_ts, kind="DIGEST")
    if status != "failed":
        store.mark_digests_sent(org_id, now_ts)
