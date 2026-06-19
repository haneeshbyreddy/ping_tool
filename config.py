"""Central configuration for the Village WISP Monitor.

All tunables live here as a single frozen dataclass loaded from the environment
with sensible defaults, so the system runs out-of-the-box with zero setup and is
reconfigured purely through env vars (no code edits) when hardware/credentials arrive.

Nothing in here imports the rest of the project, so it is safe to import anywhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Config:
    # --- Storage -------------------------------------------------------------
    db_path: Path = field(
        default_factory=lambda: Path(_env("WISP_DB", str(PROJECT_ROOT / "wisp.db")))
    )
    migrations_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "migrations")
    # SQLite waits this long for a competing writer before raising "database is locked".
    busy_timeout_ms: int = field(default_factory=lambda: _env_int("WISP_BUSY_TIMEOUT_MS", 5000))

    # --- Polling -------------------------------------------------------------
    poll_interval_s: int = field(default_factory=lambda: _env_int("WISP_POLL_INTERVAL_S", 60))
    pings_per_poll: int = field(default_factory=lambda: _env_int("WISP_PINGS_PER_POLL", 5))

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

    # --- Escalation timing (minutes) ----------------------------------------
    realert_after_min: int = field(default_factory=lambda: _env_int("WISP_REALERT_MIN", 10))
    escalate_owner_after_min: int = field(
        default_factory=lambda: _env_int("WISP_ESCALATE_MIN", 20)
    )
    # A recipient won't get the same device's alert more than once per this window
    # (except the explicit escalation steps above).
    alert_dedupe_min: int = field(default_factory=lambda: _env_int("WISP_ALERT_DEDUPE_MIN", 10))

    # --- Provider selection (mock-first; swap to real later) -----------------
    # prober: 'simulated' | 'icmp'      notifier: 'mock' | 'ntfy' | 'telegram'
    prober: str = field(default_factory=lambda: _env("WISP_PROBER", "simulated").lower())
    notifier: str = field(default_factory=lambda: _env("WISP_NOTIFIER", "mock").lower())

    # --- Channel credentials (only needed once real notifiers are selected) --
    ntfy_base_url: str = field(default_factory=lambda: _env("WISP_NTFY_URL", "https://ntfy.sh"))
    telegram_bot_token: str = field(default_factory=lambda: _env("WISP_TG_TOKEN", ""))
    owner_telegram_chat_id: str = field(default_factory=lambda: _env("WISP_OWNER_CHAT", ""))

    def __str__(self) -> str:  # friendly one-liner for startup logs
        return (
            f"Config(db={self.db_path.name}, poll={self.poll_interval_s}s, "
            f"prober={self.prober}, notifier={self.notifier})"
        )


# Importable singleton. Construct lazily-friendly: it just reads env at import time.
CONFIG = Config()
