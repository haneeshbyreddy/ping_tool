"""Central-side hourly latency/packet-loss rollups (CLAUDE.md item 2, second slice — the
trend-chart piece the outage-derived SLA slice in `central/analytics.py` deliberately
left open, since it needs its own time-series storage). Retention: 30 days.
Granularity: hourly buckets. Both decided; see CLAUDE.md's "Open questions".

Folded incrementally at each "full" `POST /report` cycle (never a `recheck` — that's
just the fast-confirm suspect subset sampled every couple seconds, which would badly
skew an hourly average), straight from that cycle's already-computed per-device
samples — so no raw per-poll history needs to be stored, and no separate ingest sweep
is needed either. Pruning runs on its own background thread (mirrors
`central/watchdog.py`'s pattern): central-brain mode has no other sweep loop to
piggyback on (the old edge's prune/rollup sweeps were edge-local and deleted in
Phase C — see CLAUDE.md).
"""
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
    """Floor an ISO8601 timestamp to its containing hour, naive UTC (matches every
    other central timestamp read through `core/analytics._parse`)."""
    dt = _parse(ts)
    return dt.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def record_cycle(store, tenant_id: str, eng, cycle, results: dict, ts: str) -> None:
    """Fold one full-report cycle's per-device samples into the current hour's running
    rollup. `eng`/`cycle`/`results` are the SAME objects `central/server.py:_report`
    already has right after `central_engine.run_cycle` — reused rather than re-derived,
    so this can never disagree with what `write_device_states` just persisted as the
    live state."""
    bucket = bucket_of(ts)
    entries = []
    for dev_id, state in cycle.states.items():
        dev = eng.meta[dev_id]
        res = results.get(dev.ip_address)
        latency = res.latency_ms if res else None
        loss = res.packet_loss if res else None
        down = 1 if state in DOWN_FAMILY else 0
        entries.append((tenant_id, dev_id, bucket, latency, loss, down))
    store.fold_device_rollups(entries)


def prune_old_rollups(store, now: str | None = None) -> int:
    """Delete every bucket older than RETENTION_DAYS, across all tenants — retention is
    a platform-wide policy for this slice, not per-org (see CLAUDE.md)."""
    end = _parse(now) if now else datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (end - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    return store.prune_rollups_older_than(cutoff)


def start_central_rollup_prune_thread(cfg: Config = CONFIG, store=None) -> threading.Thread:
    """A daily prune sweep on its own daemon thread, started alongside the fleet
    watchdog thread in `central/server.py:serve()`."""
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
