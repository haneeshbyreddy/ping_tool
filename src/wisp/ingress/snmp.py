from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.snmp")

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_ADMIN = "1.3.6.1.2.1.2.2.1.7"
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"
OID_IF_LASTCHANGE = "1.3.6.1.2.1.2.2.1.9"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_HCIN = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HCOUT = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_HIGHSPEED = "1.3.6.1.2.1.31.1.1.1.15"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"

WALK_COLUMNS = (
    OID_IF_DESCR, OID_IF_ADMIN, OID_IF_OPER, OID_IF_LASTCHANGE, OID_IF_SPEED,
    OID_IF_NAME, OID_IF_HCIN, OID_IF_HCOUT, OID_IF_HIGHSPEED, OID_IF_ALIAS,
)

_IF_STATUS = {
    1: "up", 2: "down", 3: "testing", 4: "unknown",
    5: "dormant", 6: "notPresent", 7: "lowerLayerDown",
}
_DOWN_OPER = frozenset({"down", "lowerLayerDown"})

@dataclass(frozen=True)
class SnmpTarget:
    ip: str
    community: str
    port: int = 161
    version: str = "2c"

@dataclass(frozen=True)
class PortStatus:
    if_index: int
    if_name: str | None
    if_alias: str | None
    admin_status: str
    oper_status: str
    last_change: str | None = None
    in_octets: int | None = None
    out_octets: int | None = None
    speed_bps: int | None = None

    def is_down(self) -> bool:
        return self.admin_status == "up" and self.oper_status in _DOWN_OPER

@runtime_checkable
class SnmpPoller(Protocol):
    async def walk(self, target: SnmpTarget) -> list[PortStatus]: ...

def _status_label(raw: str) -> str:
    s = str(raw).strip()
    try:
        return _IF_STATUS.get(int(s), "unknown")
    except (TypeError, ValueError):
        return s.lower() if s else "unknown"

def _int_or_none(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None

def throughput_bps(prev_octets: int | None, cur_octets: int | None,
                   dt_seconds: float) -> float | None:
    if prev_octets is None or cur_octets is None or dt_seconds <= 0:
        return None
    delta = cur_octets - prev_octets
    if delta < 0:
        return None
    return (delta * 8.0) / dt_seconds

def _index_after(oid: str, prefix: str) -> int | None:
    if not oid.startswith(prefix + "."):
        return None
    tail = oid[len(prefix) + 1:]
    head = tail.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None

def parse_if_table(varbinds: list[tuple[str, str]]) -> list[PortStatus]:
    cols: dict[int, dict[str, str]] = {}
    routing = {
        OID_IF_DESCR: "descr", OID_IF_ADMIN: "admin", OID_IF_OPER: "oper",
        OID_IF_LASTCHANGE: "lastchange", OID_IF_NAME: "name", OID_IF_ALIAS: "alias",
        OID_IF_SPEED: "speed", OID_IF_HCIN: "hcin", OID_IF_HCOUT: "hcout",
        OID_IF_HIGHSPEED: "highspeed",
    }
    for oid, value in varbinds:
        for prefix, key in routing.items():
            idx = _index_after(oid, prefix)
            if idx is not None:
                cols.setdefault(idx, {})[key] = "" if value is None else str(value)
                break

    ports: list[PortStatus] = []
    for idx in sorted(cols):
        c = cols[idx]
        if "oper" not in c and "admin" not in c:
            continue
        name = (c.get("name") or "").strip() or (c.get("descr") or "").strip() or None
        alias = (c.get("alias") or "").strip() or None
        last = (c.get("lastchange") or "").strip() or None
        hi = _int_or_none(c.get("highspeed"))
        lo = _int_or_none(c.get("speed"))
        speed_bps = (hi * 1_000_000) if hi else (lo if lo else None)
        ports.append(PortStatus(
            if_index=idx,
            if_name=name,
            if_alias=alias,
            admin_status=_status_label(c.get("admin", "")),
            oper_status=_status_label(c.get("oper", "")),
            last_change=last,
            in_octets=_int_or_none(c.get("hcin")),
            out_octets=_int_or_none(c.get("hcout")),
            speed_bps=speed_bps,
        ))
    return ports

# GETBULK exception values (not data) that end a column mid-response.
_END_OF_TABLE = frozenset({"EndOfMibView", "NoSuchObject", "NoSuchInstance"})
# Each repetition carries one row of every active column, so 8 keeps a PDU near
# ~80 varbinds instead of a jumbo response a cheap agent might truncate.
_MAX_REPETITIONS = 8
# 400 rounds x 8 repetitions = 3200 rows per column; the biggest fleet OLT has
# ~333 interfaces. Past this the agent is looping, not answering.
_MAX_ROUNDS = 400

# The combined fast path gets only a SLICE of the port budget (min of this and a
# third of the cap), so a big/weak OLT that can't serve it (EDGE_HALIYA "timeout"
# mode) fails over to the per-column net with budget to spare — instead of the
# combined walk eating the whole cap while the fallback never runs (the daemon's
# outer port_walk_timeout_s wait_for wraps BOTH paths).
_FAST_PATH_MAX_S = 15.0
# Stop the walk this far before the cap so parse+return finish before the daemon's
# outer wait_for would cancel us: a slow box then yields a partial-but-usable table
# instead of a bare "timeout".
_WALK_MARGIN_S = 0.25
# A per-column walk that gets this many consecutive no-answers with nothing gathered
# yet is talking to a dead agent — stop and let walk() re-raise so the daemon still
# classifies it no_response. A merely-flaky agent (answers ANY column) keeps going and
# skips only the columns it drops — that tolerance is the whole point.
_DEAD_AGENT_GIVEUP = 3
# How often a device pinned to a gentler strategy re-probes one rung faster, so a
# firmware fix or a hardware swap recovers the efficient combined walk on its own —
# no vendor hardcode, the poller relearns.
_PROMOTE_INTERVAL_S = 6 * 3600.0
# Per-column repetition counts down the ladder: the normal net, then a small-packet
# net for agents that drop even a single-column GETBULK of 25 rows.
_PERCOLUMN_REPETITIONS = 25
_PERCOLUMN_SMALL_REPETITIONS = 4
_MAX_LEVEL = 2  # 0 = combined, 1 = per-column/25, 2 = per-column/4

# Per-column walk order: interface STATUS first (admin/oper decide up/down, name/descr
# label it), then speed, then byte counters — so a budget-bounded partial walk still
# yields port up/down for as many interfaces as it reached. A permutation of
# WALK_COLUMNS; parse_if_table is order-independent.
_PERCOLUMN_ORDER = (
    OID_IF_ADMIN, OID_IF_OPER, OID_IF_NAME, OID_IF_DESCR, OID_IF_LASTCHANGE,
    OID_IF_HIGHSPEED, OID_IF_SPEED, OID_IF_ALIAS, OID_IF_HCIN, OID_IF_HCOUT,
)

def _oid_tuple(oid: str) -> tuple[int, ...]:
    return tuple(int(part) for part in oid.split("."))

class MultiColumnWalk:
    """Cursor state for walking N table columns in one multi-varbind GETBULK.

    The ten ifTable/ifXTable columns share their ifIndex rows, so a combined
    walk returns every column per round and cuts round-trips ~10x vs ten serial
    walks. pysnmp's bulk_walk_cmd accepts exactly ONE varbind — passing the ten
    columns positionally was the v0.15.3 fleet-wide stale-ports outage — so the
    combined walk drives raw single-PDU bulk_cmd calls by hand: request() names
    the next OID for each still-active column, feed() consumes the
    repetition-major response (varbind i belongs to requested column i % N;
    GETBULK truncates only at the tail, so the mapping survives short
    responses). A column retires when it leaves its subtree, hits an exception
    value, or returns a non-increasing OID (a buggy agent must stall out, not
    spin forever). Pure — no I/O — so tests drive it with canned rows.
    """

    def __init__(self, columns: Sequence[str]) -> None:
        self._root = {c: _oid_tuple(c) for c in columns}
        self._cursor = {c: _oid_tuple(c) for c in columns}
        self._active = list(columns)
        self.varbinds: list[tuple[str, str]] = []

    @property
    def done(self) -> bool:
        return not self._active

    def request(self) -> list[str]:
        return [".".join(str(x) for x in self._cursor[c]) for c in self._active]

    def feed(self, rows: Sequence[tuple[str, str, str]]) -> None:
        """rows = [(oid, value, value_class_name), ...] in response order."""
        cols = list(self._active)
        if not cols:
            return
        if not rows:  # empty response with no error: the agent has nothing more
            self._active = []
            return
        finished: set[str] = set()
        for i, (oid, value, value_class) in enumerate(rows):
            col = cols[i % len(cols)]
            if col in finished:
                continue
            if value_class in _END_OF_TABLE:
                finished.add(col)
                continue
            t = _oid_tuple(oid)
            root = self._root[col]
            if t[: len(root)] != root or t <= self._cursor[col]:
                finished.add(col)
                continue
            self.varbinds.append((oid, value))
            self._cursor[col] = t
        self._active = [c for c in cols if c not in finished]

class PysnmpPoller:
    """Adaptive ifTable poller. Tries the efficient combined GETBULK first, then a
    TOLERANT per-column net (health-style: skip a dropped column, never zero the
    table), and remembers per device the gentlest strategy that last worked — so a
    weak C-Data/DBC agent is never re-hammered with a walk it can't serve. It is
    vendor-agnostic: weakness is discovered empirically and faster paths are re-probed
    on a slow clock, so a firmware fix or a hardware swap self-heals. One SnmpEngine
    per instance (reused across walks — the leak invariant); the daemon reuses one
    poller, so the learned per-device levels persist across sweeps.

    Strategy ladder, walked from the device's remembered level down until one yields
    rows (see _combined_walk / _percolumn_walk):
      0  combined 10-column GETBULK, hard-capped at _FAST_PATH_MAX_S
      1  per-column bulk_walk, _PERCOLUMN_REPETITIONS rows/packet
      2  per-column bulk_walk, _PERCOLUMN_SMALL_REPETITIONS rows/packet (weakest agents)
    """

    def __init__(self, cfg: Config = CONFIG) -> None:
        self._timeout = cfg.snmp_request_timeout_s or cfg.snmp_timeout_s
        self._retries = max(1, cfg.snmp_request_retries)
        self._budget_s = cfg.port_walk_timeout_s
        self._engine = None
        # device key -> gentlest strategy level that last SUCCEEDED, and when to next
        # re-probe one rung faster. Persist across sweeps (one long-lived poller).
        self._device_level: dict[str, int] = {}
        self._promote_at: dict[str, float] = {}

    def _start_level(self, key: str, now: float) -> int:
        level = self._device_level.get(key, 0)
        if level > 0 and now >= self._promote_at.get(key, 0.0):
            self._promote_at[key] = now + _PROMOTE_INTERVAL_S
            level -= 1  # periodic re-probe of a faster strategy
        return level

    def _remember(self, key: str, level: int, now: float) -> None:
        self._device_level[key] = level
        if level > 0:
            self._promote_at.setdefault(key, now + _PROMOTE_INTERVAL_S)
        else:
            self._promote_at.pop(key, None)  # already fastest; nothing to re-probe

    async def walk(self, target: SnmpTarget) -> list[PortStatus]:
        try:
            from pysnmp.hlapi.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget,
            )
        except ImportError as exc:
            raise RuntimeError(
                "SnmpPoller needs 'pysnmp' (pip install pysnmp)."
            ) from exc

        if self._engine is None:
            self._engine = SnmpEngine()
        community = CommunityData(target.community, mpModel=1)
        transport = await UdpTransportTarget.create(
            (target.ip, target.port), timeout=self._timeout, retries=self._retries)

        loop = asyncio.get_running_loop()
        now = loop.time()
        budget = self._budget_s
        if budget and budget > 0:
            # Reserve a margin under the daemon's outer cap so we return before it
            # cancels us; box the fast path to a slice so the net always gets airtime.
            hard_deadline: float | None = now + budget - _WALK_MARGIN_S
            fast_budget = min(_FAST_PATH_MAX_S, budget / 3.0)
        else:
            hard_deadline = None                # no cap: per-column runs to completion
            fast_budget = _FAST_PATH_MAX_S      # but still never let combined hang forever

        key = f"{target.ip}:{target.port}"
        answered = False           # any strategy got the agent to respond at all
        last_exc: Exception | None = None

        for level in range(self._start_level(key, now), _MAX_LEVEL + 1):
            if level == 0:
                ports = await self._combined_walk(
                    target, community, transport, fast_budget)
                if ports is None:
                    continue                    # combined failed -> drop to per-column
                answered = True
                if ports:
                    self._remember(key, 0, now)
                    return ports
                continue                        # answered but empty; let per-column confirm
            max_rep = (_PERCOLUMN_REPETITIONS if level == 1
                       else _PERCOLUMN_SMALL_REPETITIONS)
            varbinds, responded, exc = await self._percolumn_walk(
                target, community, transport, max_rep, hard_deadline)
            answered = answered or responded
            if exc is not None:
                last_exc = exc
            ports = parse_if_table(varbinds)
            if ports:
                self._remember(key, level, now)
                return ports

        if answered:
            return []                           # agent answered, no usable ifTable rows
        # Nothing anywhere answered — re-raise so the daemon classifies it
        # (no_response/error) rather than a silent "empty".
        raise RuntimeError(
            f"SNMP walk of {target.ip} failed: {last_exc}") from last_exc

    async def _combined_walk(self, target, community, transport, fast_budget):
        """Fast path: all ten columns in one multi-varbind GETBULK stream (see
        MultiColumnWalk), hard-capped at fast_budget. Returns parsed rows, or None if
        it failed/timed out and the caller should drop to the per-column net — any
        failure is non-fatal (tooBig from a cheap agent, a stall-guard trip, API
        drift, or the time box). pysnmp 7: multi-varbind ONLY via bulk_cmd."""
        from pysnmp.hlapi.asyncio import (
            ContextData, ObjectType, ObjectIdentity, bulk_cmd,
        )
        engine = self._engine

        async def inner() -> list[PortStatus]:
            walker = MultiColumnWalk(WALK_COLUMNS)
            rounds = 0
            while not walker.done:
                rounds += 1
                if rounds > _MAX_ROUNDS:
                    raise RuntimeError("combined ifTable walk did not terminate")
                errInd, errStat, errIdx, binds = await bulk_cmd(
                    engine, community, transport, ContextData(),
                    0, _MAX_REPETITIONS,
                    *[ObjectType(ObjectIdentity(o)) for o in walker.request()])
                if errInd or errStat:
                    raise RuntimeError(str(errInd or errStat))
                walker.feed([(str(name), val.prettyPrint(), type(val).__name__)
                             for name, val in binds])
            return parse_if_table(walker.varbinds)

        try:
            if fast_budget and fast_budget > 0:
                return await asyncio.wait_for(inner(), fast_budget)
            return await inner()
        except Exception as exc:
            log.warning("combined ifTable walk of %s failed (%s); "
                        "falling back to per-column walks", target.ip, exc)
            return None

    async def _percolumn_walk(self, target, community, transport, max_rep,
                              hard_deadline):
        """Per-column safety net, TOLERANT like the health walk: a dropped or erroring
        column is skipped, never fatal. The brittle 'one dropped column zeroes the
        whole ifTable' behavior is exactly why EDGE_HALIYA's small OLTs no_responsed
        while their health walk — same box, same sweep — succeeded. Status columns
        walk FIRST (see _PERCOLUMN_ORDER), so a budget-bounded partial still yields
        port up/down. Returns (varbinds, responded, last_exc); `responded` separates
        'answered but empty' from 'never answered' so walk() keeps the no_response
        signal for a genuinely dead agent."""
        from pysnmp.hlapi.asyncio import (
            ContextData, ObjectType, ObjectIdentity, bulk_walk_cmd,
        )
        engine = self._engine
        loop = asyncio.get_running_loop()

        async def one_column(col: str) -> list[tuple[str, str]]:
            out: list[tuple[str, str]] = []
            async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
                engine, community, transport, ContextData(),
                0, max_rep, ObjectType(ObjectIdentity(col)),
                lexicographicMode=False,
            ):
                if errInd or errStat:
                    raise RuntimeError(str(errInd or errStat))
                for name, val in binds:
                    out.append((str(name), val.prettyPrint()))
            return out

        varbinds: list[tuple[str, str]] = []
        responded = False
        consecutive_fail = 0
        last_exc: Exception | None = None
        for column in _PERCOLUMN_ORDER:
            remaining = None
            if hard_deadline is not None:
                remaining = hard_deadline - loop.time()
                if remaining <= 0:
                    break                       # out of budget; keep what we gathered
            try:
                col_binds = (await asyncio.wait_for(one_column(column), remaining)
                             if remaining is not None else await one_column(column))
            except Exception as exc:
                last_exc = exc
                consecutive_fail += 1
                log.debug("ifTable column %s of %s skipped: %s",
                          column, target.ip, exc)
                if not responded and consecutive_fail >= _DEAD_AGENT_GIVEUP:
                    break                       # nothing answering — stop, let walk() re-raise
                continue
            responded = True
            consecutive_fail = 0
            varbinds.extend(col_binds)
        return varbinds, responded, last_exc

def build_snmp_poller(cfg: Config = CONFIG) -> SnmpPoller:
    return PysnmpPoller(cfg)
