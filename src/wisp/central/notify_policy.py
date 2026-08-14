from __future__ import annotations

import logging
from collections import defaultdict

from wisp.central.assignment import PagingAudience
from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.egress.notifiers import NotifyResult, WhatsAppFacts, queue_send

log = logging.getLogger(__name__)

PUSH = "push"
DIGEST = "digest"

_ACTIVE_KINDS = frozenset({
    "PORT_DOWN", "PORT_RESTORED",
    "PORT_BW_HIGH", "PORT_BW_NORMAL",
    "PORT_BW_LOW", "PORT_BW_OK",
    "CAMERA_DOWN", "CAMERA_RESTORED",
})

_DIGEST_KINDS = frozenset({
    "PON_FAULT", "PON_RECOVERED",
    "ONU_LIMIT", "ONU_DUP_MAC",
    "PERF_DEGRADED", "PERF_RECOVERED",
    "ON_BACKUP", "BACKUP_CLEARED",
    "HOURLY_ESCALATION",
})

_KIND_LABELS = {
    "PON_FAULT": "🔦 PON faults",
    "PON_RECOVERED": "✅ PON recovered",
    "OPTICAL_CRIT": "🔻 Optical critical",
    "OPTICAL_RECOVERED": "✅ Optical recovered",
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
    "CAMERA_DOWN": "📷 Camera down",
    "CAMERA_RESTORED": "✅ Camera restored",
}


def tier_for(kind: str) -> str:
    return DIGEST if kind in _DIGEST_KINDS else PUSH


class AlertRouter:
    def __init__(self, store, org_id: str, notifier, cfg: Config = CONFIG, *,
                 audience=None) -> None:
        self.store = store
        self.org_id = org_id
        self.notifier = notifier
        self.cfg = cfg
        self.audience = audience if audience is not None else PagingAudience(
            store, org_id)

    def emit(self, kind: str, *, title: str, body: str,
             priority: int, ts: str, outage_id: int | None = None,
             device_id: int | None = None, payload: str | None = None,
             gate: bool = True, cooldown_min: int | None = None,
             whatsapp=None, wa_facts: WhatsAppFacts | None = None) -> NotifyResult:
        payload = payload if payload is not None else kind
        channel = self.notifier.channel

        def _log(status: str, recipient: str | None = None) -> None:
            self.store.log_alert(self.org_id, outage_id, device_id, channel,
                                 recipient, status, payload, ts, kind=kind)

        if kind not in _ACTIVE_KINDS:
            _log("suppressed")
            return NotifyResult(False, "inactive kind")

        if not gate:
            _log("suppressed")
            return NotifyResult(False, "gated")

        numbers = (self.audience.for_device(device_id)
                   if whatsapp is None else list(whatsapp))
        recipient = ",".join(numbers) or None

        if tier_for(kind) == DIGEST:
            self.store.queue_digest(self.org_id, device_id, kind, title, body, ts)
            _log("digest", recipient)
            return NotifyResult(True, "queued for digest")

        if not numbers:
            _log("suppressed")
            return NotifyResult(False, "no whatsapp recipients")

        cd = self.cfg.alert_cooldown_min if cooldown_min is None else cooldown_min
        if cd > 0 and self.store.recently_pushed(
                self.org_id, device_id, kind, ts, cd):
            _log("suppressed", recipient)
            return NotifyResult(False, "cooldown")

        facts = wa_facts or WhatsAppFacts.derive(title, body, kind, ts)
        return queue_send(
            self.notifier, title, body, priority, whatsapp=numbers, facts=facts,
            on_result=lambda res: _log("sent" if res.ok else "failed", recipient))


def compose_digest(rows: list[dict]) -> tuple[str, str]:
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
    title = f"📥 Summary · {total} event{'s' if total != 1 else ''}"
    return title, "\n".join(lines)


def flush_digests(store, org_id: str, notifier, cfg: Config, now_ts: str) -> None:
    rows = store.pending_digest(org_id)
    if not rows:
        return
    age_s = (_parse(now_ts) - _parse(rows[0]["created_at"])).total_seconds()
    if age_s < cfg.digest_interval_min * 60:
        return

    numbers = list(store.org_alert_recipients(org_id))
    recipient = ",".join(numbers) or None
    title, body = compose_digest(rows)
    channel = notifier.channel
    if not numbers:
        store.log_alert(org_id, None, None, channel, recipient, "suppressed",
                        "DIGEST", now_ts, kind="DIGEST")
        store.mark_digests_sent(org_id, now_ts)
        return

    def _done(res) -> None:
        status = "sent" if res.ok else "failed"
        store.log_alert(org_id, None, None, channel, recipient, status,
                        "DIGEST", now_ts, kind="DIGEST")
        if status != "failed":
            store.mark_digests_sent(org_id, now_ts)

    queue_send(notifier, title, body, 2, whatsapp=numbers,
               facts=WhatsAppFacts.derive(title, body, "DIGEST", now_ts),
               on_result=_done)
