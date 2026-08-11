from __future__ import annotations

import json

SETTING_KEY = "map_detail"

DEFAULTS: dict[str, int] = {
    "labels": 12,
    "passives": 13,
    "subscribers": 14,
    "subscriber_names": 17,
    "drop_lines": 16,
}

MIN_ZOOM = 4
MAX_ZOOM = 19

FIELDS = tuple(DEFAULTS)


def _one(raw, key: str) -> int:

    value = raw.get(key) if isinstance(raw, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULTS[key]
    try:
        n = int(round(value))
    except (ValueError, OverflowError):
        return DEFAULTS[key]
    return max(MIN_ZOOM, min(MAX_ZOOM, n))


def clean(raw) -> dict[str, int]:


    out = {k: _one(raw, k) for k in FIELDS}
    out["subscriber_names"] = max(out["subscriber_names"], out["subscribers"])
    out["drop_lines"] = max(out["drop_lines"], out["subscribers"], out["passives"])
    return out


def is_default(detail: dict[str, int]) -> bool:
    return all(detail.get(k) == v for k, v in DEFAULTS.items())


def load(store) -> dict[str, int]:

    raw = store.get_setting(SETTING_KEY)
    if not raw:
        return dict(DEFAULTS)
    try:
        return clean(json.loads(raw))
    except (ValueError, TypeError):
        return dict(DEFAULTS)


def save(store, raw) -> dict[str, int]:
    cleaned = clean(raw)
    store.set_setting(
        SETTING_KEY, None if is_default(cleaned) else json.dumps(cleaned))
    return cleaned
