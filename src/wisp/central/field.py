from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone

from wisp.config import CONFIG, Config
from wisp.egress.notifiers import _display_zone

log = logging.getLogger("wisp.central.field")

_KNOTS_TO_MPS = 0.514444


class TrackError(ValueError):
    pass


class TrackDropped(ValueError):
    pass


def param(params: dict, *names: str) -> str:

    for n in names:
        v = params.get(n)
        if isinstance(v, list):
            v = v[0] if v else None
        if v is None:
            continue
        v = str(v).strip()
        if v:
            return v
    return ""


def _float(raw: str) -> float | None:
    if not raw:
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def parse_ts(raw: str, now: datetime) -> datetime:

    if not raw:
        return now
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace(" ", "T").replace("Z", "+00:00"))
        except ValueError:
            raise TrackError("timestamp is neither unix seconds nor ISO8601")
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if secs > 1e11:
        secs /= 1000.0
    try:
        return datetime.fromtimestamp(secs, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise TrackError("timestamp is out of range")


def clean_fix(params: dict, cfg: Config = CONFIG,
              now: datetime | None = None) -> dict:

    now = now or datetime.now(timezone.utc)
    lat = _float(param(params, "lat", "latitude"))
    lng = _float(param(params, "lon", "lng", "longitude"))
    if lat is None or lng is None:
        raise TrackError("lat and lon are both required")
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise TrackError("lat/lon out of range")

    ts = parse_ts(param(params, "timestamp", "time"), now)
    age = (now - ts).total_seconds()
    if age > cfg.field_track_max_age_s:
        raise TrackDropped(f"fix is {int(age)}s old")
    if age < -cfg.field_track_max_skew_s:
        raise TrackDropped(f"fix is stamped {int(-age)}s in the future")

    accuracy = _float(param(params, "accuracy", "hdop"))
    if accuracy is not None and accuracy < 0:
        accuracy = None
    if accuracy is not None and accuracy > cfg.field_track_max_accuracy_m:
        raise TrackDropped(f"accuracy ±{int(accuracy)} m is a cell-tower estimate")

    speed_kn = _float(param(params, "speed"))
    heading = _float(param(params, "bearing", "heading", "course"))
    battery = _float(param(params, "batt", "battery"))
    if battery is not None and not (0.0 <= battery <= 100.0):
        battery = None
    if heading is not None and not (0.0 <= heading <= 360.0):
        heading = None

    return {
        "ts": ts.isoformat(timespec="seconds"),
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "accuracy_m": round(accuracy, 1) if accuracy is not None else None,
        "speed_mps": (round(max(speed_kn, 0.0) * _KNOTS_TO_MPS, 2)
                      if speed_kn is not None else None),
        "heading": round(heading, 1) if heading is not None else None,
        "battery_pct": round(battery) if battery is not None else None,
    }


class TrackRate:


    def __init__(self, per_min: int = 60, burst_factor: float = 2.0) -> None:
        self.rate = max(per_min, 1) / 60.0
        self.capacity = max(per_min, 1) * burst_factor
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        t = _time.time() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, t))
            tokens = min(self.capacity, tokens + (t - last) * self.rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, t)
                return False
            self._buckets[key] = (tokens - 1.0, t)
            return True


def trail_since(cfg: Config = CONFIG, now: datetime | None = None) -> str:

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_display_zone(cfg.display_tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def prune_worker_locations(store, cfg: Config = CONFIG,
                           now: datetime | None = None) -> int:
    end = now or datetime.now(timezone.utc)
    cutoff = (end - timedelta(days=cfg.field_track_retention_days)
              ).isoformat(timespec="seconds")
    return store.prune_worker_locations(cutoff)


def start_field_prune_thread(cfg: Config = CONFIG, store=None) -> threading.Thread:

    from wisp.central.store import CentralStore
    store = store or CentralStore(cfg.central_db)
    interval = 24 * 3600

    def _loop() -> None:
        log.info("worker-location prune sweep started (retention=%dd, every %ds)",
                 cfg.field_track_retention_days, interval)
        while True:
            try:
                removed = prune_worker_locations(store, cfg)
                if removed:
                    log.info("worker-location prune: removed %d fix(es) older than %dd",
                             removed, cfg.field_track_retention_days)
            except Exception:
                log.exception("worker-location prune failed; will retry next tick")
            _time.sleep(interval)

    t = threading.Thread(target=_loop, name="wisp-central-field-prune", daemon=True)
    t.start()
    return t
