"""Single source of the running build version.

The edge reports this in its heartbeat; central compares it to the rollout target and the
supervisor pulls only on a mismatch (Phase 10 Part D). Resolution order, most-authoritative
first, so one string ties commit → artifact → running version → rollout decision:

  1. `WISP_VERSION` env var (lets a frozen binary or a test pin it without editing code);
  2. a CI-generated `_buildinfo.py` next to this file (`VERSION = "..."` stamped from
     `git describe` at build time — git-ignored, absent in a dev checkout);
  3. the hand-maintained fallback below.
"""
from __future__ import annotations

import os

_FALLBACK = "0.10.0"


def _resolve() -> str:
    env = os.environ.get("WISP_VERSION")
    if env:
        return env
    try:
        from wisp import _buildinfo  # type: ignore
        return getattr(_buildinfo, "VERSION", _FALLBACK)
    except Exception:
        return _FALLBACK


VERSION = _resolve()


def platform_tag() -> str:
    """The artifact platform key central matches a release against, e.g. 'linux-amd64',
    'linux-arm64', 'win-amd64'. Coarse on purpose — it picks the right frozen binary."""
    import platform
    system = {"linux": "linux", "darwin": "darwin", "windows": "win"}.get(
        platform.system().lower(), platform.system().lower())
    mach = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
            "arm64": "arm64"}.get(mach, mach)
    return f"{system}-{arch}"
