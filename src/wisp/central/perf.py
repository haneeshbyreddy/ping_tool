"""Central-side per-link performance baseline (CLAUDE.md item 3).

Reuses `core/baseline.py`'s pure median+MAD deviation math (`evaluate_perf`) verbatim —
identical to the old single-box edge's soft "slow link" signal: a link running well
under the FSM's absolute thresholds can still be sitting far above ITS OWN normal
latency/jitter, and that's worth an operator heads-up before it degrades into an
outage. None of that math needed to change; central's job is just the trailing-sample
window (`CentralStore.device_perf_samples`, a bounded per-device ring buffer — see its
schema docstring for why this is deliberately NOT the same storage as
`central/rollup.py`'s hourly trend buckets) and the badge (`device_perf`).

Not part of the outage/escalation ladder — a degraded-but-still-UP link is NOT louder.
A hard-DOWN device's perf state is moot (the outage owns that story) and its badge
clears silently. Operator-only page, gated by `cfg.perf_alerts`; the badge is always
written.
"""
from __future__ import annotations

from wisp.config import CONFIG, Config
from wisp.core.baseline import Sample, evaluate_perf
from wisp.core.state_machine import DOWN_FAMILY


def record_and_evaluate(store, tenant_id: str, eng, cycle, results: dict, ts: str,
                        notifier, cfg: Config = CONFIG) -> None:
    """One full-report cycle: append this cycle's sample to each device's trailing
    window, judge it against `evaluate_perf`, persist the badge, and page the operator
    on an enter/leave edge. Called from `central/server.py:_report` right alongside the
    SNMP port fold and the trend rollup — same "full reports only" gating (a recheck's
    rapid re-probe of a suspect subset is not a meaningful perf sample)."""
    for dev_id, state in cycle.states.items():
        dev = eng.meta[dev_id]
        res = results.get(dev.ip_address)
        latency_ms = res.latency_ms if res else None
        packet_loss = res.packet_loss if res else 100.0
        jitter_ms = res.jitter_ms if res else None

        store.record_perf_sample(tenant_id, dev_id, ts, latency_ms, packet_loss,
                                 jitter_ms, state, cfg.perf_window)
        prior = store.device_perf_state(tenant_id, dev_id)
        was_degraded = bool(prior["degraded"]) if prior else False

        if state in DOWN_FAMILY:
            # A hard-down link's perf is moot — clear the badge silently if it was set.
            if was_degraded or prior is not None:
                store.write_device_perf(tenant_id, dev_id, False, None, None, None,
                                        None, ts)
            continue

        rows = store.perf_sample_window(tenant_id, dev_id)
        window = [Sample(r["latency_ms"], r["packet_loss"], r["jitter_ms"], r["state"])
                 for r in rows]
        v = evaluate_perf(window, cfg, was_degraded=was_degraded)
        since = (ts if (v.degraded and v.changed)
                else (prior["since"] if (prior and v.degraded) else None))
        store.write_device_perf(tenant_id, dev_id, v.degraded, v.metric, v.baseline_ms,
                                v.current_ms, since, ts)

        if not v.changed:
            continue
        topic = store.org_role_topic(tenant_id, "operator")
        if v.degraded:
            title, body, payload = (f"\U0001f40c Slow link — {dev.name} ({dev.region})",
                                    v.reason, "PERF_DEGRADED")
        else:
            title, body, payload = (f"✅ Recovered — {dev.name} ({dev.region})",
                                    "Link performance back to baseline", "PERF_RECOVERED")
        if cfg.perf_alerts and topic:
            res_notify = notifier.send(topic, title, body, 3)
            status = "sent" if res_notify.ok else "failed"
        else:
            status = "suppressed"
        store.log_alert(tenant_id, None, dev_id, notifier.channel, topic, status,
                        payload, ts)
