"""Central-side on-backup redundancy signal (CLAUDE.md item 3).

A device with a BACKUP parent edge (`org_device_links`, kind='backup' — see
`central/inventory.py:clean_backup_link` / `central/engine.py:load_device_meta`) that
loses its PRIMARY uplink but is still reachable via the backup is "running on backup":
a genuine heads-up (redundancy is gone, one more failure is an outage), not an outage
itself. `core/state_machine.MonitorEngine` already computes this in its full pass
(`CycleResult.redundancy`) — it was built generically enough in the old single-box tool
that NOTHING in the engine needed to change to support it here. This module is the
direct port of the old edge's `AlertDispatcher.redundancy_sweep` onto `CentralStore`'s
org-scoped `device_redundancy` table.

Not part of the outage/escalation ladder — on-backup is NOT louder (decision carried
over verbatim): it never opens an outage, and a node that has itself gone DOWN clears
its badge silently (the outage owns that story, not this). Operator-only page, gated by
`cfg.backup_alerts`; the badge is always written.
"""
from __future__ import annotations

from wisp.config import CONFIG, Config
from wisp.core.state_machine import DOWN_FAMILY


def sweep(store, org_id: str, eng, redundancy: dict[int, bool],
         states: dict[int, str], notifier, ts: str, cfg: Config = CONFIG) -> None:
    """`redundancy`/`states` are `CycleResult.redundancy`/`.states` from THIS cycle's
    full pass (`redundancy` is always empty on a recheck — the engine only computes it
    in the full pass — so calling this after every report, full or recheck, is safe;
    the caller still only bothers on a full report, same gating as ports/rollup)."""
    for dev_id, on_backup in redundancy.items():
        dev = eng.meta.get(dev_id)
        if dev is None:
            continue
        node_down = states.get(dev_id) in DOWN_FAMILY
        eff = bool(on_backup) and not node_down

        prior = store.device_redundancy_state(org_id, dev_id)
        was = bool(prior["on_backup"]) if prior else False
        # `since` marks when the current on-backup episode began (held across holds).
        since = (ts if (eff and not was)
                else (prior["primary_down_since"] if (prior and eff) else None))
        store.write_device_redundancy(org_id, dev_id, eff, since, ts)

        if eff == was:
            continue   # no edge — badge refreshed, nobody paged
        if eff:
            _page(store, org_id, notifier, cfg, dev_id,
                 f"\U0001f501 On backup — {dev.name} ({dev.region})",
                 f"{dev.name} ({dev.ip_address}) lost its primary uplink and is running "
                 f"on a backup path. It's still up, but redundancy is gone — one more "
                 f"failure is an outage.", "ON_BACKUP", ts)
        elif not node_down:
            # left on-backup because the primary path came back (not because the node
            # itself died) — a clean "redundancy restored" heads-up.
            _page(store, org_id, notifier, cfg, dev_id,
                 f"✅ Primary restored — {dev.name} ({dev.region})",
                 "Primary uplink is back; running on the primary path again.",
                 "BACKUP_CLEARED", ts)
        # else: node_down -> clear silently; the outage owns the alarm.


def _page(store, org_id: str, notifier, cfg: Config, device_id: int,
         title: str, body: str, payload: str, ts: str) -> None:
    topic = store.org_role_topic(org_id, "operator")
    if cfg.backup_alerts and topic:
        res = notifier.send(topic, title, body, 3)
        status = "sent" if res.ok else "failed"
    else:
        status = "suppressed"
    store.log_alert(org_id, None, device_id, notifier.channel, topic, status, payload, ts)
