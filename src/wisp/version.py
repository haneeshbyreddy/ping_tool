from __future__ import annotations

import os
import re

_FALLBACK = "0.13.0"

def _resolve() -> str:
    env = os.environ.get("WISP_VERSION")
    if env:
        return env
    try:
        from wisp import _buildinfo
        return getattr(_buildinfo, "VERSION", _FALLBACK)
    except Exception:
        return _FALLBACK

VERSION = _resolve()

def platform_tag() -> str:
    import platform
    system = {"linux": "linux", "darwin": "darwin", "windows": "win"}.get(
        platform.system().lower(), platform.system().lower())
    mach = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
            "arm64": "arm64"}.get(mach, mach)
    return f"{system}-{arch}"

def version_tuple(v: str | None) -> tuple[int, int, int]:
    if not v:
        return (0, 0, 0)
    out: list[int] = []
    for part in re.split(r"[.\-+]", v.strip().lstrip("vV"))[:3]:
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    while len(out) < 3:
        out.append(0)
    return (out[0], out[1], out[2])

def is_newer(candidate: str | None, current: str | None) -> bool:
    return version_tuple(candidate) > version_tuple(current)
