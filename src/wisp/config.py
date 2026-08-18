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

    proxy_enabled: bool = field(default_factory=lambda: _env_bool("WISP_PROXY_ENABLED", True))
    proxy_mgmt_ports: str = field(default_factory=lambda: _env("WISP_PROXY_MGMT_PORTS", "80,443"))
    proxy_session_ttl_s: int = field(
        default_factory=lambda: _env_int("WISP_PROXY_SESSION_TTL_S", 600))
    proxy_poll_hold_s: float = field(
        default_factory=lambda: _env_float("WISP_PROXY_POLL_HOLD_S", 25.0))
    proxy_workers: int = field(default_factory=lambda: _env_int("WISP_PROXY_WORKERS", 4))
    proxy_request_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_PROXY_REQUEST_TIMEOUT_S", 30.0))
    proxy_connect_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_PROXY_CONNECT_TIMEOUT_S", 5.0))
    proxy_max_body_bytes: int = field(
        default_factory=lambda: _env_int("WISP_PROXY_MAX_BODY_BYTES", 8 * 1024 * 1024))
    proxy_cache_enabled: bool = field(
        default_factory=lambda: _env_bool("WISP_PROXY_CACHE", True))
    proxy_cache_ttl_s: float = field(
        default_factory=lambda: _env_float("WISP_PROXY_CACHE_TTL_S", 300.0))
    proxy_cache_max_entries: int = field(
        default_factory=lambda: _env_int("WISP_PROXY_CACHE_MAX_ENTRIES", 128))
    proxy_cache_max_bytes: int = field(
        default_factory=lambda: _env_int("WISP_PROXY_CACHE_MAX_BYTES", 4 * 1024 * 1024))
    proxy_device_max_inflight: int = field(
        default_factory=lambda: _env_int("WISP_PROXY_DEVICE_MAX_INFLIGHT", 4))
    proxy_keepalive_idle_s: float = field(
        default_factory=lambda: _env_float("WISP_PROXY_KEEPALIVE_IDLE_S", 90.0))

    # Live ping: a scrolling stream of individual echoes for ONE device, for a
    # technician standing at it. Its own channel and its own kill switch on
    # purpose — it is NOT gated on `orgs.web_proxy`, which exists for the web
    # UI tunnel and is a much larger grant (a browser session onto the device's
    # own admin page). Watching a box answer pings is not that.
    liveping_enabled: bool = field(
        default_factory=lambda: _env_bool("WISP_LIVEPING_ENABLED", True))
    # The hard auto-stop. A live session is EPHEMERAL: it dies with this
    # deadline, with the process, and with a stop click, and it is enforced on
    # BOTH sides — central drops the session and the edge's own generator is
    # bounded by a packet count — so a central that goes silent cannot leave a
    # probe pinging a customer's gear forever.
    liveping_max_s: int = field(
        default_factory=lambda: _env_int("WISP_LIVEPING_MAX_S", 300))
    # A packet a second for a leaf, one every two for aggregation gear. The
    # slower rung is the same reason `pings_per_poll_infra` exists: a parent
    # box answers for everything under it, and ICMP rate-limiters read as
    # phantom loss — so an operator WATCHING an OLT could make the real sweep
    # report an outage on the very device they are standing next to.
    liveping_interval_ms: int = field(
        default_factory=lambda: _env_int("WISP_LIVEPING_INTERVAL_MS", 1000))
    liveping_infra_interval_ms: int = field(
        default_factory=lambda: _env_int("WISP_LIVEPING_INFRA_INTERVAL_MS", 2000))
    liveping_max_per_org: int = field(
        default_factory=lambda: _env_int("WISP_LIVEPING_MAX_PER_ORG", 3))
    # How long central holds the edge's exchange when there is nothing to say.
    # Shorter than the proxy's hold: this channel only runs while a session is
    # armed, and a shorter hold means a stop click lands sooner.
    liveping_poll_hold_s: float = field(
        default_factory=lambda: _env_float("WISP_LIVEPING_POLL_HOLD_S", 20.0))

    snmp_timeout_s: float = field(default_factory=lambda: _env_float("WISP_SNMP_TIMEOUT_S", 2.0))
    snmp_interval_s: int = field(default_factory=lambda: _env_int("WISP_SNMP_INTERVAL_S", 300))
    port_interval_s: int = field(default_factory=lambda: _env_int("WISP_PORT_INTERVAL_S", 300))
    gpon_interval_s: int = field(default_factory=lambda: _env_int("WISP_GPON_INTERVAL_S", 300))
    snmp_walk_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_SNMP_WALK_TIMEOUT_S", 20.0))
    gpon_walk_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_GPON_WALK_TIMEOUT_S", 75.0))
    gpon_request_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_GPON_REQUEST_TIMEOUT_S", 5.0))
    gpon_request_retries: int = field(
        default_factory=lambda: _env_int("WISP_GPON_REQUEST_RETRIES", 3))
    snmp_request_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_SNMP_REQUEST_TIMEOUT_S", 5.0))
    snmp_request_retries: int = field(
        default_factory=lambda: _env_int("WISP_SNMP_REQUEST_RETRIES", 3))
    port_walk_timeout_s: float = field(
        default_factory=lambda: _env_float("WISP_PORT_WALK_TIMEOUT_S", 60.0))
    port_identity_interval_s: float = field(
        default_factory=lambda: _env_float("WISP_PORT_IDENTITY_INTERVAL_S", 3600.0))
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
    web_optics_enabled: bool = field(
        default_factory=lambda: _env_bool("WISP_WEB_OPTICS_ENABLED", True))
    web_optics_interval_s: int = field(
        default_factory=lambda: _env_int("WISP_WEB_OPTICS_INTERVAL_S", 900))
    web_optics_max_age_s: int = field(
        default_factory=lambda: _env_int("WISP_WEB_OPTICS_MAX_AGE_S", 3600))
    web_optics_device_budget_s: int = field(
        default_factory=lambda: _env_int("WISP_WEB_OPTICS_DEVICE_BUDGET_S", 120))
    web_optics_browse_idle_s: int = field(
        default_factory=lambda: _env_int("WISP_WEB_OPTICS_BROWSE_IDLE_S", 180))
    nvr_interval_s: int = field(
        default_factory=lambda: _env_int("WISP_NVR_INTERVAL_S", 300))
    radius_enabled: bool = field(
        default_factory=lambda: _env_bool("WISP_RADIUS_ENABLED", True))
    radius_interval_s: int = field(
        default_factory=lambda: _env_int("WISP_RADIUS_INTERVAL_S", 3600))
    radius_timeout_s: int = field(
        default_factory=lambda: _env_int("WISP_RADIUS_TIMEOUT_S", 60))
    # THE HISTORIAN (central/history.py): bounded derived-number time series
    # sampled off sweeps that already run. Retentions are OPS knobs (they bound
    # disk + nightly-backup size), not display knobs — hence Config, not
    # app_settings. hist_enabled=0 stops every sample and the maintenance
    # thread; existing rows keep pruning on their own age either way.
    hist_enabled: bool = field(
        default_factory=lambda: _env_bool("WISP_HIST_ENABLED", True))
    hist_raw_hours: int = field(
        default_factory=lambda: _env_int("WISP_HIST_RAW_HOURS", 48))
    hist_olt_hour_days: int = field(
        default_factory=lambda: _env_int("WISP_HIST_OLT_HOUR_DAYS", 90))
    hist_pon_hour_days: int = field(
        default_factory=lambda: _env_int("WISP_HIST_PON_HOUR_DAYS", 14))
    hist_port_hour_days: int = field(
        default_factory=lambda: _env_int("WISP_HIST_PORT_HOUR_DAYS", 30))
    # The per-ONU tier is the cardinality outlier — 5,205 ONUs against 29 OLTs
    # and 177 eligible ports — so it gets its own two horizons instead of
    # riding the shared ones. The hour tier is a SHORT rolling window (the
    # post-splice / last-night question, ~125 k rows a day); the day tier is
    # the long record and stops well short of hist_day_days' 730, which at this
    # width would be ~3.8 M rows in one table. onu_events rides the day horizon
    # — the transition ledger must cover exactly the window the buckets do.
    hist_onu_hour_days: int = field(
        default_factory=lambda: _env_int("WISP_HIST_ONU_HOUR_DAYS", 2))
    hist_onu_day_days: int = field(
        default_factory=lambda: _env_int("WISP_HIST_ONU_DAY_DAYS", 180))
    hist_day_days: int = field(
        default_factory=lambda: _env_int("WISP_HIST_DAY_DAYS", 730))
    field_track_retention_days: int = field(
        default_factory=lambda: _env_int("WISP_FIELD_TRACK_RETENTION_DAYS", 7))
    field_track_max_accuracy_m: float = field(
        default_factory=lambda: _env_float("WISP_FIELD_TRACK_MAX_ACCURACY_M", 500.0))
    field_track_max_age_s: int = field(
        default_factory=lambda: _env_int("WISP_FIELD_TRACK_MAX_AGE_S", 86400))
    field_track_max_skew_s: int = field(
        default_factory=lambda: _env_int("WISP_FIELD_TRACK_MAX_SKEW_S", 300))
    field_track_rate_per_min: int = field(
        default_factory=lambda: _env_int("WISP_FIELD_TRACK_RATE_PER_MIN", 60))
    field_track_fresh_s: int = field(
        default_factory=lambda: _env_int("WISP_FIELD_TRACK_FRESH_S", 300))
    pon_fault_alerts: bool = field(
        default_factory=lambda: _env_bool("WISP_PON_FAULT_ALERTS", True))
    onu_pon_limit: int = field(
        default_factory=lambda: _env_int("WISP_ONU_PON_LIMIT", 64))
    onu_limit_alerts: bool = field(
        default_factory=lambda: _env_bool("WISP_ONU_LIMIT_ALERTS", True))
    onu_dup_mac_alerts: bool = field(
        default_factory=lambda: _env_bool("WISP_ONU_DUP_MAC_ALERTS", True))
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

    digest_interval_min: int = field(
        default_factory=lambda: _env_int("WISP_DIGEST_INTERVAL_MIN", 60)
    )
    alert_cooldown_min: int = field(
        default_factory=lambda: _env_int("WISP_ALERT_COOLDOWN_MIN", 30)
    )

    prober: str = field(default_factory=lambda: _env("WISP_PROBER", "icmp").lower())

    notify_retries: int = field(default_factory=lambda: _env_int("WISP_NOTIFY_RETRIES", 3))
    notify_retry_backoff_s: float = field(
        default_factory=lambda: _env_float("WISP_NOTIFY_RETRY_BACKOFF_S", 0.5)
    )
    display_tz: str = field(
        default_factory=lambda: _env("WISP_DISPLAY_TZ", "Asia/Kolkata"))

    enable_whatsapp: bool = field(
        default_factory=lambda: _env_bool("WISP_ENABLE_WHATSAPP", True))
    whatsapp_token: str = field(default_factory=lambda: _env("WISP_WHATSAPP_TOKEN", ""))
    whatsapp_phone_id: str = field(default_factory=lambda: _env("WISP_WHATSAPP_PHONE_ID", ""))
    whatsapp_template: str = field(
        default_factory=lambda: _env("WISP_WHATSAPP_TEMPLATE", "wisp_alert1"))
    whatsapp_lang: str = field(default_factory=lambda: _env("WISP_WHATSAPP_LANG", "en"))
    whatsapp_api_version: str = field(
        default_factory=lambda: _env("WISP_WHATSAPP_API_VERSION", "v20.0"))
    whatsapp_admin_number: str = field(
        default_factory=lambda: _env("WISP_WHATSAPP_ADMIN_NUMBER", ""))
    whatsapp_verify_token: str = field(
        default_factory=lambda: _env("WISP_WHATSAPP_VERIFY_TOKEN", ""))
    whatsapp_app_secret: str = field(
        default_factory=lambda: _env("WISP_WHATSAPP_APP_SECRET", ""))

    central_url: str = field(default_factory=lambda: _env("WISP_CENTRAL_URL", "").rstrip("/"))
    central_token: str = field(default_factory=lambda: _env("WISP_CENTRAL_TOKEN", ""))
    central_client_cert: str = field(default_factory=lambda: _env("WISP_CENTRAL_CLIENT_CERT", ""))
    central_client_key: str = field(default_factory=lambda: _env("WISP_CENTRAL_CLIENT_KEY", ""))
    central_ca_cert: str = field(default_factory=lambda: _env("WISP_CENTRAL_CA_CERT", ""))
    org_id: str = field(default_factory=lambda: _env("WISP_ORG_ID", "default"))
    node_id: str = field(default_factory=lambda: _env("WISP_NODE_ID", "") or _hostname())
    ship_timeout_s: float = field(default_factory=lambda: _env_float("WISP_SHIP_TIMEOUT_S", 10.0))
    # gzip the edge -> central body once it is worth the CPU. ONE knob doing two
    # jobs on purpose: it is the threshold AND the escape hatch (<= 0 sends
    # everything uncompressed), because when a fleet of unknown boxes starts
    # compressing, an operator wants one name to grep for, not two. Below ~4 KB
    # the saving is noise and the CPU is real on a small probe. Central always
    # accepts both, so turning this off needs nothing on the other side.
    ship_gzip_min_bytes: int = field(
        default_factory=lambda: _env_int("WISP_SHIP_GZIP_MIN_BYTES", 4096))
    tracemalloc_every: int = field(
        default_factory=lambda: _env_int("WISP_TRACEMALLOC_EVERY", 0))
    central_db: Path = field(
        default_factory=lambda: Path(_env("WISP_CENTRAL_DB", str(DATA_DIR / "central.db"))))
    secret_key: str = field(default_factory=lambda: _env("WISP_SECRET_KEY", ""))
    central_bind: str = field(default_factory=lambda: _env("WISP_CENTRAL_BIND", "0.0.0.0"))
    central_port: int = field(default_factory=lambda: _env_int("WISP_CENTRAL_PORT", 8443))
    releases_repo: str = field(default_factory=lambda: _env(
        "WISP_RELEASES_REPO", "haneeshbyreddy/ping_tool"))
    github_token: str = field(default_factory=lambda: _env("WISP_GITHUB_TOKEN", ""))
    app_releases_repo: str = field(default_factory=lambda: _env(
        "WISP_APP_RELEASES_REPO", ""))
    release_cache_dir: Path = field(
        default_factory=lambda: Path(_env("WISP_RELEASE_CACHE_DIR", str(DATA_DIR / "releases"))))
    central_tls_cert: str = field(default_factory=lambda: _env("WISP_CENTRAL_TLS_CERT", ""))
    central_tls_key: str = field(default_factory=lambda: _env("WISP_CENTRAL_TLS_KEY", ""))
    central_client_ca: str = field(default_factory=lambda: _env("WISP_CENTRAL_CLIENT_CA", ""))
    central_pki_dir: Path = field(
        default_factory=lambda: Path(_env("WISP_CENTRAL_PKI_DIR", str(DATA_DIR / "pki"))))
    central_node_stale_s: int = field(
        default_factory=lambda: _env_int("WISP_CENTRAL_NODE_STALE_S", 180))
    central_watchdog_interval_s: int = field(
        default_factory=lambda: _env_int("WISP_CENTRAL_WATCHDOG_INTERVAL_S", 0))
    showcase_enabled: bool = field(default_factory=lambda: _env_bool("WISP_SHOWCASE", True))
    rollout_health_window_s: int = field(
        default_factory=lambda: _env_int("WISP_ROLLOUT_HEALTH_WINDOW_S", 600))
    agent_health_deadline_s: int = field(
        default_factory=lambda: _env_int("WISP_AGENT_HEALTH_DEADLINE_S", 300))

    session_timeout_h: int = field(default_factory=lambda: _env_int("WISP_SESSION_TIMEOUT_H", 12))
    session_remember_days: int = field(
        default_factory=lambda: _env_int("WISP_SESSION_REMEMBER_DAYS", 30))
    session_trusted_admin_hours: int = field(
        default_factory=lambda: _env_int("WISP_SESSION_TRUSTED_ADMIN_H", 24))
    session_idle_minutes: int = field(
        default_factory=lambda: _env_int("WISP_SESSION_IDLE_MIN", 30))
    session_cookie_secure: bool = field(
        default_factory=lambda: _env_bool("WISP_SESSION_COOKIE_SECURE", True))
    trust_forwarded_for: bool = field(
        default_factory=lambda: _env_bool("WISP_TRUST_FORWARDED_FOR", True))

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
