"""Phase B — central runs the brain: per-tenant MonitorEngine over org_devices.

`core/state_machine.MonitorEngine` is pure and DB-agnostic; the edge's own
`load_device_meta`/`build_engine`/`apply_events` (bottom of that file) are just its DB
glue over the edge's single-tenant schema. This module is the same glue over
`CentralStore`'s multi-tenant schema — `org_devices` (Phase A's ISP-managed topology)
supplies `DeviceMeta`, `device_states` is the rehydration source (mirrors the edge reading
back the last `poll_results` row).

Central's HTTP handling is stateless per request, but the FSM's flap-suppression counters
(`down_streak`/`nondown_streak`/…) must survive across an edge's successive `POST /report`
calls, or a device could never accumulate `down_consecutive` samples — one HTTP request
would only ever feed it one sample. `EngineRegistry` keeps one live `MonitorEngine` per
tenant in memory (the direct analogue of the daemon's own long-lived `engine` variable in
`run_forever`), rebuilding only when that tenant's topology actually changed — same
device-set-reload check the daemon does at the top of every cycle.
"""
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


def load_device_meta(store, tenant_id: str) -> list[DeviceMeta]:
    """DeviceMeta's BACKUP parent edges (CLAUDE.md item 3) come from `org_device_links` —
    the PRIMARY parent stays `parent_device_id`, unchanged. The engine itself needed NO
    changes to support this: `DeviceMeta.effective_parents()`/topology suppression/
    redundancy math were all built generically in Phase 9 already; central's job is just
    wiring the extra edges in."""
    edges = store.org_device_backup_edges(tenant_id)
    backups: dict[int, list[ParentEdge]] = {}
    for e in edges:
        backups.setdefault(e["child_id"], []).append(ParentEdge(e["parent_id"], BACKUP))
    return [
        DeviceMeta(id=r["id"], name=r["name"], ip_address=r["ip_address"],
                  region=r["region"], parent_device_id=r["parent_device_id"],
                  technician_phone=None, parents=tuple(backups.get(r["id"], ())))
        for r in store.org_device_topology(tenant_id)
    ]


def build_engine(store, tenant_id: str, cfg: Config = CONFIG) -> MonitorEngine:
    """Construct the engine and rehydrate each FSM from `device_states` so a central
    restart continues instead of re-paging every open outage (mirrors the edge's
    `build_engine`)."""
    engine = MonitorEngine(load_device_meta(store, tenant_id), cfg)
    states = store.device_states(tenant_id)
    for dev_id, fsm in engine.fsm.items():
        row = states.get(dev_id)
        if row:
            fsm.prime(row["state"])
    if store.uplink_active(tenant_id):
        engine._uplink_active = True
    return engine


def apply_events(store, tenant_id: str, events: list[Event], ts: str) -> None:
    """Persist outage open/recategorize/resolve (mirrors
    `core/state_machine.apply_events`, tenant-scoped)."""
    for ev in events:
        if isinstance(ev, OutageOpened):
            store.open_outage_if_absent(tenant_id, ev.device_id, ts, ev.state)
        elif isinstance(ev, OutageRecategorized):
            store.recategorize_outage(tenant_id, ev.device_id, ev.state)
        elif isinstance(ev, OutageResolved):
            store.resolve_outage(tenant_id, ev.device_id, ts)


class EngineRegistry:
    """One live `MonitorEngine` per tenant, rebuilt only on a topology change. Thread-safe
    (central's `ThreadingHTTPServer` can process reports for different tenants — or
    concurrent retries for the same one — on different threads)."""

    def __init__(self, store, cfg: Config = CONFIG) -> None:
        self.store = store
        self.cfg = cfg
        self._lock = threading.Lock()
        self._engines: dict[str, MonitorEngine] = {}
        self._fingerprints: dict[str, tuple] = {}

    @staticmethod
    def _fingerprint(devices: list[DeviceMeta]) -> tuple:
        """A cheap topology signature (id + primary parent + backup edges per device) —
        changes on any add/remove/reparent/maintenance-toggle/backup-link edit, exactly
        the edge daemon's own reload trigger, so a stale in-memory engine never silently
        ignores a topology edit. `d.parents` is a tuple of hashable `ParentEdge`s, so it
        sorts/compares fine inside the outer tuple."""
        return tuple(sorted((d.id, d.parent_device_id, d.parents) for d in devices))

    def get(self, tenant_id: str) -> MonitorEngine:
        devices = load_device_meta(self.store, tenant_id)
        fp = self._fingerprint(devices)
        with self._lock:
            if self._fingerprints.get(tenant_id) != fp:
                engine = MonitorEngine(devices, self.cfg)
                states = self.store.device_states(tenant_id)
                for dev_id, fsm in engine.fsm.items():
                    row = states.get(dev_id)
                    if row:
                        fsm.prime(row["state"])
                if self.store.uplink_active(tenant_id):
                    engine._uplink_active = True
                self._engines[tenant_id] = engine
                self._fingerprints[tenant_id] = fp
            return self._engines[tenant_id]


def run_cycle(store, tenant_id: str, engine: MonitorEngine,
             results: dict[str, PingResult], ts: str,
             subset: set[int] | None = None,
             expected_ips: set[str] | None = None) -> CycleResult:
    """One tenant's report -> one engine cycle -> persisted outages + live state. Takes
    an already-fetched engine (from `EngineRegistry.get`) rather than the registry
    itself, so the caller (central/server.py) can reuse the SAME engine instance for the
    alert dispatcher afterwards (it needs `engine.meta` for device names/regions) without
    a second registry lookup. This function only owns FSM state, not alerting — the
    caller is responsible for feeding `cycle.events` to
    `central/dispatch.py`'s `CentralAlertDispatcher`.

    `subset` mirrors the edge's own confirmation-pass mode (`MonitorEngine.process_cycle`'s
    `subset` param): when given, only those device ids advance — used for a fast-confirm
    "recheck" report, which only carries samples for the suspect IPs, not the whole
    fleet. `cycle.states` (and so the `device_states` write below) is naturally already
    scoped to just the fed devices in that case — nothing else needs to change.

    `expected_ips` (full pass only — device assignment, CLAUDE.md's multi-edge-per-tenant
    feature) narrows which devices THIS report can commit; see
    `MonitorEngine.process_cycle`'s own docstring for why that's the right place to skip
    an out-of-scope device instead of scoring it 100% loss."""
    cycle = engine.process_cycle(results, ts, subset=subset, expected_ips=expected_ips)
    apply_events(store, tenant_id, cycle.events, ts)
    rows = []
    for dev_id, state in cycle.states.items():
        dev = engine.meta[dev_id]
        res = results.get(dev.ip_address)
        rows.append((dev_id, state,
                    res.latency_ms if res else None,
                    res.packet_loss if res else None,
                    res.jitter_ms if res else None))
    store.write_device_states(tenant_id, rows, ts)
    return cycle


def compute_recheck(engine: MonitorEngine, cycle: CycleResult,
                    results: dict[str, PingResult], cfg: Config = CONFIG) -> dict:
    """The fast-confirm round-trip hint (see CLAUDE.md's design rationale): which IPs are
    worth re-probing right away, mirroring the same suspect-set logic the old single-box
    daemon's `_confirm_down`/`_confirm_up` used before Phase C moved the FSM here — just
    IP-keyed (the wire convention) instead of device-id-keyed.

    * `down_ips` — committed state NOT in DOWN_FAMILY, but this cycle's sample was 100%
      loss (a down-streak started but hasn't reached `down_consecutive` yet).
    * `up_ips`   — committed state IS in DOWN_FAMILY, but this cycle's sample was
      reachable (a recovery streak started but hasn't reached `recover_consecutive` yet).

    Self-terminating by construction, no attempt counter needed: a suspect leaves the set
    the moment its streak either commits (crosses the threshold) or resets (a fresh
    lost/healthy sample breaks it) — `central/server.py` calls this again after EVERY
    report (full or recheck) and the edge just keeps following the hint until it's empty.
    Empty lists (or missing keys) mean nothing to recheck; the caller omits `recheck`
    from the reply entirely in that case. A frozen cycle (`cycle.canary_down` — the whole
    uplink is down and `canary_freeze` suppressed every local transition) never yields a
    hint: rapid rechecking would just work around the freeze it was frozen to avoid."""
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
