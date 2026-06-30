"""Single source of the running build version.

The edge reports this string in its heartbeat; central compares it to the target it
is rolling out (Phase 10 Part D). For now it is a hand-maintained semver — Part D's
CI stamps it from `git describe` at build time so every artifact maps to one commit.
Keep it importable with zero dependencies (it is read on the hot heartbeat path).
"""
from __future__ import annotations

VERSION = "0.10.0"
