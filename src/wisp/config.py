"""Central configuration for the Village WISP Monitor.

All tunables live here as a single frozen dataclass loaded from the environment
with sensible defaults, so the system runs out-of-the-box with zero setup and is
reconfigured purely through env vars (no code edits) when hardware/credentials arrive.
Change a value by exporting the env var and restarting the daemon/dashboard.

Nothing in here imports the rest of the project, so it is safe to import anywhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# This file lives at <repo>/src/wisp/config.py, so the repo root is three up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _hostname() -> str:
    """Stable default node id when WISP_NODE_ID is unset (the box's hostname).
    Falls back to a literal so identity is never empty even on a nameless host."""
    import socket
    try:
        return socket.gethostname() or "edge"
    except Exception:
        return "edge"


@dataclass(frozen=True)
class Config:
    # --- Storage -------------------------------------------------------------
    db_path: Path = field(
        default_factory=lambda: Path(_env("WISP_DB", str(DATA_DIR / "wisp.db")))
    )
    migrations_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "migrations")
    # SQLite waits this long for a competing writer before raising "database is locked".
    busy_timeout_ms: int = field(default_factory=lambda: _env_int("WISP_BUSY_TIMEOUT_MS", 5000))

    # --- Polling -------------------------------------------------------------
    poll_interval_s: int = field(default_factory=lambda: _env_int("WISP_POLL_INTERVAL_S", 60))
    # Detection latency is `poll_interval_s × down_consecutive` (3 polls to confirm
    # DOWN). A small deployment can afford a faster cadence — and quicker detection —
    # that a 10k-device box can't. Opt in with WISP_POLL_INTERVAL_ADAPTIVE=1: while the
    # active fleet is at or below `small_fleet_max`, the daemon polls every
    # `poll_interval_small_s` (default 30s → ~90s to declare DOWN); above it, it falls
    # back to `poll_interval_s` to protect the box. Re-evaluated on device-set reload,
    # so crossing the threshold retunes the cadence in-process (no restart).
    poll_interval_adaptive: bool = field(
        default_factory=lambda: _env_bool("WISP_POLL_INTERVAL_ADAPTIVE", False)
    )
    poll_interval_small_s: int = field(
        default_factory=lambda: _env_int("WISP_POLL_INTERVAL_SMALL_S", 30)
    )
    small_fleet_max: int = field(default_factory=lambda: _env_int("WISP_SMALL_FLEET_MAX", 1000))
    # Fast confirmation (soft-state → hard-state). When a poll reads 100% loss, the
    # daemon re-probes *just that device* back-to-back every `retry_interval_s` until it
    # has `down_consecutive` all-lost samples — so DOWN is confirmed in seconds instead
    # of `down_consecutive` full poll intervals, without touching the healthy fleet's
    # cadence or weakening flap suppression (still N consecutive all-lost samples). A
    # reachable retry clears the suspicion, so a blip never pages. 0 disables it (one
    # sample per poll, detection = down_consecutive × poll_interval as before).
    retry_interval_s: float = field(
        default_factory=lambda: _env_float("WISP_RETRY_INTERVAL_S", 2.0)
    )
    pings_per_poll: int = field(default_factory=lambda: _env_int("WISP_PINGS_PER_POLL", 5))
    # Aggregation gear (towers/switches/APs — any device that is a *parent* of
    # another) is probed *gently*: fewer echoes per poll so we don't trip the ICMP
    # rate-limiter on its control plane and read phantom loss on the very box that
    # backhauls hundreds of customers. Leaf CPEs + the canary get `pings_per_poll`.
    pings_per_poll_infra: int = field(
        default_factory=lambda: _env_int("WISP_PINGS_PER_POLL_INFRA", 2)
    )
    # Cap on concurrent in-flight probes. A naive fan-out opens one ICMP socket per
    # device at once; past the process FD limit (`ulimit -n`) the kernel refuses new
    # sockets and every excess probe reads as 100% loss — a self-inflicted mass
    # outage exactly when the fleet is largest. Bounding the in-flight set lets 10k
    # devices clear within the poll window on a few hundred FDs. 0 = unbounded.
    probe_max_inflight: int = field(
        default_factory=lambda: _env_int("WISP_MAX_INFLIGHT", 256)
    )
    # Raw poll samples older than this are pruned by the daemon (once/day) so a
    # 24/7 deployment reaches a steady-state DB size instead of growing forever.
    # Raw polls are *scratch*: the hourly `poll_rollups` are the long-term trend
    # record (latency min/avg/max, mean loss, per-state counts) and `outages` is
    # the permanent incident log — neither is pruned. Nothing reads raw rows older
    # than the current un-folded hour (charts read rollups, history reads outages,
    # FSM restart reads only the newest row), so a short window is plenty; it just
    # has to clear the hourly rollup cadence so a prune never races a fold. 7 days
    # leaves huge margin plus an ad-hoc forensic window. At ~1k devices the old
    # 90-day default held ~130M rows (10s of GB) that answered no query. Raise it
    # only if you add a sub-hour drill-down view. 0 = keep everything (pruning off).
    poll_retention_days: int = field(
        default_factory=lambda: _env_int("WISP_POLL_RETENTION_DAYS", 7)
    )

    # --- Per-link performance baseline (soft "slow link" signal) -------------
    # The FSM only knows UP/DEGRADED/DOWN against ABSOLUTE thresholds; this tier
    # flags a link that is slow/jittery vs ITS OWN rolling baseline (median + MAD)
    # even while it still pings "up" — the classic degrading-wireless-backhaul case.
    # It's a soft heads-up (operator-only page once, plus a dashboard badge), kept
    # entirely separate from the outage/escalation ladder. See core/baseline.py.
    perf_window: int = field(default_factory=lambda: _env_int("WISP_PERF_WINDOW", 20))
    # Min healthy samples needed to form a baseline before we'll judge a deviation.
    perf_min_samples: int = field(default_factory=lambda: _env_int("WISP_PERF_MIN_SAMPLES", 10))
    # Consecutive deviating samples to enter (and clean samples to leave) — hysteresis.
    perf_consecutive: int = field(default_factory=lambda: _env_int("WISP_PERF_CONSECUTIVE", 3))
    # A sample trips only if it's > factor× the baseline AND > baseline + k×MAD.
    perf_deviation_factor: float = field(
        default_factory=lambda: _env_float("WISP_PERF_DEVIATION_FACTOR", 3.0))
    perf_mad_k: float = field(default_factory=lambda: _env_float("WISP_PERF_MAD_K", 5.0))
    # Baselines below these floors are too small to judge a multiple of (avoids
    # flagging 2ms→8ms as a "3× degradation").
    perf_min_baseline_ms: float = field(
        default_factory=lambda: _env_float("WISP_PERF_MIN_BASELINE_MS", 5.0))
    perf_min_jitter_ms: float = field(
        default_factory=lambda: _env_float("WISP_PERF_MIN_JITTER_MS", 3.0))
    # Gate the operator push (the dashboard badge is always written). 0 = badge only.
    perf_alerts: bool = field(default_factory=lambda: _env_bool("WISP_PERF_ALERTS", True))

    # --- Redundancy / on-backup signal (graph topology, Phase 9 Part A) -------
    # When a node's PRIMARY uplink dies but a BACKUP path carries it, the node still
    # pings "up" — no outage — but redundancy is gone ("one more failure is an outage").
    # Like the perf tier this is a soft heads-up: the dashboard badge is always written;
    # this gates the single operator page on the enter/leave edge. 0 = badge only.
    backup_alerts: bool = field(default_factory=lambda: _env_bool("WISP_BACKUP_ALERTS", True))

    # --- SNMP port status (graph topology Part B; IF-MIB oper/admin only) ----
    # A second ingress: walk each snmp_enabled switch's ifTable for port status AND live
    # bandwidth (byte-counter deltas). One bulk-walk per switch is cheap, so it runs on
    # its own cadence. 30s gives near-real-time throughput on the dashboard (and ~3×30s to
    # a bandwidth alarm); raise it to ease load on switches with many monitored ports, or
    # lower it for fresher rates. 0 disables the SNMP task entirely.
    snmp_interval_s: int = field(default_factory=lambda: _env_int("WISP_SNMP_INTERVAL_S", 30))
    # Flap suppression for ports: a monitored port must read down this many consecutive
    # walks before it alarms (mirrors the ICMP down_consecutive idea, gentler cadence).
    snmp_down_consecutive: int = field(
        default_factory=lambda: _env_int("WISP_SNMP_DOWN_CONSECUTIVE", 2))
    # Gate the port page (the switch_ports badge/state is always written). 0 = badge only.
    snmp_alerts: bool = field(default_factory=lambda: _env_bool("WISP_SNMP_ALERTS", True))
    # --- Per-port bandwidth (throughput) low-threshold alarm ------------------
    # Orthogonal to oper/admin status: each walk reads the 64-bit byte counters and the
    # daemon diffs them into a rate (bits/sec). A monitored port whose rate falls below its
    # operator-assigned threshold for this many consecutive walks alarms — flap suppression
    # like the port-down path, but its own count because traffic is burstier than link
    # state (a port that's up but momentarily idle shouldn't page on a single quiet walk).
    snmp_bw_consecutive: int = field(
        default_factory=lambda: _env_int("WISP_SNMP_BW_CONSECUTIVE", 3))
    # Gate the low-bandwidth page (the switch_ports bw state is always written). 0 = badge only.
    snmp_bw_alerts: bool = field(default_factory=lambda: _env_bool("WISP_SNMP_BW_ALERTS", True))
    # SNMP request timeout / retries per walk (seconds). Kept short — it runs in the
    # daemon loop inside its own try/except, so a dead switch never sinks the ICMP cycle.
    snmp_timeout_s: float = field(default_factory=lambda: _env_float("WISP_SNMP_TIMEOUT_S", 2.0))

    # --- Monitor watchdog (dead-monitor alarm) -------------------------------
    # If the newest poll is older than this, the monitor itself is considered
    # down and the watchdog pages the owner. 0 = auto (max(180s, 3 poll cycles)),
    # so a slow box that misses one cycle doesn't false-alarm.
    monitor_stale_after_s: int = field(
        default_factory=lambda: _env_int("WISP_MONITOR_STALE_S", 0)
    )

    # --- State-machine thresholds (see plan.md §"State machine") -------------
    latency_threshold_ms: float = field(
        default_factory=lambda: _env_float("WISP_LATENCY_MS", 150.0)
    )
    loss_degraded_pct: float = field(default_factory=lambda: _env_float("WISP_LOSS_DEGRADED", 5.0))
    down_consecutive: int = field(default_factory=lambda: _env_int("WISP_DOWN_CONSECUTIVE", 3))
    degraded_consecutive: int = field(
        default_factory=lambda: _env_int("WISP_DEGRADED_CONSECUTIVE", 2)
    )
    recover_consecutive: int = field(
        default_factory=lambda: _env_int("WISP_RECOVER_CONSECUTIVE", 2)
    )

    # --- Canary / uplink check ----------------------------------------------
    canary_ip: str = field(default_factory=lambda: _env("WISP_CANARY_IP", "1.1.1.1"))
    # When the canary (our own internet) is down, freeze local monitoring and raise a
    # single UplinkDown instead of a storm of per-site pages — right when every remote
    # site is unreachable *through* the dead uplink. Set WISP_CANARY_FREEZE=0 for gear
    # reachable on the LAN regardless of the internet: the UplinkDown/Restored notices
    # still fire, but local devices keep being evaluated and paged.
    canary_freeze: bool = field(
        default_factory=lambda: _env_bool("WISP_CANARY_FREEZE", True)
    )

    # --- Escalation timing (minutes) ----------------------------------------
    # A fresh DOWN pages the operator immediately; thereafter, while the outage
    # is still open, an all-hands page (owner + operator + tech) fires every
    # `escalate_every_min` minutes with the running duration (and who acked it,
    # if anyone). Acknowledgement does NOT stop this clock — only recovery does.
    escalate_every_min: int = field(
        default_factory=lambda: _env_int("WISP_ESCALATE_EVERY_MIN", 60)
    )
    # A recipient won't get the same device's initial alert more than once per this
    # window (the recurring all-hands escalation bypasses this).
    alert_dedupe_min: int = field(default_factory=lambda: _env_int("WISP_ALERT_DEDUPE_MIN", 10))

    # --- Providers (real adapters: ICMP ping + ntfy push) --------------------
    prober: str = field(default_factory=lambda: _env("WISP_PROBER", "icmp").lower())
    notifier: str = field(default_factory=lambda: _env("WISP_NOTIFIER", "ntfy").lower())

    # --- Channel credentials (only needed once real notifiers are selected) --
    ntfy_base_url: str = field(default_factory=lambda: _env("WISP_NTFY_URL", "https://ntfy.sh"))
    # A page must not be silently lost to a transient blip: retry a failed send
    # this many times with exponential backoff (only network/5xx errors retry;
    # a 4xx is a config error and fails fast). Kept short — it runs outside any DB txn.
    ntfy_retries: int = field(default_factory=lambda: _env_int("WISP_NTFY_RETRIES", 3))
    ntfy_retry_backoff_s: float = field(
        default_factory=lambda: _env_float("WISP_NTFY_RETRY_BACKOFF_S", 0.5)
    )

    # --- Role channels -------------------------------------------------------
    # Three fixed ntfy topics, one per role. Each person subscribes to the topic
    # that matches their role (owner / operator / tech) — there is no per-person
    # routing key. On public ntfy.sh pick unguessable names (anyone who knows a
    # topic can read it); override these env vars or self-host with auth.
    ntfy_topic_owner: str = field(default_factory=lambda: _env("WISP_NTFY_TOPIC_OWNER", "hansa-owner"))
    ntfy_topic_operator: str = field(default_factory=lambda: _env("WISP_NTFY_TOPIC_OPERATOR", "hansa-operator"))
    ntfy_topic_tech: str = field(default_factory=lambda: _env("WISP_NTFY_TOPIC_TECH", "hansa-tech"))

    # --- Organization / locale (display) -------------------------------------
    org_name: str = field(default_factory=lambda: _env("WISP_ORG_NAME", "HANSA Communications"))
    timezone: str = field(default_factory=lambda: _env("WISP_TIMEZONE", "Asia/Kolkata"))

    # --- Central reporting (Phase 10 Part A — edge shipper + outbox) ----------
    # THE back-compat anchor: WISP_CENTRAL_URL empty ⇒ this whole distributed layer
    # is dormant and the edge is byte-for-byte today's standalone monitor (nothing is
    # written to the outbox, no thread starts). Set it to the central ingest base URL
    # (e.g. https://central.example.net) to turn the edge into a reporting node. The
    # edge ALWAYS keeps detecting + paging locally; central only aggregates.
    central_url: str = field(default_factory=lambda: _env("WISP_CENTRAL_URL", "").rstrip("/"))
    # Stopgap auth until Part C's mTLS enrollment exists: a per-node shared secret sent
    # as `Authorization: Bearer <token>`. The wire envelope is designed so mTLS slots in
    # later without changing the record format. Keep it out of logs.
    central_token: str = field(default_factory=lambda: _env("WISP_CENTRAL_TOKEN", ""))
    # Edge identity. (tenant_id, node_id) is the durable identity central keys every
    # record by; edge-local devices.id are per-SQLite and meaningless across nodes, so
    # they ride along only as a per-node correlation id (central maps them in Part B).
    # node_id defaults to the hostname so a fresh install still has a stable id.
    tenant_id: str = field(default_factory=lambda: _env("WISP_TENANT_ID", "default"))
    node_id: str = field(default_factory=lambda: _env("WISP_NODE_ID", "") or _hostname())
    # Shipper cadence + batch size. The drain loop wakes this often; the heartbeat is
    # sent on its own (usually slower) sub-timer. A batch caps how many outbox rows go in
    # one POST so a large backlog drains in bounded chunks.
    ship_interval_s: float = field(default_factory=lambda: _env_float("WISP_SHIP_INTERVAL_S", 5.0))
    ship_batch: int = field(default_factory=lambda: _env_int("WISP_SHIP_BATCH", 200))
    heartbeat_interval_s: int = field(
        default_factory=lambda: _env_int("WISP_HEARTBEAT_INTERVAL_S", 60))
    # Exponential backoff bounds for a failing central (WAN cut, central down). Capped so
    # a long outage doesn't stretch the retry to hours — when central returns, the backlog
    # ships promptly. Isolated from the poll loop, so this never affects local detection.
    ship_backoff_s: float = field(default_factory=lambda: _env_float("WISP_SHIP_BACKOFF_S", 2.0))
    ship_backoff_max_s: float = field(
        default_factory=lambda: _env_float("WISP_SHIP_BACKOFF_MAX_S", 300.0))
    ship_timeout_s: float = field(default_factory=lambda: _env_float("WISP_SHIP_TIMEOUT_S", 10.0))
    # Outbox high-water mark. Past this the shipper evicts the OLDEST 'rollup' rows (trend
    # analytics, reconstructable) — never an unsent 'event' (an outage record is sacred).
    # 0 = no cap (let it grow; an event is never dropped regardless).
    outbox_max_rows: int = field(default_factory=lambda: _env_int("WISP_OUTBOX_MAX_ROWS", 100000))

    # --- Central server (the aggregation plane — a separate process/deploy) ----
    # Used only by apps/central; the edge ignores these. The central store is its OWN
    # SQLite (the edge's stays untouched); Part B is where it may graduate to Postgres.
    # It authenticates ingest with the same `central_token` the edges present.
    central_db: Path = field(
        default_factory=lambda: Path(_env("WISP_CENTRAL_DB", str(DATA_DIR / "central.db"))))
    central_bind: str = field(default_factory=lambda: _env("WISP_CENTRAL_BIND", "0.0.0.0"))
    central_port: int = field(default_factory=lambda: _env_int("WISP_CENTRAL_PORT", 8443))

    # --- Dashboard session (the shared-PIN auth lives in server/auth.py) ------
    session_timeout_h: int = field(default_factory=lambda: _env_int("WISP_SESSION_TIMEOUT_H", 12))

    def effective_interval(self, device_count: int) -> int:
        """Poll cadence for the current fleet size. Adaptive mode off → just
        `poll_interval_s`. On → a small fleet (<= small_fleet_max) polls every
        `poll_interval_small_s` (faster detection), anything larger falls back to
        `poll_interval_s` (protect the box). Detection latency = this × down_consecutive."""
        if self.poll_interval_adaptive and device_count <= self.small_fleet_max:
            return self.poll_interval_small_s
        return self.poll_interval_s

    def central_enabled(self) -> bool:
        """Is this edge reporting to a central server? Empty WISP_CENTRAL_URL ⇒ the
        whole distributed layer is dormant (the Phase 10 back-compat anchor): no outbox
        writes, no shipper thread, behaviour identical to the standalone monitor."""
        return bool(self.central_url)

    def stale_threshold_s(self) -> int:
        """Seconds without a fresh poll before the monitor is 'down'. Honours an
        explicit override, else auto-derives a forgiving floor from the cadence so
        one slow cycle never trips the alarm."""
        return self.monitor_stale_after_s or max(180, 3 * self.poll_interval_s)

    def __str__(self) -> str:  # friendly one-liner for startup logs
        return (
            f"Config(db={self.db_path.name}, poll={self.poll_interval_s}s, "
            f"prober={self.prober}, notifier={self.notifier})"
        )


# Importable singleton. Construct lazily-friendly: it just reads env at import time.
CONFIG = Config()
