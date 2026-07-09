"""Central host health snapshot — CPU, memory, disk — pure stdlib.

Linux-first: CPU and memory come from /proc (the deployment target); every field
degrades to None elsewhere or on read failure so the endpoint never 500s over a
missing pseudo-file. CPU% is a delta between successive calls (the dashboard's
poll cadence is the sampling window), with a short two-point sample on the very
first call so it never reports a meaningless since-boot average.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from pathlib import Path

_MIN_CPU_WINDOW_S = 1.0
_FIRST_SAMPLE_S = 0.15

_lock = threading.Lock()
_last_cpu: tuple[float, float, float] | None = None  # (busy, total, monotonic)
_last_pct: float | None = None


def _read_cpu_times() -> tuple[float, float]:
    with open("/proc/stat") as fh:
        fields = [float(x) for x in fh.readline().split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)  # idle + iowait
    return sum(fields) - idle, sum(fields)


def _cpu_percent() -> float | None:
    global _last_cpu, _last_pct
    with _lock:
        prev = _last_cpu
        try:
            if prev is None:
                b0, t0 = _read_cpu_times()
                prev = (b0, t0, time.monotonic())
                time.sleep(_FIRST_SAMPLE_S)
            busy, total = _read_cpu_times()
        except OSError:
            return None
        now = time.monotonic()
        if _last_cpu is not None and (now - prev[2] < _MIN_CPU_WINDOW_S or total <= prev[1]):
            return _last_pct  # window too small to be meaningful; reuse last
        dt = total - prev[1]
        if dt > 0:
            _last_pct = round(min(100.0, max(0.0, 100.0 * (busy - prev[0]) / dt)), 1)
        _last_cpu = (busy, total, now)
        return _last_pct


def _meminfo() -> dict | None:
    try:
        with open("/proc/meminfo") as fh:
            kv = {}
            for line in fh:
                name, _, rest = line.partition(":")
                kv[name] = int(rest.split()[0]) * 1024  # kB -> bytes
    except (OSError, ValueError, IndexError):
        return None
    total = kv.get("MemTotal")
    avail = kv.get("MemAvailable")
    if not total or avail is None:
        return None
    used = total - avail
    return {"total_bytes": total, "used_bytes": used, "available_bytes": avail,
            "percent": round(100.0 * used / total, 1)}


def _disk(path: Path) -> dict | None:
    probe = path if path.exists() else path.parent
    try:
        du = shutil.disk_usage(probe)
    except OSError:
        return None
    return {"total_bytes": du.total, "used_bytes": du.used, "free_bytes": du.free,
            "percent": round(100.0 * du.used / du.total, 1) if du.total else 0.0}


def _uptime_s() -> float | None:
    try:
        with open("/proc/uptime") as fh:
            return float(fh.readline().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _process_rss_bytes() -> int | None:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def snapshot(db_path: Path) -> dict:
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = None
    try:
        db_bytes = db_path.stat().st_size
    except OSError:
        db_bytes = None
    return {
        "hostname": socket.gethostname(),
        "uptime_s": _uptime_s(),
        "cpu": {"percent": _cpu_percent(), "cores": os.cpu_count(),
                "load": list(load) if load else None},
        "memory": _meminfo(),
        "disk": _disk(Path(db_path).resolve()),
        "process": {"rss_bytes": _process_rss_bytes(), "db_bytes": db_bytes},
    }
