from __future__ import annotations

from wisp.config import CONFIG, Config
from wisp.core.state_machine import DOWN_FAMILY

def sweep(store, org_id: str, eng, redundancy: dict[int, bool],
         states: dict[int, str], notifier, ts: str, cfg: Config = CONFIG) -> None:
    for dev_id, on_backup in redundancy.items():
        dev = eng.meta.get(dev_id)
        if dev is None:
            continue
        node_down = states.get(dev_id) in DOWN_FAMILY
        eff = bool(on_backup) and not node_down

        prior = store.device_redundancy_state(org_id, dev_id)
        was = bool(prior["on_backup"]) if prior else False
        since = (ts if (eff and not was)
                else (prior["primary_down_since"] if (prior and eff) else None))
        store.write_device_redundancy(org_id, dev_id, eff, since, ts)

        if eff == was:
            continue
        if eff:
            _page(store, org_id, notifier, cfg, dev_id,
                 f"\U0001f501 On backup — {dev.name} ({dev.region})",
                 f"{dev.name} ({dev.ip_address}) lost its primary uplink and is running "
                 f"on a backup path. It's still up, but redundancy is gone — one more "
                 f"failure is an outage.", "ON_BACKUP", ts)
        elif not node_down:
            _page(store, org_id, notifier, cfg, dev_id,
                 f"✅ Primary restored — {dev.name} ({dev.region})",
                 "Primary uplink is back; running on the primary path again.",
                 "BACKUP_CLEARED", ts)

def _page(store, org_id: str, notifier, cfg: Config, device_id: int,
         title: str, body: str, payload: str, ts: str) -> None:
    topic = store.org_role_topic(org_id, "operator")
    if cfg.backup_alerts and topic:
        res = notifier.send(topic, title, body, 3)
        status = "sent" if res.ok else "failed"
    else:
        status = "suppressed"
    store.log_alert(org_id, None, device_id, notifier.channel, topic, status, payload, ts)
