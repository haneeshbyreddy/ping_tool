from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PHASE_STARTING = "starting"
PHASE_RUNNING = "running"
PHASE_ERROR = "error"

STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_STARTING = "starting"
STATE_ERROR = "error"
STATE_STALE = "stale"
STATE_UNKNOWN = "unknown"

_STALE_FLOOR_S = 180.0

def status_path(db_path: str | os.PathLike[str]) -> Path:
    return Path(db_path).parent / "status.json"

class StatusWriter:

    def __init__(self, path: str | os.PathLike[str], *, org_id: str, node_id: str,
                 central_url: str, interval_s: float, version: str) -> None:
        self.path = Path(path)
        self._base = {
            "v": 1,
            "org_id": org_id,
            "node_id": node_id,
            "central_url": central_url,
            "interval_s": interval_s,
            "version": version,
            "pid": os.getpid(),
        }

    def set_interval(self, interval_s: float) -> None:
        self._base["interval_s"] = interval_s

    def write(self, phase: str, *, ok: bool | None = None, error: str | None = None,
              devices: int | None = None) -> None:
        body = dict(self._base)
        body.update({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "phase": phase,
            "ok": ok,
            "error": error,
            "devices": devices,
        })
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(body))
            os.replace(tmp, self.path)
        except OSError:
            pass

@dataclass(frozen=True)
class StatusView:
    state: str
    detail: str
    raw: dict | None = None

def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def read_status(path: str | os.PathLike[str], *, now: datetime | None = None) -> StatusView:
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return StatusView(STATE_UNKNOWN, "probe has never reported — not running?")

    now = now or datetime.now(timezone.utc)
    ts = _parse_ts(raw.get("ts", ""))
    age_s = (now - ts).total_seconds() if ts else None
    age_txt = f"{int(age_s)}s ago" if age_s is not None else "unknown age"
    interval = float(raw.get("interval_s") or 60.0)
    stale_after = max(3.0 * interval, _STALE_FLOOR_S)

    phase = raw.get("phase")
    if phase == PHASE_ERROR:
        return StatusView(STATE_ERROR, raw.get("error") or "probe failed to start", raw)
    if age_s is None or age_s > stale_after:
        return StatusView(
            STATE_STALE, f"probe stopped reporting (last status {age_txt})", raw)
    if phase == PHASE_STARTING:
        return StatusView(STATE_STARTING, "probe starting up…", raw)
    if raw.get("ok"):
        n = raw.get("devices")
        return StatusView(
            STATE_OK, f"reporting {n if n is not None else '?'} device(s) — {age_txt}", raw)
    return StatusView(
        STATE_DEGRADED, raw.get("error") or f"last report to central failed ({age_txt})", raw)

_ENV_LINE = re.compile(r"^\s*\$env:(\w+)\s*=\s*'(.*)'\s*$")

def parse_env_ps1(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out
