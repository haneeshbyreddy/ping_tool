"""Worker location tracking: the /field/track ingest, and the retention prune.

Workers run **Traccar Client** — free, open source, Android and iOS — rather than
anything of ours. Location was the only reason left to write native code (push
was declined; the WhatsApp bot already delivers an assignment with a working
[✅ I'm on it] button), and an off-the-shelf tracker brings years of Doze and
OEM-battery-manager tuning we would not match, plus iOS for free later.

Two consequences shape this whole module:

  * **ON-SHIFT ONLY, and the tracker's own ON/OFF switch is the real toggle.**
    When it is off the phone transmits nothing. We deliberately did NOT build
    "always transmit, discard off-shift server-side" — receiving a worker's
    evening and choosing not to store it is a much worse promise than not
    receiving it. The Start/End shift button in the web app is the SECOND,
    explicit declaration, and the two-tap cost buys the one thing no server code
    can produce: when somebody marks on-shift and no fixes arrive, that gap IS
    the "the OEM battery manager killed the service" alarm.
  * **A short trail on a 7-day clock**, not a movement archive. Long enough to
    answer "did he reach the site" and "what route did the van take today".

Traccar speaks the **OsmAnd protocol**: one HTTP request carrying `id, lat, lon,
timestamp, speed, bearing, altitude, accuracy, batt`. Client builds differ over
GET vs POST and over query-string vs form body, so the route accepts all four
combinations — a fix silently dropped because we only handled one verb is the
worst failure this feature has.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone

from wisp.config import CONFIG, Config
from wisp.egress.notifiers import _display_zone

log = logging.getLogger("wisp.central.field")

# The OsmAnd protocol reports speed in KNOTS — Traccar Client converts the
# platform's m/s before it sends, and the server-side decoder converts back. We
# store SI, so the conversion happens once, here, at ingest. Same discipline as
# the dbc profile's `distance_m`: a number whose unit nobody wrote down is how a
# reading ends up 39% wrong on every screen that shows it.
_KNOTS_TO_MPS = 0.514444


class TrackError(ValueError):
    """The request was malformed — not a fix we chose to drop. 400."""


class TrackDropped(ValueError):
    """A well-formed fix we deliberately did not store (too vague, too old).

    Deliberately NOT a 400. Traccar re-sends anything it did not get a 2xx for,
    and it sends in order, so refusing a fix we are never going to accept WEDGES
    the client's offline buffer behind it forever — the newer positions we
    actually want never arrive. The honest answer is "received, and here is why
    it isn't on the map": 200 with `stored: false` and this reason.
    """


def param(params: dict, *names: str) -> str:
    """First non-empty value among `names`, from a parse_qs-shaped dict.

    Several spellings per field because the builds differ: `lon`/`lng`,
    `bearing`/`heading`, `batt`/`battery`. Cheaper to accept them all than to
    diagnose one silent field months later.
    """
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
    # NaN/inf reach _reply and the trail geometry alike; drop them here rather
    # than let a device's junk become a coordinate.
    return f if f == f and abs(f) != float("inf") else None


def parse_ts(raw: str, now: datetime) -> datetime:
    """Traccar's `timestamp`, as an aware UTC datetime.

    Unix seconds is what the Android client sends; ISO8601 turns up from other
    OsmAnd-speaking clients, and both are cheap to accept. An ABSENT timestamp
    means now — a fix with no clock is still a position, and the request only
    just arrived.
    """
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
    # Some builds send milliseconds. 1e11 seconds is the year 5138 and 1e11 ms is
    # 1973, so the split is unambiguous for any date this software will see.
    if secs > 1e11:
        secs /= 1000.0
    try:
        return datetime.fromtimestamp(secs, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise TrackError("timestamp is out of range")


def clean_fix(params: dict, cfg: Config = CONFIG,
              now: datetime | None = None) -> dict:
    """One OsmAnd request as a storable fix, or a refusal.

    Every refusal here exists because a tracking table that accepts junk is worse
    than an empty one: a 2 km "fix" drawn as a pin says a worker is somewhere he
    demonstrably might not be, and a NOC screen that can be that wrong about a
    person is worse than one that says nothing.
    """
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
        # Offline buffering is a setting we RECOMMEND, so a morning replaying at
        # once is the feature working; past the retention window the fix would
        # land in a trail nobody will ever see.
        raise TrackDropped(f"fix is {int(age)}s old")
    if age < -cfg.field_track_max_skew_s:
        # A fix from ahead of now is a broken phone clock. It would sort to the
        # head of the trail and render as "here now", which is the one thing this
        # layer may not get wrong.
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
    """Per-token ceiling, as a token BUCKET rather than a minimum gap.

    A minimum gap would throw away exactly the burst we asked the client to keep:
    offline buffering means a crew coming out of a dead zone flushes an hour of
    fixes back-to-back, and those are the ones that answer "what route did the
    van take". A bucket passes that and still stops a looping client — at the
    designed 90 s cadence a phone spends ~0.7 of its per-minute allowance.

    In-process and unsynchronised across restarts, like `LoginThrottle`: this
    bounds a runaway client, it is not a security control.
    """

    def __init__(self, per_min: int = 60, burst_factor: float = 2.0) -> None:
        self.rate = max(per_min, 1) / 60.0          # tokens per second
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
    """Start of TODAY in the operator's own zone, as a stored-UTC timestamp.

    "Today" has to mean the operator's day, not UTC's — in IST a UTC-midnight
    boundary would cut the trail at 05:30 and drop the morning's driving off the
    screen while the van was still out. The zone comes from the same
    `WISP_DISPLAY_TZ` choke point WhatsApp times and the issue exports use; a
    second notion of local is how half a screen ends up 5h30m out from the other
    half.
    """
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_display_zone(cfg.display_tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def prune_worker_locations(store, cfg: Config = CONFIG,
                           now: datetime | None = None) -> int:
    """Drop fixes past the retention window. The window IS the policy."""
    end = now or datetime.now(timezone.utc)
    cutoff = (end - timedelta(days=cfg.field_track_retention_days)
              ).isoformat(timespec="seconds")
    return store.prune_worker_locations(cutoff)


def start_field_prune_thread(cfg: Config = CONFIG, store=None) -> threading.Thread:
    """Daily retention sweep, the shape `rollup.py` already uses.

    Do not ship the ingest without this thread: `data/releases/` is the standing
    example of a directory nothing prunes, and here the retention window is not a
    housekeeping detail — it is the whole answer to what this feature keeps about
    the people who work for the org.
    """
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
