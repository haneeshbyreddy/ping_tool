from __future__ import annotations

import threading

from wisp.config import CONFIG, Config
from wisp.core.state_machine import (
    BACKUP,
    CycleResult,
    DeviceMeta,
    DOWN_FAMILY,
    Event,
    MonitorEngine,
    OutageOpened,
    OutageRecategorized,
    OutageResolved,
    ParentEdge,
)
from wisp.ingress.probers import PingResult

def load_device_meta(store, org_id: str) -> list[DeviceMeta]:
    edges = store.org_device_backup_edges(org_id)
    backups: dict[int, list[ParentEdge]] = {}
    for e in edges:
        backups.setdefault(e["child_id"], []).append(ParentEdge(e["parent_id"], BACKUP))
    return [
        DeviceMeta(id=r["id"], name=r["name"], ip_address=r["ip_address"],
                  region=r["region"], parent_device_id=r["parent_device_id"],
                  technician_phone=None, parents=tuple(backups.get(r["id"], ())))
        for r in store.org_device_topology(org_id)
    ]

def build_engine(store, org_id: str, cfg: Config = CONFIG) -> MonitorEngine:
    engine = MonitorEngine(load_device_meta(store, org_id), cfg)
    states = store.device_states(org_id)
    for dev_id, fsm in engine.fsm.items():
        row = states.get(dev_id)
        if row:
            fsm.prime(row["state"])
    if store.uplink_active(org_id):
        engine._uplink_active = True
    return engine

def apply_events(store, org_id: str, events: list[Event], ts: str) -> None:
    for ev in events:
        if isinstance(ev, OutageOpened):
            store.open_outage_if_absent(org_id, ev.device_id, ts, ev.state)
        elif isinstance(ev, OutageRecategorized):
            store.recategorize_outage(org_id, ev.device_id, ev.state)
        elif isinstance(ev, OutageResolved):
            store.resolve_outage(org_id, ev.device_id, ts)

class EngineRegistry:

    def __init__(self, store, cfg: Config = CONFIG) -> None:
        self.store = store
        self.cfg = cfg
        self._lock = threading.Lock()
        self._engines: dict[str, MonitorEngine] = {}
        self._fingerprints: dict[str, tuple] = {}

    @staticmethod
    def _fingerprint(devices: list[DeviceMeta]) -> tuple:
        return tuple(sorted((d.id, d.parent_device_id, d.parents) for d in devices))

    def get(self, org_id: str) -> MonitorEngine:
        devices = load_device_meta(self.store, org_id)
        fp = self._fingerprint(devices)
        with self._lock:
            if self._fingerprints.get(org_id) != fp:
                engine = MonitorEngine(devices, self.cfg)
                states = self.store.device_states(org_id)
                for dev_id, fsm in engine.fsm.items():
                    row = states.get(dev_id)
                    if row:
                        fsm.prime(row["state"])
                if self.store.uplink_active(org_id):
                    engine._uplink_active = True
                self._engines[org_id] = engine
                self._fingerprints[org_id] = fp
            return self._engines[org_id]

def run_cycle(store, org_id: str, engine: MonitorEngine,
             results: dict[str, PingResult], ts: str,
             subset: set[int] | None = None,
             expected_ips: set[str] | None = None) -> CycleResult:
    cycle = engine.process_cycle(results, ts, subset=subset, expected_ips=expected_ips)
    apply_events(store, org_id, cycle.events, ts)
    rows = []
    for dev_id, state in cycle.states.items():
        dev = engine.meta[dev_id]
        res = results.get(dev.ip_address)
        rows.append((dev_id, state,
                    res.latency_ms if res else None,
                    res.packet_loss if res else None,
                    res.jitter_ms if res else None))
    store.write_device_states(org_id, rows, ts)
    return cycle

def compute_recheck(engine: MonitorEngine, cycle: CycleResult,
                    results: dict[str, PingResult], cfg: Config = CONFIG) -> dict:
    if cycle.canary_down:
        return {}
    down_ips, up_ips = [], []
    for dev_id, state in cycle.states.items():
        dev = engine.meta[dev_id]
        res = results.get(dev.ip_address)
        loss = res.packet_loss if res else 100.0
        if state not in DOWN_FAMILY and loss >= 100.0:
            down_ips.append(dev.ip_address)
        elif state in DOWN_FAMILY and loss < 100.0:
            up_ips.append(dev.ip_address)
    if not down_ips and not up_ips:
        return {}
    return {"down_ips": sorted(down_ips), "up_ips": sorted(up_ips),
           "interval_s": cfg.retry_interval_s}
