"""Server-wide theme overrides — the colour layer the superadmin controls.

The shipped palette lives in `web/src/index.css` and stays the default and the
design record. This module stores a thin OVERRIDE map on top of it
(`app_settings.theme_overrides`) and renders it into a `<style>` block that
`server.py:_serve_static` injects into the SPA's `<head>`.

Three things about the shape here are deliberate:

* **Injection, not an API call.** The colours have to be on the page BEFORE
  first paint or every load flashes the default palette and then repaints —
  worst on the login and billing-lock screens, which is exactly where a
  white-flash on a dark theme is most jarring. An injected style block also
  needs no session, so those pre-auth screens are themed too.

* **Storage is a SPARSE DIFF, never a full snapshot.** A token the operator
  never touched is simply absent, so it keeps following index.css. Writing a
  complete palette on first save would freeze each deployment on whatever the
  shipped colours happened to be that day, and later design work would silently
  stop reaching anyone who had ever opened the colour picker.

* **The allowlist is a security boundary, not tidiness.** These values are
  interpolated into a stylesheet. `_TOKENS` and `_VALUE_RE` together mean the
  only thing that can reach the page is a known custom property set to a
  literal colour — no arbitrary declarations, no `}` to break out of the rule,
  no `url()` to make the browser fetch anything. Widen either one carefully.

The colour MATH (deriving a surface ladder or a readable ink from a seed) is
deliberately not here — it lives in `web/src/lib/theme-tokens.ts`, runs in the
browser, and posts its result. Central stays pure-stdlib and does no colour
science; it validates and stores what it is given.
"""
from __future__ import annotations

import json
import re

SETTING_KEY = "theme_overrides"

# Mirrors ALL_TOKENS in web/src/lib/theme-tokens.ts. The two lists are pinned
# together by tests/unit/test_theme.py:test_allowlist_matches_spa, which reads
# the TS source — a token added on one side only would otherwise be silently
# dropped on save, which looks exactly like "the colour picker is broken".
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

# hex, rgb()/rgba(), hsl()/hsla(), oklch()/oklab(), or a bare colour keyword.
# Deliberately excludes anything with a URL, a semicolon or a brace.
_VALUE_RE = re.compile(
    r"^(#[0-9a-fA-F]{3,8}"
    r"|(rgb|rgba|hsl|hsla|oklch|oklab|lab|lch)\([0-9a-zA-Z.,%/\s+-]{1,64}\)"
    r"|[a-zA-Z]{3,24})$"
)

_MODES = ("dark", "light")
# One mode can hold at most every token; the cap is a belt-and-braces bound on
# how much CSS a single bad POST can push onto every page load.
_MAX_TOKENS_PER_MODE = 64


def clean_overrides(raw) -> dict[str, dict[str, str]]:
    """Coerce a posted payload into `{mode: {token: value}}`, dropping anything
    unrecognised. Never raises: a malformed entry is not worth 422-ing a whole
    palette save over, and silently ignoring one bad token is far better than
    rejecting the other forty."""
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
    """Read the stored overrides. Re-validates on the way out so a row written
    by an older/looser build (or edited straight in SQLite) still can't reach
    the page unchecked."""
    raw = store.get_setting(SETTING_KEY)
    if not raw:
        return {}
    try:
        return clean_overrides(json.loads(raw))
    except (ValueError, TypeError):
        return {}


def save(store, raw) -> dict[str, dict[str, str]]:
    """Validate and persist. An empty result CLEARS the row rather than storing
    `{}` — resetting to the shipped palette should leave no trace behind."""
    cleaned = clean_overrides(raw)
    store.set_setting(SETTING_KEY, json.dumps(cleaned) if cleaned else None)
    return cleaned


def render_css(overrides: dict[str, dict[str, str]]) -> str:
    """Render `{mode: {token: value}}` as CSS. Empty in, empty out — a stock
    install injects nothing at all.

    THE SELECTORS ARE MUTUALLY EXCLUSIVE ON PURPOSE, and the naive pair
    (`:root` for light, `.dark` for dark, mirroring index.css) is WRONG here —
    it shipped and blew out dark mode. `:root` and `.dark` have IDENTICAL
    specificity (0,1,0), and this block is injected after the bundle's
    stylesheet, so a plain `:root{}` of light-mode overrides beats the bundle's
    `.dark{}` on source order and applies IN DARK MODE. The result is the
    operator's light surfaces under dark mode's light text — an unreadable
    white screen — and it only bites tokens they actually customised, so the
    palette looks half-applied rather than obviously broken.

    `:root:not(.dark)` / `:root.dark` fixes both halves at once: (0,2,0) each,
    so they outrank the bundle's `:root`/`.dark` no matter where this lands in
    the document, and they can never both match, so neither mode's overrides
    can leak into the other. index.css can keep its simpler pair because it
    controls its own source order; an injected layer cannot assume that.
    """
    blocks = []
    for mode in _MODES:
        tokens = overrides.get(mode) or {}
        if not tokens:
            continue
        body = "".join(f"{t}:{v};" for t, v in sorted(tokens.items()))
        blocks.append((":root.dark" if mode == "dark" else ":root:not(.dark)")
                      + "{" + body + "}")
    return "".join(blocks)
