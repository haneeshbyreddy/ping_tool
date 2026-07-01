"""Layer 2 — Pattern Recognition.

The brains: turns raw per-poll samples into confirmed states and outages, with
flap suppression, recovery hysteresis, and two network-aware overrides:

  * Uplink canary   — if our own internet is down, freeze everything and raise
                      ONE Uplink_Down instead of a storm of per-hub alerts.
  * Topology        — a child of a DOWN parent becomes UNREACHABLE (suppressed),
                      not separately DOWN.

`MonitorEngine` is deliberately pure: it takes a dict of {ip: PingResult} plus a
timestamp and returns committed states + a list of events, with no I/O of its own —
`central/engine.py` owns building/rehydrating it and persisting its events against
central's multi-tenant store, so the decision logic here can be unit-tested with no
database at all.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import NamedTuple

from wisp.config import CONFIG, Config
from wisp.ingress.probers import PingResult

# --- States -----------------------------------------------------------------
UP = "UP"
DEGRADED = "DEGRADED"
DOWN = "DOWN"
UNREACHABLE = "UNREACHABLE"
DOWN_FAMILY = frozenset({DOWN, UNREACHABLE})

# --- Topology edge kinds (Phase 9 Part A) -----------------------------------
PRIMARY = "primary"   # the denormalized parent_device_id path
BACKUP = "backup"     # a redundant path from device_links


class ParentEdge(NamedTuple):
    """One parent relationship: which node feeds this one, over which kind of path.
    The PRIMARY edge is synthesized from `parent_device_id`; BACKUP edges come from
    `device_links`. NamedTuple so it stays cheap, hashable, and value-comparable
    (DeviceMeta equality drives the daemon's device-set reload)."""
    parent_id: int
    kind: str


@dataclass
class DeviceMeta:
    id: int
    name: str
    ip_address: str
    region: str | None
    parent_device_id: int | None
    technician_phone: str | None
    # BACKUP parent edges only (device_links). The primary path stays the single source
    # of truth on `parent_device_id`; `effective_parents()` combines the two. Empty for
    # devices with no redundancy (every legacy single-parent node).
    parents: tuple[ParentEdge, ...] = ()

    def effective_parents(self) -> tuple[ParentEdge, ...]:
        """All parent edges: the PRIMARY (parent_device_id) plus any BACKUP edges.
        One source of truth per concept — primary on the device row, backups in
        device_links — so a single-parent node behaves byte-for-byte as before."""
        edges: list[ParentEdge] = []
        if self.parent_device_id is not None:
            edges.append(ParentEdge(self.parent_device_id, PRIMARY))
        edges.extend(self.parents)
        return tuple(edges)


# --- Events the engine emits (daemon/notifier act on these) -----------------
@dataclass
class OutageOpened:
    device_id: int
    state: str            # DOWN or UNREACHABLE


@dataclass
class OutageRecategorized:
    device_id: int
    state: str


@dataclass
class OutageResolved:
    device_id: int


@dataclass
class UplinkDown:
    pass


@dataclass
class UplinkRestored:
    pass


Event = (
    OutageOpened | OutageRecategorized | OutageResolved | UplinkDown | UplinkRestored
)


@dataclass
class CycleResult:
    states: dict[int, str]
    events: list[Event]
    canary_down: bool
    # device_id -> is the node currently running on a BACKUP path (primary parent down,
    # a backup parent alive, the node itself still reachable)? Only redundancy-capable
    # devices (those with a backup edge) appear. Computed in the full pass only; the
    # daemon persists the badge + pages the operator on an edge (see redundancy_sweep).
    redundancy: dict[int, bool] = field(default_factory=dict)


# --- Per-device finite state machine (pure) ---------------------------------
class DeviceFSM:
    """Holds the consecutive-sample counters that implement flap suppression and
    recovery hysteresis for one device."""

    def __init__(self, state: str = UP) -> None:
        self.state = state
        self.down_streak = 0
        self.nondown_streak = 0
        self.degraded_streak = 0
        self.healthy_streak = 0
        self.prime(state)

    def prime(self, state: str) -> None:
        """Rehydrate to a stable state after a restart so we don't re-page."""
        self.state = state
        self.down_streak = self.nondown_streak = 0
        self.degraded_streak = self.healthy_streak = 0
        if state in DOWN_FAMILY:
            self.down_streak = 99
        elif state == DEGRADED:
            self.degraded_streak = self.nondown_streak = 99
        else:
            self.healthy_streak = self.nondown_streak = 99

    def feed(self, result: PingResult, cfg: Config) -> tuple[str, str]:
        """Advance the FSM by one sample. Returns (previous_state, new_state).
        Never emits UNREACHABLE — that is a topology override applied outside."""
        prev = self.state

        if result.packet_loss >= 100.0:
            self.down_streak += 1
            self.nondown_streak = self.degraded_streak = self.healthy_streak = 0
        else:
            self.down_streak = 0
            self.nondown_streak += 1
            healthy = (
                result.latency_ms is not None
                and result.latency_ms < cfg.latency_threshold_ms
                and result.packet_loss < cfg.loss_degraded_pct
            )
            if healthy:
                self.healthy_streak += 1
                self.degraded_streak = 0
            else:
                self.degraded_streak += 1
                self.healthy_streak = 0

        if self.down_streak >= cfg.down_consecutive:
            new = DOWN
        elif prev in DOWN_FAMILY:
            # hysteresis: need sustained recovery to leave a down state
            if self.healthy_streak >= cfg.recover_consecutive:
                new = UP
            elif self.nondown_streak >= cfg.recover_consecutive:
                new = DEGRADED          # back, but impaired — still closes the outage
            else:
                new = prev
        elif self.degraded_streak >= cfg.degraded_consecutive:
            new = DEGRADED
        elif prev == DEGRADED:
            new = UP if self.healthy_streak >= cfg.recover_consecutive else DEGRADED
        else:
            new = UP

        self.state = new
        return prev, new


# --- The engine -------------------------------------------------------------
class MonitorEngine:
    def __init__(self, devices: list[DeviceMeta], cfg: Config = CONFIG) -> None:
        self.cfg = cfg
        self.meta: dict[int, DeviceMeta] = {d.id: d for d in devices}
        self.fsm: dict[int, DeviceFSM] = {d.id: DeviceFSM() for d in devices}
        self._order = self._topological_order(devices)
        self._uplink_active = False  # are we currently in an Uplink_Down condition?

    @staticmethod
    def _topological_order(devices: list[DeviceMeta]) -> list[int]:
        """Parents before children, so every parent's new state is known when we
        evaluate a child for topology suppression. Kahn's algorithm by in-degree over
        the *full* edge set (primary + backups), so a node with two parents lands after
        BOTH. With exactly one parent this is identical to the old single-parent queue.
        Cycles (which Kahn's detects as leftovers) are appended best-effort by id."""
        ids = {d.id for d in devices}
        parents_of = {
            d.id: [e.parent_id for e in d.effective_parents() if e.parent_id in ids]
            for d in devices
        }
        children_of: dict[int, list[int]] = {}
        for d in devices:
            for pid in parents_of[d.id]:
                children_of.setdefault(pid, []).append(d.id)
        indeg = {d.id: len(parents_of[d.id]) for d in devices}
        # Seed in device order (ORDER BY id) so the result is deterministic.
        queue = deque(d.id for d in devices if indeg[d.id] == 0)
        order: list[int] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for child in children_of.get(nid, []):
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)
        if len(order) < len(devices):  # cycle(s): emit the rest best-effort, by id
            placed = set(order)
            order.extend(sorted(d.id for d in devices if d.id not in placed))
        return order

    def required_ips(self) -> set[str]:
        ips = {d.ip_address for d in self.meta.values()}
        ips.add(self.cfg.canary_ip)
        return ips

    def probe_plan(self) -> dict[str, int]:
        """Per-IP ping count for one cycle (keys == required_ips). Aggregation nodes
        — any device that is a *parent* of another — are probed gently
        (`pings_per_poll_infra`) so a tower/switch/AP control plane doesn't trip its
        ICMP rate-limiter and report phantom loss; leaf CPEs and the canary get the
        full `pings_per_poll`. Topology-derived, so no schema/UI change: a node
        becomes 'infra' the moment something is parented under it."""
        cfg = self.cfg
        # "Is a parent" now means "appears as ANY parent_id in the edge set" — a node
        # that is only a *backup* parent still backhauls traffic, so it earns the gentle
        # infra cadence too.
        parent_ids = {
            e.parent_id
            for d in self.meta.values()
            for e in d.effective_parents()
            if e.parent_id in self.meta
        }
        plan: dict[str, int] = {
            d.ip_address: (cfg.pings_per_poll_infra if d.id in parent_ids else cfg.pings_per_poll)
            for d in self.meta.values()
        }
        plan[cfg.canary_ip] = cfg.pings_per_poll
        return plan

    def process_cycle(
        self, results: dict[str, PingResult], ts: str, subset: set[int] | None = None,
        expected_ips: set[str] | None = None,
    ) -> CycleResult:
        """Advance the FSMs by one sample and return the committed states + events.

        `subset=None` is the normal full pass (every device, plus canary/uplink edge
        detection and the freeze). Passing a set of device ids runs a **confirmation
        pass**: it advances *only* those FSMs by one more sample (used by the daemon's
        fast-retry path to confirm a suspected DOWN within seconds), leaving every other
        device's committed state untouched and skipping the canary/uplink logic that the
        full pass already handled this cycle.

        `expected_ips`, on a full pass only, narrows which devices this particular
        report is authoritative for (central's multi-edge-per-tenant device assignment —
        see `central/store.py:node_expected_ips`). A device whose ip ISN'T in
        `expected_ips` is skipped entirely — no feed(), no committed entry, no event —
        rather than defaulting to the missing-sample 100%-loss fallback below, so a
        device assigned to a DIFFERENT edge never looks falsely DOWN just because THIS
        report didn't mention it. `None` (every existing caller) means "every device is
        this report's concern", identical to before this parameter existed."""
        cfg = self.cfg
        events: list[Event] = []
        canary_down = False

        if subset is None:
            canary = results.get(cfg.canary_ip)
            canary_down = canary is not None and canary.packet_loss >= 100.0

            # --- Uplink edge detection (independent of the freeze policy) ---
            if canary_down and not self._uplink_active:
                self._uplink_active = True
                events.append(UplinkDown())
            elif not canary_down and self._uplink_active:
                self._uplink_active = False
                events.append(UplinkRestored())

            # --- Canary freeze: when our own internet is down, optionally suppress all
            # local transitions and raise just the one UplinkDown — avoids a storm of
            # per-site pages when every remote site is unreachable *through* the dead
            # uplink. Disabled (WISP_CANARY_FREEZE=0) for LAN-reachable gear that stays
            # monitorable when the internet drops: the UplinkDown above still fires, but
            # we fall through and evaluate local devices normally. ---
            if canary_down and cfg.canary_freeze:
                states = {dev_id: fsm.state for dev_id, fsm in self.fsm.items()}
                # Frozen: no transitions, so no redundancy recompute either (the
                # on-backup badge holds until the uplink is back) — same policy as
                # fast-confirm being skipped under the freeze.
                return CycleResult(states=states, events=events, canary_down=True)
            order = self._order if expected_ips is None else [
                dev_id for dev_id in self._order
                if self.meta[dev_id].ip_address in expected_ips
            ]
        else:
            # Confirmation pass: advance only the suspected devices, in topological
            # order so a parent confirmed down this pass still suppresses its children.
            order = [dev_id for dev_id in self._order if dev_id in subset]

        committed: dict[int, str] = {}
        for dev_id in order:
            dev = self.meta[dev_id]
            res = results.get(dev.ip_address) or PingResult(dev.ip_address, None, 100.0)
            prev, new = self.fsm[dev_id].feed(res, cfg)

            # --- Topology override: a child whose EVERY parent is down -> UNREACHABLE.
            # With redundancy this is "all parents down", not "the parent is down": if
            # ANY parent (primary OR backup) is still alive yet the child won't answer,
            # that's a genuine fault, not a topology artifact, so it stays DOWN and pages.
            # With exactly one parent this is identical to the old single-parent check. ---
            if new == DOWN:
                monitored = [e for e in dev.effective_parents() if e.parent_id in self.fsm]
                if monitored and all(
                    committed.get(e.parent_id, self.fsm[e.parent_id].state) in DOWN_FAMILY
                    for e in monitored
                ):
                    new = UNREACHABLE
                    self.fsm[dev_id].state = UNREACHABLE

            committed[dev_id] = new

            # --- Outage lifecycle from the transition ---
            was_down = prev in DOWN_FAMILY
            is_down = new in DOWN_FAMILY
            if not was_down and is_down:
                events.append(OutageOpened(dev_id, new))
            elif was_down and not is_down:
                events.append(OutageResolved(dev_id))
            elif was_down and is_down and prev != new:
                events.append(OutageRecategorized(dev_id, new))

        # --- Redundancy (on-backup) — full pass only -------------------------------
        # Pure function of this cycle's committed parent states + each device's edge
        # kinds, so it lives in the engine (no second DB pass). A node is "on backup"
        # when its PRIMARY parent is down, at least one BACKUP parent is alive, and the
        # node itself still pings — i.e. the backup path is carrying it. The confirmation
        # (subset) pass never recomputes this.
        redundancy: dict[int, bool] = {}
        if subset is None:
            for dev_id in order:
                dev = self.meta[dev_id]
                backups = [e for e in dev.parents if e.parent_id in self.fsm]
                if not backups:
                    continue  # no redundancy to reason about
                primary = dev.parent_device_id
                primary_down = (
                    primary in self.fsm
                    and committed.get(primary, self.fsm[primary].state) in DOWN_FAMILY
                )
                backup_alive = any(
                    committed.get(e.parent_id, self.fsm[e.parent_id].state) not in DOWN_FAMILY
                    for e in backups
                )
                self_up = committed.get(dev_id, self.fsm[dev_id].state) not in DOWN_FAMILY
                redundancy[dev_id] = bool(primary_down and backup_alive and self_up)

        return CycleResult(
            states=committed, events=events, canary_down=False, redundancy=redundancy)

