"""Notification governor — the paging policy that sits between every paging
shell and the notifier.

ntfy was removed 2026-07-24 and WhatsApp is the sole channel. With that, the
active notification set was cut to an ALLOWLIST (`_ACTIVE_KINDS`): only the
port up/down kinds that flow through this governor still page. Device/uplink
up/down go straight through `dispatch.py` (not this module), and probe up/down
through the watchdog — so between them the operator's chosen set is
device / uplink / port / probe, each up and down.

Everything else — the whole SNMP-derived stream (PON faults, ONU cap/dup-MAC,
perf, on-backup, optics) plus the hourly escalation re-nag — is turned OFF
"for now": `emit` logs it `suppressed` and sends nothing. The two-tier
PUSH/DIGEST machinery below is intact but DORMANT (no active kind routes to the
digest), kept because re-enabling a kind is a one-line edit to `_ACTIVE_KINDS`.

State rows are still written by the shells regardless of allowlist/gate, so the
dashboard stays fully live; this module only governs the *notification*.

WHO an active kind reaches is a second, independent narrowing: a device-scoped
alert goes to owners plus the field accounts responsible for that device
(``central/assignment.py``), while an unassigned device — and any kind with no
``device_id`` — still reaches the whole org audience. Allowlist first, then
audience: a kind that is off pages nobody regardless of assignment.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from wisp.central.assignment import PagingAudience
from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.egress.notifiers import NotifyResult, WhatsAppFacts

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
# The kinds that page since ntfy was removed (2026-07-24). Device/uplink up/down
# page via dispatch.py and probe up/down via the watchdog — neither routes
# through here — so the governor's job is port up/down PLUS port bandwidth
# floor/ceiling crossings and their clears (operator ask 2026-07-24: bandwidth
# high/low is wanted alongside device/port up-down). Everything else is
# suppressed (state still written by the shell). Re-enable a kind by adding it
# here; if it is also in `_DIGEST_KINDS` it resumes digesting.
_ACTIVE_KINDS = frozenset({
    "PORT_DOWN", "PORT_RESTORED",
    # Port bandwidth: floor/ceiling alarms AND their all-clears — a page without
    # its clear leaves the operator hanging. PUSH (not in _DIGEST_KINDS), gated
    # per-if_index with no cooldown (see ports.py:_page).
    "PORT_BW_HIGH", "PORT_BW_NORMAL",
    "PORT_BW_LOW", "PORT_BW_OK",
})

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
    # PUSH kinds, labelled like PORT_BW_* so that moving optics to the digest
    # later is a one-line change to _DIGEST_KINDS and not a formatting bug.
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
}


def tier_for(kind: str) -> str:
    return DIGEST if kind in _DIGEST_KINDS else PUSH


class AlertRouter:
    """One choke point replacing the `send + status + log_alert` trio every
    paging shell used to run inline. Constructed per sweep (cheap wrapper)."""

    def __init__(self, store, org_id: str, notifier, cfg: Config = CONFIG, *,
                 audience=None) -> None:
        self.store = store
        self.org_id = org_id
        self.notifier = notifier
        self.cfg = cfg
        # Resolves who is responsible for a device (central/assignment.py). Built
        # here when a shell doesn't pass one so EVERY router narrows by
        # assignment, including a shell added later — the same "a new route is
        # gated by default" instinct as _WORKER_ROUTES. A caller sharing its own
        # instance (dispatch does) just saves the tree read.
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

        # Allowlist and gate BEFORE resolving an audience: since ntfy was removed
        # only allowlisted kinds page, and the suppressed ones are most of the
        # SNMP stream — resolving a per-device audience for each would put three
        # extra queries on the report cycle for a page that was never going to be
        # sent. A suppressed row logs no recipient because there wasn't one; the
        # shell still wrote its state row, so the dashboard is live either way.
        if kind not in _ACTIVE_KINDS:
            _log("suppressed")
            return NotifyResult(False, "inactive kind")

        if not gate:
            _log("suppressed")
            return NotifyResult(False, "gated")

        # Owners plus the workers RESPONSIBLE for this device — its assignees or
        # ones inherited from an ancestor (central/assignment.py); an unassigned
        # device still reaches every worker, and an org-level kind (no device_id)
        # reaches the whole audience. No per-role routing beyond that (operator
        # choice 2026-07-24); the superadmin ops number is in neither (2026-07-25).
        numbers = (self.audience.for_device(device_id)
                   if whatsapp is None else list(whatsapp))
        recipient = ",".join(numbers) or None

        # DORMANT while no active kind is a digest kind; kept so re-enabling a
        # digest kind is just adding it to `_ACTIVE_KINDS`.
        if tier_for(kind) == DIGEST:
            self.store.queue_digest(self.org_id, device_id, kind, title, body, ts)
            _log("digest", recipient)
            return NotifyResult(True, "queued for digest")

        # PUSH — needs a live recipient. Empty here can also mean "assigned to
        # somebody who never set a WhatsApp number"; the assign API reports that
        # at assign time rather than this widening back to the whole team.
        if not numbers:
            _log("suppressed")
            return NotifyResult(False, "no whatsapp recipients")

        # Optional per-(device, kind) cooldown backstop against a flap.
        cd = self.cfg.alert_cooldown_min if cooldown_min is None else cooldown_min
        if cd > 0 and self.store.recently_pushed(
                self.org_id, device_id, kind, ts, cd):
            _log("suppressed", recipient)
            return NotifyResult(False, "cooldown")

        facts = wa_facts or WhatsAppFacts.derive(title, body, kind, ts)
        res = self.notifier.send(title, body, priority,
                                 whatsapp=numbers, facts=facts)
        _log("sent" if res.ok else "failed", recipient)
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
    title = f"📥 Summary · {total} event{'s' if total != 1 else ''}"
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

    numbers = list(store.org_alert_recipients(org_id))
    recipient = ",".join(numbers) or None
    title, body = compose_digest(rows)
    if numbers:
        res = notifier.send(title, body, 2, whatsapp=numbers,
                            facts=WhatsAppFacts.derive(title, body, "DIGEST", now_ts))
        status = "sent" if res.ok else "failed"
    else:
        status = "suppressed"
    store.log_alert(org_id, None, None, notifier.channel, recipient, status,
                    "DIGEST", now_ts, kind="DIGEST")
    if status != "failed":
        store.mark_digests_sent(org_id, now_ts)
