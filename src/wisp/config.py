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
    # The edge keeps no database of its own — this is just the per-node data directory
    # anchor: the single-instance lock file (`apps/daemon/main.py`) and the supervisor's
    # transient download/update-request files (`apps/supervisor/main.py`) live next to it.
    db_path: Path = field(
        default_factory=lambda: Path(_env("WISP_DB", str(DATA_DIR / "wisp.db")))
    )

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
    # A degraded/recovered edge always updates the device_perf badge; this gates
    # whether central actually PAGES the operator about it.
    perf_alerts: bool = field(default_factory=lambda: _env_bool("WISP_PERF_ALERTS", True))

    # --- Redundancy / on-backup signal ----------------------------------------
    # A device with a BACKUP parent edge (org_device_links, kind='backup') that loses
    # its primary uplink but is still reachable via the backup is "on backup" — a
    # heads-up (redundancy is gone, one more failure is an outage), not an outage
    # itself. An enter/leave edge always updates the device_redundancy badge; this
    # gates whether central actually PAGES the operator about it.
    backup_alerts: bool = field(default_factory=lambda: _env_bool("WISP_BACKUP_ALERTS", True))

    # --- SNMP ingress (graph topology Part B; IF-MIB oper/admin only) --------
    # `ingress/snmp.py`'s poller, wired into the central-brain daemon loop on its own
    # slow cadence (ports don't flap like radio links) — see apps/daemon/main.py's
    # `_gather_snmp_ports` + `POST /report`'s `ports` key, folded/alerted centrally by
    # `central/ports.py:CentralPortMonitor`. SNMP request timeout per walk, kept short
    # since a dead switch must never block anything else (ICMP or SNMP).
    snmp_timeout_s: float = field(default_factory=lambda: _env_float("WISP_SNMP_TIMEOUT_S", 2.0))
    # How often the edge walks its snmp-enabled switches (independent of poll_interval_s
    # — a port flapping every 30s is a switch bug, not a signal to chase that fast). 0
    # disables the SNMP task entirely.
    snmp_interval_s: int = field(default_factory=lambda: _env_int("WISP_SNMP_INTERVAL_S", 90))
    # Consecutive down walks a MONITORED port needs before central alarms it (flap
    # suppression, same discipline as the ICMP FSM's down_consecutive).
    snmp_down_consecutive: int = field(
        default_factory=lambda: _env_int("WISP_SNMP_DOWN_CONSECUTIVE", 2))
    # A monitored port-down/restored always updates switch_ports; this gates whether
    # central actually PAGES the operator about it (state is written either way).
    snmp_alerts: bool = field(default_factory=lambda: _env_bool("WISP_SNMP_ALERTS", True))
    # --- Per-port bandwidth (throughput) low-threshold alarm (CLAUDE.md item 3) -----
    # Orthogonal to oper/admin status: each walk reads the 64-bit byte counters and
    # central diffs them into a rate (bits/sec). A monitored port whose rate falls
    # below its operator-assigned threshold for this many consecutive walks alarms —
    # its own streak, because traffic is burstier than link state (a port that's up
    # but momentarily idle shouldn't page on a single quiet walk).
    snmp_bw_consecutive: int = field(
        default_factory=lambda: _env_int("WISP_SNMP_BW_CONSECUTIVE", 3))
    # Gate the low-bandwidth page (the switch_ports bw state is always written).
    snmp_bw_alerts: bool = field(default_factory=lambda: _env_bool("WISP_SNMP_BW_ALERTS", True))

    # --- State-machine thresholds (see CLAUDE.md §"State machine") -------------
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

    # --- Providers (real adapters: ICMP ping + ntfy push) --------------------
    prober: str = field(default_factory=lambda: _env("WISP_PROBER", "icmp").lower())

    # --- Channel credentials (only needed once real notifiers are selected) --
    ntfy_base_url: str = field(default_factory=lambda: _env("WISP_NTFY_URL", "https://ntfy.sh"))
    # A page must not be silently lost to a transient blip: retry a failed send
    # this many times with exponential backoff (only network/5xx errors retry;
    # a 4xx is a config error and fails fast). Kept short — it runs outside any DB txn.
    ntfy_retries: int = field(default_factory=lambda: _env_int("WISP_NTFY_RETRIES", 3))
    ntfy_retry_backoff_s: float = field(
        default_factory=lambda: _env_float("WISP_NTFY_RETRY_BACKOFF_S", 0.5)
    )

    # --- Central reporting (Phase B — the edge is a thin probe) ---------------
    # THE back-compat anchor: WISP_CENTRAL_URL empty ⇒ the edge has nowhere to report
    # to and refuses to start (see main()). Set it to the central ingest base URL
    # (e.g. https://central.example.net). All detection/alerting happens on central —
    # the edge only probes and ships raw per-IP samples.
    central_url: str = field(default_factory=lambda: _env("WISP_CENTRAL_URL", "").rstrip("/"))
    # The original stopgap auth (now that mTLS enrollment exists below, this is the
    # fallback/coexistence path, not the only option): a per-node shared secret sent as
    # `Authorization: Bearer <token>`. Keep it out of logs.
    central_token: str = field(default_factory=lambda: _env("WISP_CENTRAL_TOKEN", ""))
    # --- mTLS enrollment (CLAUDE.md item 6) — the edge's half -------------------
    # Client identity for the ingest TLS handshake, issued by `central.admin
    # enroll-edge` (see central/pki.py). Empty ⇒ the edge presents no client cert and
    # falls back to the bearer token above — both can be set at once during a
    # migration (either satisfies central's ingest auth), so turning this on is not a
    # hard cutover.
    central_client_cert: str = field(default_factory=lambda: _env("WISP_CENTRAL_CLIENT_CERT", ""))
    central_client_key: str = field(default_factory=lambda: _env("WISP_CENTRAL_CLIENT_KEY", ""))
    # CA bundle to verify central's own TLS server cert against (needed only when
    # central's cert isn't from a public CA the OS trust store already has — the
    # common case for the internal CA `central.admin init-ca` creates).
    central_ca_cert: str = field(default_factory=lambda: _env("WISP_CENTRAL_CA_CERT", ""))
    # Edge identity. (org_id, node_id) is the durable identity central keys every
    # record by. node_id defaults to the hostname so a fresh install still has a
    # stable id.
    org_id: str = field(default_factory=lambda: _env("WISP_ORG_ID", "default"))
    node_id: str = field(default_factory=lambda: _env("WISP_NODE_ID", "") or _hostname())
    # HTTP timeout for GET /edge/devices and POST /report.
    ship_timeout_s: float = field(default_factory=lambda: _env_float("WISP_SHIP_TIMEOUT_S", 10.0))
    # Historical opt-in flag from when central-brain mode was one of three daemon
    # modes; the daemon now has only this mode, but `central_brain_enabled()` (still
    # exercised directly by tests) keeps the "needs central_url too" contract explicit.
    central_brain_mode: bool = field(
        default_factory=lambda: _env_bool("WISP_CENTRAL_BRAIN", False))

    # --- Central server (the aggregation plane — a separate process/deploy) ----
    # Used only by apps/central; the edge ignores these. The central store is its OWN
    # SQLite (the edge's stays untouched); Part B is where it may graduate to Postgres.
    # It authenticates ingest with the same `central_token` the edges present.
    central_db: Path = field(
        default_factory=lambda: Path(_env("WISP_CENTRAL_DB", str(DATA_DIR / "central.db"))))
    central_bind: str = field(default_factory=lambda: _env("WISP_CENTRAL_BIND", "0.0.0.0"))
    central_port: int = field(default_factory=lambda: _env_int("WISP_CENTRAL_PORT", 8443))
    # --- mTLS enrollment (CLAUDE.md item 6) — central's half --------------------
    # Central's own TLS identity. Both empty (the default) ⇒ central serves plain
    # HTTP exactly as before — fully backward compatible; every existing deploy and
    # test keeps working unchanged. Set both to switch the listener to HTTPS (a cert
    # from `central.admin init-ca`, or a real one — either works, they're just PEM
    # files). `central/pki.py`'s `issue_cert`/`init-ca` writes both under
    # `WISP_CENTRAL_PKI_DIR` by default.
    central_tls_cert: str = field(default_factory=lambda: _env("WISP_CENTRAL_TLS_CERT", ""))
    central_tls_key: str = field(default_factory=lambda: _env("WISP_CENTRAL_TLS_KEY", ""))
    # The CA bundle to verify an edge's PRESENTED client cert against (mTLS). Empty ⇒
    # ingest stays bearer-token-only even over HTTPS — client certs are simply never
    # requested. Set once you've run `central.admin init-ca` and enrolled edges with
    # `enroll-edge`; a verified cert then satisfies ingest auth as an alternative to
    # the bearer token (self-activating, same pattern as the fleet installers'
    # minisign/Authenticode checks — see CLAUDE.md's "Fleet update hardening").
    central_client_ca: str = field(default_factory=lambda: _env("WISP_CENTRAL_CLIENT_CA", ""))
    # Where `central.admin init-ca`/`enroll-edge` read/write the CA + issued certs.
    central_pki_dir: Path = field(
        default_factory=lambda: Path(_env("WISP_CENTRAL_PKI_DIR", str(DATA_DIR / "pki"))))
    # Cross-edge fleet watchdog (Part B): central pages (per-org) when a node's heartbeat
    # goes silent — box dead OR WAN cut. A node is "stale" after this many seconds without a
    # heartbeat; keep it a comfortable multiple of WISP_HEARTBEAT_INTERVAL_S so one missed
    # beat never false-alarms (default 180 = 3 × the 60s default heartbeat).
    central_node_stale_s: int = field(
        default_factory=lambda: _env_int("WISP_CENTRAL_NODE_STALE_S", 180))
    # Fallback ntfy topic for the fleet watchdog when an org has set no per-org topic.
    central_ntfy_topic: str = field(
        default_factory=lambda: _env("WISP_CENTRAL_NTFY_TOPIC", "wisp-central"))
    # 0 = auto (max(30, node_stale/2)); how often the central watchdog re-evaluates liveness.
    central_watchdog_interval_s: int = field(
        default_factory=lambda: _env_int("WISP_CENTRAL_WATCHDOG_INTERVAL_S", 0))
    # --- Staged rollout / self-update (Part D) -------------------------------
    # How long a CANARY node has, after central tells it to update, to come back reporting the
    # target version with a fresh heartbeat. If a canary misses that window the rollout
    # auto-halts (it never promotes a bad version fleet-wide). "Update every node" must never
    # mean "brick every node at once."
    rollout_health_window_s: int = field(
        default_factory=lambda: _env_int("WISP_ROLLOUT_HEALTH_WINDOW_S", 600))
    # Edge supervisor: after swapping in a new agent binary, how long it has to prove healthy
    # (its preflight + a successful heartbeat) before the supervisor rolls back to last-known-good.
    agent_health_deadline_s: int = field(
        default_factory=lambda: _env_int("WISP_AGENT_HEALTH_DEADLINE_S", 300))

    # --- Central dashboard session (the login/session crypto lives in central/auth.py) --
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
        """Is a central URL configured at all? A helper for `central_brain_enabled()` —
        the edge has no other mode, so this is really just "is central_url set"."""
        return bool(self.central_url)

    def central_brain_enabled(self) -> bool:
        """Central-brain mode needs central_url too — WISP_CENTRAL_BRAIN=1 with no
        central_url is a no-op, not an error."""
        return self.central_enabled() and self.central_brain_mode

    def __str__(self) -> str:  # friendly one-liner for startup logs
        return (
            f"Config(db={self.db_path.name}, poll={self.poll_interval_s}s, "
            f"prober={self.prober})"
        )


# Importable singleton. Construct lazily-friendly: it just reads env at import time.
CONFIG = Config()
