from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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
    import socket
    try:
        return socket.gethostname() or "edge"
    except Exception:
        return "edge"

@dataclass(frozen=True)
class Config:
    db_path: Path = field(
        default_factory=lambda: Path(_env("WISP_DB", str(DATA_DIR / "wisp.db")))
    )

    poll_interval_s: int = field(default_factory=lambda: _env_int("WISP_POLL_INTERVAL_S", 60))
    poll_interval_adaptive: bool = field(
        default_factory=lambda: _env_bool("WISP_POLL_INTERVAL_ADAPTIVE", False)
    )
    poll_interval_small_s: int = field(
        default_factory=lambda: _env_int("WISP_POLL_INTERVAL_SMALL_S", 30)
    )
    small_fleet_max: int = field(default_factory=lambda: _env_int("WISP_SMALL_FLEET_MAX", 1000))
    retry_interval_s: float = field(
        default_factory=lambda: _env_float("WISP_RETRY_INTERVAL_S", 2.0)
    )
    pings_per_poll: int = field(default_factory=lambda: _env_int("WISP_PINGS_PER_POLL", 5))
    pings_per_poll_infra: int = field(
        default_factory=lambda: _env_int("WISP_PINGS_PER_POLL_INFRA", 2)
    )
    probe_max_inflight: int = field(
        default_factory=lambda: _env_int("WISP_MAX_INFLIGHT", 256)
    )
    perf_window: int = field(default_factory=lambda: _env_int("WISP_PERF_WINDOW", 20))
    perf_min_samples: int = field(default_factory=lambda: _env_int("WISP_PERF_MIN_SAMPLES", 10))
    perf_consecutive: int = field(default_factory=lambda: _env_int("WISP_PERF_CONSECUTIVE", 3))
    perf_deviation_factor: float = field(
        default_factory=lambda: _env_float("WISP_PERF_DEVIATION_FACTOR", 3.0))
    perf_mad_k: float = field(default_factory=lambda: _env_float("WISP_PERF_MAD_K", 5.0))
    perf_min_baseline_ms: float = field(
        default_factory=lambda: _env_float("WISP_PERF_MIN_BASELINE_MS", 5.0))
    perf_min_jitter_ms: float = field(
        default_factory=lambda: _env_float("WISP_PERF_MIN_JITTER_MS", 3.0))
    perf_alerts: bool = field(default_factory=lambda: _env_bool("WISP_PERF_ALERTS", True))

    backup_alerts: bool = field(default_factory=lambda: _env_bool("WISP_BACKUP_ALERTS", True))

    # Device web-UI proxy (reverse tunnel through the edge). Ships DARK — the
    # whole feature is a no-op until proxy_enabled. See webplan.md. proxy_mgmt_ports
    # is the CLOSED set of device ports the tunnel may reach; a session may target
    # nothing else (the anti-pivot clamp, alongside the edge's device-list gate).
    proxy_enabled: bool = field(default_factory=lambda: _env_bool("WISP_PROXY_ENABLED", False))
    proxy_mgmt_ports: str = field(default_factory=lambda: _env("WISP_PROXY_MGMT_PORTS", "80,443"))
    proxy_session_ttl_s: int = field(
        default_factory=lambda: _env_int("WISP_PROXY_SESSION_TTL_S", 600))
    proxy_poll_hold_s: float = field(
        default_factory=lambda: _env_float("WISP_PROXY_POLL_HOLD_S", 25.0))
    proxy_workers: int = field(default_factory=lambda: _env_int("WISP_PROXY_WORKERS", 4))
    proxy_request_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_PROXY_REQUEST_TIMEOUT_S", 30.0))
    proxy_max_body_bytes: int = field(
        default_factory=lambda: _env_int("WISP_PROXY_MAX_BODY_BYTES", 8 * 1024 * 1024))

    snmp_timeout_s: float = field(default_factory=lambda: _env_float("WISP_SNMP_TIMEOUT_S", 2.0))
    snmp_interval_s: int = field(default_factory=lambda: _env_int("WISP_SNMP_INTERVAL_S", 90))
    snmp_walk_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_SNMP_WALK_TIMEOUT_S", 20.0))
    # GPON roster walks get their own, larger cap: a slow EPON agent (PYLON/NDN
    # class) needs >20s for 5 roster columns x hundreds of ONUs, and the optics
    # sweep is a background task under the SNMP semaphore — a slow OLT delays
    # nothing but its own reading. 20s starved those boxes into permanently
    # stale optics (field-diagnosed 2026-07-09 via remote diag walks).
    gpon_walk_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_GPON_WALK_TIMEOUT_S", 75.0))
    # Per-REQUEST tolerance for the GPON roster walk, separate from the global 2s
    # snmp_timeout_s. A slow C-Data/DBC EPON agent (PYLON class) intermittently
    # drops or delays a single GETBULK on the big .12 registration table; at 2s x
    # 1 retry one unanswered request fails the WHOLE walk ("No SNMP response
    # received before timeout") and freezes the roster for a full snmp_interval —
    # field-diagnosed 2026-07-13, PYLON roster stuck ~25 min while health/ports
    # stayed fresh on the same box. More time + more retries per request rides out
    # the slow spells; the gpon_walk_timeout_s cap still bounds the total.
    gpon_request_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_GPON_REQUEST_TIMEOUT_S", 5.0))
    gpon_request_retries: int = field(
        default_factory=lambda: _env_int("WISP_GPON_REQUEST_RETRIES", 3))
    # Port (ifTable) walks get their own cap for the same reason GPON does: a big OLT
    # (HILL/PYLON class, 200+ interfaces x 10 columns) can't finish 10 bulk-walk
    # columns inside 20s, timed out every cycle, and left switch_ports permanently
    # stale while health/optics stayed fresh (same box, smaller walks) — field-
    # diagnosed 2026-07-09. Like optics it's a background task under the SNMP
    # semaphore, so a slow OLT delays nothing but its own port reading.
    port_walk_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_PORT_WALK_TIMEOUT_S", 60.0))
    snmp_max_inflight: int = field(
        default_factory=lambda: _env_int("WISP_SNMP_MAX_INFLIGHT", 4))
    snmp_down_consecutive: int = field(
        default_factory=lambda: _env_int("WISP_SNMP_DOWN_CONSECUTIVE", 2))
    snmp_alerts: bool = field(default_factory=lambda: _env_bool("WISP_SNMP_ALERTS", True))
    snmp_bw_consecutive: int = field(
        default_factory=lambda: _env_int("WISP_SNMP_BW_CONSECUTIVE", 3))
    snmp_bw_alerts: bool = field(default_factory=lambda: _env_bool("WISP_SNMP_BW_ALERTS", True))
    optical_warn_dbm: float = field(
        default_factory=lambda: _env_float("WISP_OPTICAL_WARN_DBM", -24.0))
    optical_crit_dbm: float = field(
        default_factory=lambda: _env_float("WISP_OPTICAL_CRIT_DBM", -27.0))
    optical_alerts: bool = field(default_factory=lambda: _env_bool("WISP_OPTICAL_ALERTS", True))
    # PON mass-drop heads-up (central/ponalert.py): page the operator when a
    # PON reads as a fiber cut. State is tracked regardless; only paging gates.
    pon_fault_alerts: bool = field(
        default_factory=lambda: _env_bool("WISP_PON_FAULT_ALERTS", True))
    # ONU-roster hygiene (central/onualert.py): per-PON ONU cap (EPON tops out at a
    # 1:64 split — page when a PON is full) and redundant-MAC detection (same ONU MAC
    # on 2+ slots = clone/loop/double-registration). Per-OLT `onu_pon_limit` override
    # (org_devices) raises the cap for a 1:128 GPON box. State tracked regardless of
    # the gates, like ponalert; both page the operator transition-only.
    onu_pon_limit: int = field(
        default_factory=lambda: _env_int("WISP_ONU_PON_LIMIT", 64))
    onu_limit_alerts: bool = field(
        default_factory=lambda: _env_bool("WISP_ONU_LIMIT_ALERTS", True))
    onu_dup_mac_alerts: bool = field(
        default_factory=lambda: _env_bool("WISP_ONU_DUP_MAC_ALERTS", True))
    # Empty = per-OLT sysObjectID auto-detect (the normal path). Set to force one
    # vendor profile on every untagged OLT this edge probes — an escape hatch for
    # a box whose sysObjectID is missing or lies; per-device `gpon_vendor` from the
    # dashboard overrides both.
    gpon_vendor: str = field(default_factory=lambda: _env("WISP_GPON_VENDOR", ""))

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

    canary_ip: str = field(default_factory=lambda: _env("WISP_CANARY_IP", "1.1.1.1"))
    canary_freeze: bool = field(
        default_factory=lambda: _env_bool("WISP_CANARY_FREEZE", True)
    )

    escalate_every_min: int = field(
        default_factory=lambda: _env_int("WISP_ESCALATE_EVERY_MIN", 60)
    )

    prober: str = field(default_factory=lambda: _env("WISP_PROBER", "icmp").lower())

    ntfy_base_url: str = field(default_factory=lambda: _env("WISP_NTFY_URL", "https://ntfy.sh"))
    ntfy_retries: int = field(default_factory=lambda: _env_int("WISP_NTFY_RETRIES", 3))
    ntfy_retry_backoff_s: float = field(
        default_factory=lambda: _env_float("WISP_NTFY_RETRY_BACKOFF_S", 0.5)
    )

    central_url: str = field(default_factory=lambda: _env("WISP_CENTRAL_URL", "").rstrip("/"))
    central_token: str = field(default_factory=lambda: _env("WISP_CENTRAL_TOKEN", ""))
    central_client_cert: str = field(default_factory=lambda: _env("WISP_CENTRAL_CLIENT_CERT", ""))
    central_client_key: str = field(default_factory=lambda: _env("WISP_CENTRAL_CLIENT_KEY", ""))
    central_ca_cert: str = field(default_factory=lambda: _env("WISP_CENTRAL_CA_CERT", ""))
    org_id: str = field(default_factory=lambda: _env("WISP_ORG_ID", "default"))
    node_id: str = field(default_factory=lambda: _env("WISP_NODE_ID", "") or _hostname())
    ship_timeout_s: float = field(default_factory=lambda: _env_float("WISP_SHIP_TIMEOUT_S", 10.0))
    tracemalloc_every: int = field(
        default_factory=lambda: _env_int("WISP_TRACEMALLOC_EVERY", 0))
    central_db: Path = field(
        default_factory=lambda: Path(_env("WISP_CENTRAL_DB", str(DATA_DIR / "central.db"))))
    central_bind: str = field(default_factory=lambda: _env("WISP_CENTRAL_BIND", "0.0.0.0"))
    central_port: int = field(default_factory=lambda: _env_int("WISP_CENTRAL_PORT", 8443))
    # Release mirror: central pulls the latest release's assets (installers +
    # agent binaries + manifest) into `release_cache_dir` and serves them at
    # /download/ — edges never talk to GitHub. The repo is public, so the token
    # is optional (only needed to lift the anonymous API rate limit or if the
    # repo ever goes private again).
    releases_repo: str = field(default_factory=lambda: _env(
        "WISP_RELEASES_REPO", "haneeshbyreddy/ping_tool"))
    github_token: str = field(default_factory=lambda: _env("WISP_GITHUB_TOKEN", ""))
    release_cache_dir: Path = field(
        default_factory=lambda: Path(_env("WISP_RELEASE_CACHE_DIR", str(DATA_DIR / "releases"))))
    central_tls_cert: str = field(default_factory=lambda: _env("WISP_CENTRAL_TLS_CERT", ""))
    central_tls_key: str = field(default_factory=lambda: _env("WISP_CENTRAL_TLS_KEY", ""))
    central_client_ca: str = field(default_factory=lambda: _env("WISP_CENTRAL_CLIENT_CA", ""))
    central_pki_dir: Path = field(
        default_factory=lambda: Path(_env("WISP_CENTRAL_PKI_DIR", str(DATA_DIR / "pki"))))
    central_node_stale_s: int = field(
        default_factory=lambda: _env_int("WISP_CENTRAL_NODE_STALE_S", 180))
    central_ntfy_topic: str = field(
        default_factory=lambda: _env("WISP_CENTRAL_NTFY_TOPIC", "wisp-central"))
    central_watchdog_interval_s: int = field(
        default_factory=lambda: _env_int("WISP_CENTRAL_WATCHDOG_INTERVAL_S", 0))
    # Public marketing landing (`/`) shows a DB-driven "trusted by" ticker of org
    # names + an early-access offer bar. Server-injected; off hides both entirely.
    showcase_enabled: bool = field(default_factory=lambda: _env_bool("WISP_SHOWCASE", True))
    rollout_health_window_s: int = field(
        default_factory=lambda: _env_int("WISP_ROLLOUT_HEALTH_WINDOW_S", 600))
    agent_health_deadline_s: int = field(
        default_factory=lambda: _env_int("WISP_AGENT_HEALTH_DEADLINE_S", 300))

    session_timeout_h: int = field(default_factory=lambda: _env_int("WISP_SESSION_TIMEOUT_H", 12))
    # "Trust this device" at login rides a much longer TTL so an operator's own
    # box isn't kicked back to the sign-in form every shift. Baked into the signed
    # cookie at issue time (auth.issue_session), not re-read per request.
    session_remember_days: int = field(
        default_factory=lambda: _env_int("WISP_SESSION_REMEMBER_DAYS", 30))

    def effective_interval(self, device_count: int) -> int:
        if self.poll_interval_adaptive and device_count <= self.small_fleet_max:
            return self.poll_interval_small_s
        return self.poll_interval_s

    def __str__(self) -> str:
        return (
            f"Config(db={self.db_path.name}, poll={self.poll_interval_s}s, "
            f"prober={self.prober})"
        )

CONFIG = Config()
