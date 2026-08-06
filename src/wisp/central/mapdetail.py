"""Server-wide map detail — the zoom at which each map layer starts drawing.

Sibling of `theme.py`, and here for the same reason: these were hardcoded
constants that got hand-tuned every time somebody said the map was too busy or
too empty, which is a dashboard control wearing a code edit's clothes. Stored
under `app_settings.map_detail`, set by the SUPERADMIN in Settings → Platform,
and applied to every org.

Deliberately ONE configuration for the whole install, not a per-browser or
per-user preference (operator's call, 2026-08-02 — it shipped per-browser first
and was pulled back the same day). The reasoning is worth keeping: map density
is a judgement about how this product should read, and the person who makes it
is the one who looks at the fleet all day. Handing it to every account produces
a support surface ("my map looks different from yours") in exchange for a
choice nobody else asked to make.

Two shape decisions:

* **It rides the `/api/orgs` reply, exactly like `google_maps_key`.** The map
  already reads that row for the key, so applying a server-wide map setting
  costs no extra fetch and lands on the same cache invalidation. It is NOT org
  data and is not stored per org — every row carries the same numbers.

* **Nothing outside display reads it.** No alarm, count, verdict or page. That
  is what makes it safe to hand to a form: the worst a bad value can do is draw
  a layer sooner or later than someone wanted. Device pin DOTS, their status
  tone and the down-pulse ignore `labels` entirely, and anything down, degraded
  or selected keeps its label at every zoom, so even the extreme setting cannot
  hide a device that is down. `passives` keeps the same promise by the same
  means: a splitter whose recorded subscribers are dark — and the chain of
  plant above it, since a branch fault names the SPAN between two pins — is
  exempt from its floor, so this row can hide reference material and never an
  alarm.

Validation never raises. A malformed field falls back to the shipped default
rather than 422-ing the save, because the alternative — rejecting a whole
settings POST over one bad number — is worse, and because a NaN/None reaching
the SPA would make every `zoom >= n` false and read as "the layer is broken".
"""
from __future__ import annotations

import json

SETTING_KEY = "map_detail"

# Mirrors DETAIL_DEFAULTS in web/src/map/detail.ts, which carries the reasoning
# behind each number. Kept in both places on purpose: the SPA needs a default to
# render before the orgs query resolves, and central needs one to validate
# against without asking the browser.
DEFAULTS: dict[str, int] = {
    "labels": 12,             # device NAME labels on pins (dots always draw)
    "passives": 13,           # splitters/FDBs/closures + the cable into them
    "subscribers": 14,        # located-subscriber marks
    "subscriber_names": 17,   # the customer name beside a located subscriber
    "drop_lines": 16,         # the dotted line to a splitter + its rate chip
}

# Google's tiles stop at 20 and the region lock sets its own floor, so the
# accepted span is deliberately narrower than what Leaflet would take: past
# either end the setting would be pretending to do something. A floor at or
# below the map's own minimum simply reads as "always on".
MIN_ZOOM = 4
MAX_ZOOM = 19

FIELDS = tuple(DEFAULTS)


def _one(raw, key: str) -> int:
    """One field, coerced and clamped, falling back to the shipped default.

    PER FIELD, not per object: a row written before a field existed must not
    discard the fields it does carry, and `True`/`"14"`/`None` must degrade to
    something renderable rather than propagate."""
    value = raw.get(key) if isinstance(raw, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULTS[key]
    try:
        n = int(round(value))
    except (ValueError, OverflowError):  # inf/nan
        return DEFAULTS[key]
    return max(MIN_ZOOM, min(MAX_ZOOM, n))


def clean(raw) -> dict[str, int]:
    """Coerce a posted payload into `{field: zoom}`.

    Enforces the ordering invariant: a line or a label may not be drawn at a
    zoom where the MARK it belongs to isn't. It is not cosmetic. The SPA gates
    each of these on the mark being drawn, so a floor set below its mark's
    doesn't draw it earlier, it does nothing at all — and a setting that
    silently no-ops is worse than one that refuses.

    * `subscriber_names` >= `subscribers`: a name rides the mark it labels.
    * `drop_lines` >= `subscribers` AND >= `passives`: a drop line has TWO
      ends, the subscriber's diamond and the splitter feeding it, and a dotted
      line running to a point where nothing is drawn reads as a rendering
      fault. This is why `passives` is in the invariant at all.

    `subscriber_names` and `drop_lines` share a floor but are INDEPENDENT of
    each other — a name rides the mark, a rate chip rides the line — so naming
    subscribers without drawing their drop lines stays a legitimate setting.

    Repairing it HERE rather than in the SPA means a value hand-written into
    SQLite can't reach the map in that state either."""
    out = {k: _one(raw, k) for k in FIELDS}
    out["subscriber_names"] = max(out["subscriber_names"], out["subscribers"])
    out["drop_lines"] = max(out["drop_lines"], out["subscribers"], out["passives"])
    return out


def is_default(detail: dict[str, int]) -> bool:
    return all(detail.get(k) == v for k, v in DEFAULTS.items())


def load(store) -> dict[str, int]:
    """The EFFECTIVE values — defaults filled in, always every field.

    Unlike `theme.load`, which hands back a sparse diff for the SPA to merge,
    this returns concrete numbers: the consumers are a settings form and a map
    that has to draw something. Re-validated on the way out so a row written
    by an older build, or edited straight in SQLite, still can't reach the page
    unchecked."""
    raw = store.get_setting(SETTING_KEY)
    if not raw:
        return dict(DEFAULTS)
    try:
        return clean(json.loads(raw))
    except (ValueError, TypeError):
        return dict(DEFAULTS)


def save(store, raw) -> dict[str, int]:
    """Validate and persist. Values equal to the shipped defaults CLEAR the row
    rather than storing a copy of them — so an install nobody has touched keeps
    following the defaults, and a later change to them still reaches everyone
    who never expressed an opinion. Same sparse-storage argument as
    `theme.save`, and the same reason Reset leaves no trace."""
    cleaned = clean(raw)
    store.set_setting(
        SETTING_KEY, None if is_default(cleaned) else json.dumps(cleaned))
    return cleaned
