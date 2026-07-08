from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone

from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.core.state_machine import DOWN_FAMILY

log = logging.getLogger("wisp.central.rollup")

RETENTION_DAYS = 30
BUCKET_HOURS = 1

def bucket_of(ts: str) -> str:
    dt = _parse(ts)
    return dt.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

def record_cycle(store, org_id: str, eng, cycle, results: dict, ts: str) -> None:
    bucket = bucket_of(ts)
    entries = []
    for dev_id, state in cycle.states.items():
        dev = eng.meta[dev_id]
        res = results.get(dev.ip_address)
        latency = res.latency_ms if res else None
        loss = res.packet_loss if res else None
        down = 1 if state in DOWN_FAMILY else 0
        entries.append((org_id, dev_id, bucket, latency, loss, down))
    store.fold_device_rollups(entries)

def prune_old_rollups(store, now: str | None = None) -> int:
    end = _parse(now) if now else datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (end - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    return store.prune_rollups_older_than(cutoff)

def start_central_rollup_prune_thread(cfg: Config = CONFIG, store=None) -> threading.Thread:
    from wisp.central.store import CentralStore
    store = store or CentralStore(cfg.central_db)
    interval = 24 * 3600

    def _loop() -> None:
        log.info("central rollup prune sweep started (retention=%dd, every %ds)",
                 RETENTION_DAYS, interval)
        while True:
            try:
                removed = prune_old_rollups(store)
                if removed:
                    log.info("rollup prune: removed %d bucket(s) older than %dd",
                             removed, RETENTION_DAYS)
            except Exception:
                log.exception("rollup prune sweep failed; will retry next tick")
            _time.sleep(interval)

    t = threading.Thread(target=_loop, name="wisp-central-rollup-prune", daemon=True)
    t.start()
    return t
