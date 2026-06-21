"""Layer 2 — Pattern Recognition.

The brains: turns raw per-poll samples into confirmed states and outages, with
flap suppression, recovery hysteresis, and three network-aware overrides:

  * Uplink canary   — if our own internet is down, freeze everything and raise
                      ONE Uplink_Down instead of a storm of per-hub alerts.
  * Topology        — a child of a DOWN parent becomes UNREACHABLE (suppressed),
                      not separately DOWN.
  * Power vs link   — a real DOWN is tagged 'Likely Power Outage' or
                      'Link/Equipment Fault' so a tech brings the right gear.

`MonitorEngine` is deliberately pure: it takes a dict of {ip: PingResult} plus a
timestamp and returns committed states + a list of events. All DB reads (to build
and rehydrate it) and DB writes (applying events) live in the small functions at
the bottom, so the decision logic can be unit-tested with no database.
"""
from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass

from wisp.config import CONFIG, Config
from wisp.database.client import connect
from wisp.ingress.probers import PingResult

# --- States -----------------------------------------------------------------
UP = "UP"
DEGRADED = "DEGRADED"
DOWN = "DOWN"
UNREACHABLE = "UNREACHABLE"
DOWN_FAMILY = frozenset({DOWN, UNREACHABLE})

POWER_CAUSE = "Likely Power Outage"
LINK_CAUSE = "Link/Equipment Fault"


@dataclass
class DeviceMeta:
    id: int
    name: str
    ip_address: str
    criticality: int
    region: str | None
    parent_device_id: int | None
    power_ref_ip: str | None
    technician_phone: str | None


# --- Events the engine emits (daemon/notifier act on these) -----------------
@dataclass
class OutageOpened:
    device_id: int
    state: str            # DOWN or UNREACHABLE
    inferred_cause: str | None


@dataclass
class OutageRecategorized:
    device_id: int
    state: str
    inferred_cause: str | None


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
        """Parents before children, so a parent's new state is known when we
        evaluate its children for topology suppression."""
        ids = {d.id for d in devices}
        pending = deque(devices)
        placed: set[int] = set()
        order: list[int] = []
        guard = 0
        while pending and guard < len(devices) ** 2 + 1:
            d = pending.popleft()
            parent = d.parent_device_id
            if parent is None or parent not in ids or parent in placed:
                order.append(d.id)
                placed.add(d.id)
            else:
                pending.append(d)
            guard += 1
        order.extend(d.id for d in pending if d.id not in placed)  # cycles: best effort
        return order

    def required_ips(self) -> set[str]:
        ips = {d.ip_address for d in self.meta.values()}
        ips.add(self.cfg.canary_ip)
        ips |= {d.power_ref_ip for d in self.meta.values() if d.power_ref_ip}
        return ips

    def _infer_cause(
        self, dev: DeviceMeta, committed: dict[int, str], results: dict[str, PingResult]
    ) -> str:
        ref = dev.power_ref_ip
        # Direct evidence: the mains-power reference node is unreachable.
        if ref and ref in results and results[ref].packet_loss >= 100.0:
            return POWER_CAUSE
        # Co-location heuristic: a whole multi-device site going dark together looks
        # like power. Needs 2+ devices on the same mains — a lone device dropping is
        # far more likely a link/equipment fault, so don't infer power from it.
        if ref:
            siblings = [m for m in self.meta.values() if m.power_ref_ip == ref]
            if len(siblings) >= 2 and all(
                committed.get(m.id, UP) in DOWN_FAMILY for m in siblings
            ):
                return POWER_CAUSE
        return LINK_CAUSE

    def process_cycle(self, results: dict[str, PingResult], ts: str) -> CycleResult:
        cfg = self.cfg
        canary = results.get(cfg.canary_ip)
        canary_down = canary is not None and canary.packet_loss >= 100.0

        # --- Canary freeze: suppress all local transitions ---
        if canary_down:
            events: list[Event] = []
            if not self._uplink_active:
                self._uplink_active = True
                events.append(UplinkDown())
            states = {dev_id: fsm.state for dev_id, fsm in self.fsm.items()}
            return CycleResult(states=states, events=events, canary_down=True)

        events = []
        if self._uplink_active:
            self._uplink_active = False
            events.append(UplinkRestored())

        committed: dict[int, str] = {}
        for dev_id in self._order:
            dev = self.meta[dev_id]
            res = results.get(dev.ip_address) or PingResult(dev.ip_address, None, 100.0)
            prev, new = self.fsm[dev_id].feed(res, cfg)

            # --- Topology override: child of a down parent -> UNREACHABLE ---
            if new == DOWN and dev.parent_device_id is not None:
                parent_state = committed.get(
                    dev.parent_device_id,
                    self.fsm[dev.parent_device_id].state
                    if dev.parent_device_id in self.fsm else UP,
                )
                if parent_state in DOWN_FAMILY:
                    new = UNREACHABLE
                    self.fsm[dev_id].state = UNREACHABLE

            committed[dev_id] = new

            # --- Outage lifecycle from the transition ---
            was_down = prev in DOWN_FAMILY
            is_down = new in DOWN_FAMILY
            if not was_down and is_down:
                cause = self._infer_cause(dev, committed, results) if new == DOWN else None
                events.append(OutageOpened(dev_id, new, cause))
            elif was_down and not is_down:
                events.append(OutageResolved(dev_id))
            elif was_down and is_down and prev != new:
                cause = self._infer_cause(dev, committed, results) if new == DOWN else None
                events.append(OutageRecategorized(dev_id, new, cause))

        return CycleResult(states=committed, events=events, canary_down=False)


# --- DB glue: build/rehydrate the engine, and apply its events --------------
def load_device_meta(cfg: Config = CONFIG) -> list[DeviceMeta]:
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT id, name, ip_address, criticality, region, parent_device_id,"
            " power_ref_ip, technician_phone"
            " FROM devices WHERE is_active = 1 ORDER BY id"
        ).fetchall()
    return [DeviceMeta(**dict(r)) for r in rows]


def build_engine(cfg: Config = CONFIG) -> MonitorEngine:
    """Construct the engine and rehydrate each FSM from the last recorded state
    so a restart continues instead of re-paging everyone."""
    devices = load_device_meta(cfg)
    engine = MonitorEngine(devices, cfg)
    with connect(cfg) as conn:
        for dev_id in engine.fsm:
            row = conn.execute(
                "SELECT state FROM poll_results WHERE device_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (dev_id,),
            ).fetchone()
            if row:
                engine.fsm[dev_id].prime(row["state"])
        # Rehydrate uplink state: if the last uplink log entry was UPLINK_DOWN,
        # mark the engine as uplink-active so the next healthy cycle emits
        # UplinkRestored and clears the dashboard badge.
        uplink_row = conn.execute(
            "SELECT payload FROM alert_log"
            " WHERE payload LIKE '%UPLINK%' OR payload LIKE '%Uplink%'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if uplink_row and "UPLINK_DOWN" in (uplink_row["payload"] or ""):
            engine._uplink_active = True
    return engine


def apply_events(conn: sqlite3.Connection, events: list[Event], ts: str) -> None:
    """Persist outage open/close/recategorize. Idempotent against the open-outage
    row (resolve/recategorize no-op if none is open)."""
    for ev in events:
        if isinstance(ev, OutageOpened):
            conn.execute(
                "INSERT INTO outages (device_id, started_at, final_state, inferred_cause)"
                " VALUES (?,?,?,?)",
                (ev.device_id, ts, ev.state, ev.inferred_cause),
            )
        elif isinstance(ev, OutageRecategorized):
            conn.execute(
                "UPDATE outages SET final_state = ?, inferred_cause = COALESCE(?, inferred_cause)"
                " WHERE device_id = ? AND resolved_at IS NULL",
                (ev.state, ev.inferred_cause, ev.device_id),
            )
        elif isinstance(ev, OutageResolved):
            conn.execute(
                "UPDATE outages SET resolved_at = ? WHERE device_id = ? AND resolved_at IS NULL",
                (ts, ev.device_id),
            )
