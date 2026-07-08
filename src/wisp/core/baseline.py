from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from wisp.config import CONFIG, Config
from wisp.core.state_machine import UP

@dataclass(frozen=True)
class Sample:
    latency_ms: float | None
    loss_pct: float
    jitter_ms: float | None
    state: str

@dataclass(frozen=True)
class PerfVerdict:
    degraded: bool
    changed: bool
    metric: str | None
    baseline_ms: float | None
    current_ms: float | None
    reason: str

def _mad(values: list[float], med: float) -> float:
    return median([abs(v - med) for v in values])

def _deviates(current: float, med: float, mad: float, floor: float,
              factor: float, k: float) -> bool:
    if med < floor:
        return False
    return current > med * factor and current > med + k * mad

def evaluate_perf(window: list[Sample], cfg: Config = CONFIG, *,
                  was_degraded: bool) -> PerfVerdict:
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
        if all(m is not None for m in metrics):
            return degraded(metrics[-1] or "latency", changed=True)
        return PerfVerdict(False, False, None, None, None, "within baseline")

    if all(m is None for m in metrics):
        return PerfVerdict(False, True, None, lat_med, cur.latency_ms, "recovered to baseline")
    return degraded(next((m for m in reversed(metrics) if m), None) or "latency", changed=False)
