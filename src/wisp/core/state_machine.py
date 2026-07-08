from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import NamedTuple

from wisp.config import CONFIG, Config
from wisp.ingress.probers import PingResult

UP = "UP"
DEGRADED = "DEGRADED"
DOWN = "DOWN"
UNREACHABLE = "UNREACHABLE"
DOWN_FAMILY = frozenset({DOWN, UNREACHABLE})

PRIMARY = "primary"
BACKUP = "backup"

class ParentEdge(NamedTuple):
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
    parents: tuple[ParentEdge, ...] = ()

    def effective_parents(self) -> tuple[ParentEdge, ...]:
        edges: list[ParentEdge] = []
        if self.parent_device_id is not None:
            edges.append(ParentEdge(self.parent_device_id, PRIMARY))
        edges.extend(self.parents)
        return tuple(edges)

@dataclass
class OutageOpened:
    device_id: int
    state: str

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
    redundancy: dict[int, bool] = field(default_factory=dict)

class DeviceFSM:

    def __init__(self, state: str = UP) -> None:
        self.state = state
        self.down_streak = 0
        self.nondown_streak = 0
        self.degraded_streak = 0
        self.healthy_streak = 0
        self.prime(state)

    def prime(self, state: str) -> None:
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
            if self.healthy_streak >= cfg.recover_consecutive:
                new = UP
            elif self.nondown_streak >= cfg.recover_consecutive:
                new = DEGRADED
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

class MonitorEngine:
    def __init__(self, devices: list[DeviceMeta], cfg: Config = CONFIG) -> None:
        self.cfg = cfg
        self.meta: dict[int, DeviceMeta] = {d.id: d for d in devices}
        self.fsm: dict[int, DeviceFSM] = {d.id: DeviceFSM() for d in devices}
        self._order = self._topological_order(devices)
        self._uplink_active = False

    @staticmethod
    def _topological_order(devices: list[DeviceMeta]) -> list[int]:
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
        queue = deque(d.id for d in devices if indeg[d.id] == 0)
        order: list[int] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for child in children_of.get(nid, []):
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)
        if len(order) < len(devices):
            placed = set(order)
            order.extend(sorted(d.id for d in devices if d.id not in placed))
        return order

    def required_ips(self) -> set[str]:
        ips = {d.ip_address for d in self.meta.values()}
        ips.add(self.cfg.canary_ip)
        return ips

    def probe_plan(self) -> dict[str, int]:
        cfg = self.cfg
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
        cfg = self.cfg
        events: list[Event] = []
        canary_down = False

        if subset is None:
            canary = results.get(cfg.canary_ip)
            canary_down = canary is not None and canary.packet_loss >= 100.0

            if canary_down and not self._uplink_active:
                self._uplink_active = True
                events.append(UplinkDown())
            elif not canary_down and self._uplink_active:
                self._uplink_active = False
                events.append(UplinkRestored())

            if canary_down and cfg.canary_freeze:
                states = {dev_id: fsm.state for dev_id, fsm in self.fsm.items()}
                return CycleResult(states=states, events=events, canary_down=True)
            order = self._order if expected_ips is None else [
                dev_id for dev_id in self._order
                if self.meta[dev_id].ip_address in expected_ips
            ]
        else:
            order = [dev_id for dev_id in self._order if dev_id in subset]

        committed: dict[int, str] = {}
        for dev_id in order:
            dev = self.meta[dev_id]
            res = results.get(dev.ip_address) or PingResult(dev.ip_address, None, 100.0)
            prev, new = self.fsm[dev_id].feed(res, cfg)

            if new == DOWN:
                monitored = [e for e in dev.effective_parents() if e.parent_id in self.fsm]
                if monitored and all(
                    committed.get(e.parent_id, self.fsm[e.parent_id].state) in DOWN_FAMILY
                    for e in monitored
                ):
                    new = UNREACHABLE
                    self.fsm[dev_id].state = UNREACHABLE

            committed[dev_id] = new

            was_down = prev in DOWN_FAMILY
            is_down = new in DOWN_FAMILY
            if not was_down and is_down:
                events.append(OutageOpened(dev_id, new))
            elif was_down and not is_down:
                events.append(OutageResolved(dev_id))
            elif was_down and is_down and prev != new:
                events.append(OutageRecategorized(dev_id, new))

        redundancy: dict[int, bool] = {}
        if subset is None:
            for dev_id in order:
                dev = self.meta[dev_id]
                backups = [e for e in dev.parents if e.parent_id in self.fsm]
                if not backups:
                    continue
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
