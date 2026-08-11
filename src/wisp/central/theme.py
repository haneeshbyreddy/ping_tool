from __future__ import annotations

import json
import re

SETTING_KEY = "theme_overrides"

_TOKENS = frozenset({
    "--accent", "--accent-foreground", "--background", "--border",
    "--border-subtle", "--card", "--card-foreground", "--chart-1", "--chart-2",
    "--chart-3", "--chart-4", "--chart-5", "--destructive",
    "--destructive-foreground", "--destructive-soft", "--faint-foreground",
    "--foreground", "--ghost-foreground", "--input", "--map-link", "--muted",
    "--muted-foreground", "--popover", "--popover-foreground", "--primary",
    "--primary-foreground", "--primary-soft", "--ring", "--secondary",
    "--secondary-foreground", "--sidebar", "--sidebar-accent",
    "--sidebar-accent-foreground", "--sidebar-border", "--sidebar-foreground",
    "--sidebar-primary", "--sidebar-primary-foreground", "--sidebar-ring",
    "--success", "--success-foreground", "--success-soft", "--warning",
    "--warning-foreground", "--warning-soft",
})

_VALUE_RE = re.compile(
    r"^(#[0-9a-fA-F]{3,8}"
    r"|(rgb|rgba|hsl|hsla|oklch|oklab|lab|lch)\([0-9a-zA-Z.,%/\s+-]{1,64}\)"
    r"|[a-zA-Z]{3,24})$"
)

_MODES = ("dark", "light")
_MAX_TOKENS_PER_MODE = 64


def clean_overrides(raw) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    for mode in _MODES:
        tokens = raw.get(mode)
        if not isinstance(tokens, dict):
            continue
        clean: dict[str, str] = {}
        for token, value in tokens.items():
            if len(clean) >= _MAX_TOKENS_PER_MODE:
                break
            if token not in _TOKENS or not isinstance(value, str):
                continue
            value = value.strip()
            if _VALUE_RE.match(value):
                clean[token] = value
        if clean:
            out[mode] = clean
    return out


def load(store) -> dict[str, dict[str, str]]:
    raw = store.get_setting(SETTING_KEY)
    if not raw:
        return {}
    try:
        return clean_overrides(json.loads(raw))
    except (ValueError, TypeError):
        return {}


def save(store, raw) -> dict[str, dict[str, str]]:
    cleaned = clean_overrides(raw)
    store.set_setting(SETTING_KEY, json.dumps(cleaned) if cleaned else None)
    return cleaned


def render_css(overrides: dict[str, dict[str, str]]) -> str:


    blocks = []
    for mode in _MODES:
        tokens = overrides.get(mode) or {}
        if not tokens:
            continue
        body = "".join(f"{t}:{v};" for t, v in sorted(tokens.items()))
        blocks.append((":root.dark" if mode == "dark" else ":root:not(.dark)")
                      + "{" + body + "}")
    return "".join(blocks)
