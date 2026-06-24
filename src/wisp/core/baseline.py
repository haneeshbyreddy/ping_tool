"""Layer 2 — per-link performance baseline detection (pure).

The FSM (`state_machine.py`) calls a link UP/DEGRADED against ABSOLUTE thresholds
(latency > `latency_threshold_ms`, loss > `loss_degraded_pct`). That misses the most
common WISP failure mode: a link that normally runs 8ms now sitting at 90ms with heavy
jitter — still well under 150ms, so the FSM says UP, while everyone behind it suffers.

`evaluate_perf` watches each link against ITS OWN rolling baseline (median + MAD of its
recent healthy samples) and flags a *sustained* deviation. Like `MonitorEngine` it is
deliberately **pure**: it takes the trailing sample window plus the prior perf state and
returns a verdict — no DB. The glue (history query, perf-state persistence, the operator
push) lives in the daemon, so this stays unit-testable.

Robust stats on purpose: median + MAD shrug off the odd spike that mean/stddev would
smear, so a single bad poll never trips the flag — and the consecutive-window
requirement adds hysteresis on top, symmetric on the way in and the way out.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from wisp.config import CONFIG, Config
from wisp.core.state_machine import UP


@dataclass(frozen=True)
class Sample:
    """One poll's measurements for a device (a `poll_results` row, essentially)."""
    latency_ms: float | None
    loss_pct: float
    jitter_ms: float | None
    state: str


@dataclass(frozen=True)
class PerfVerdict:
    degraded: bool          # the link's perf state AFTER this evaluation
    changed: bool           # did it differ from `was_degraded` (i.e. an edge)?
    metric: str | None      # 'latency' | 'jitter' | None — which metric tripped
    baseline_ms: float | None  # baseline value of the tripping metric
    current_ms: float | None   # current value of the tripping metric
    reason: str             # human one-liner for the alert / dashboard


def _mad(values: list[float], med: float) -> float:
    """Median absolute deviation — a spike-resistant spread estimate."""
    return median([abs(v - med) for v in values])


def _deviates(current: float, med: float, mad: float, floor: float,
              factor: float, k: float) -> bool:
    if med < floor:
        return False                       # baseline too small to judge a multiple
    return current > med * factor and current > med + k * mad


def evaluate_perf(window: list[Sample], cfg: Config = CONFIG, *,
                  was_degraded: bool) -> PerfVerdict:
    """Judge the newest sample against the link's own baseline.

    `window` is the trailing samples oldest→newest; the last `perf_consecutive`
    are the candidate window, everything before them forms the baseline (so a
    developing degradation never pulls up the baseline it's measured against).

    Hysteresis is symmetric: enter PERF_DEGRADED only when *every* sample in the
    candidate window deviates, leave it only when *none* of them do; otherwise hold
    `was_degraded`. On thin data we never *enter* degraded (but we do hold an
    existing flag, so a restart mid-degradation doesn't flap).
    """
    n = cfg.perf_consecutive
    hold = PerfVerdict(was_degraded, False, None, None, None,
                       "insufficient data; holding" if was_degraded else "within baseline")

    if len(window) < n + cfg.perf_min_samples:
        return hold

    recent = window[-n:]
    pool = [s for s in window[:-n] if s.state == UP]
    lat = [s.latency_ms for s in pool if s.latency_ms is not None]
    if len(lat) < cfg.perf_min_samples:
        return hold

    lat_med = median(lat)
    lat_mad = _mad(lat, lat_med)
    jit = [s.jitter_ms for s in pool if s.jitter_ms is not None]
    jit_med = median(jit) if len(jit) >= cfg.perf_min_samples else None
    jit_mad = _mad(jit, jit_med) if jit_med is not None else 0.0

    def tripped(s: Sample) -> str | None:
        if s.latency_ms is not None and _deviates(
                s.latency_ms, lat_med, lat_mad, cfg.perf_min_baseline_ms,
                cfg.perf_deviation_factor, cfg.perf_mad_k):
            return "latency"
        if (jit_med is not None and s.jitter_ms is not None and _deviates(
                s.jitter_ms, jit_med, jit_mad, cfg.perf_min_jitter_ms,
                cfg.perf_deviation_factor, cfg.perf_mad_k)):
            return "jitter"
        return None

    metrics = [tripped(s) for s in recent]
    cur = recent[-1]

    def degraded(metric: str, changed: bool) -> PerfVerdict:
        if metric == "jitter":
            base, val = jit_med, cur.jitter_ms
            reason = f"jitter {val:.0f}ms vs ~{base:.0f}ms baseline"
        else:
            base, val = lat_med, cur.latency_ms
            reason = f"latency {val:.0f}ms vs ~{base:.0f}ms baseline"
        return PerfVerdict(True, changed, metric, base, val, reason)

    if not was_degraded:
        if all(m is not None for m in metrics):          # sustained deviation → enter
            return degraded(metrics[-1] or "latency", changed=True)
        return PerfVerdict(False, False, None, None, None, "within baseline")

    # currently degraded: recover only when the whole window is back within baseline
    if all(m is None for m in metrics):
        return PerfVerdict(False, True, None, lat_med, cur.latency_ms, "recovered to baseline")
    return degraded(next((m for m in reversed(metrics) if m), None) or "latency", changed=False)
