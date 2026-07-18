from __future__ import annotations

from wisp.central.notify_policy import AlertRouter
from wisp.config import CONFIG, Config
from wisp.core.baseline import Sample, evaluate_perf
from wisp.core.state_machine import DOWN_FAMILY

def record_and_evaluate(store, org_id: str, eng, cycle, results: dict, ts: str,
                        notifier, cfg: Config = CONFIG) -> None:
    for dev_id, state in cycle.states.items():
        dev = eng.meta[dev_id]
        res = results.get(dev.ip_address)
        latency_ms = res.latency_ms if res else None
        packet_loss = res.packet_loss if res else 100.0
        jitter_ms = res.jitter_ms if res else None

        store.record_perf_sample(org_id, dev_id, ts, latency_ms, packet_loss,
                                 jitter_ms, state, cfg.perf_window)
        prior = store.device_perf_state(org_id, dev_id)
        was_degraded = bool(prior["degraded"]) if prior else False

        if state in DOWN_FAMILY:
            if was_degraded or prior is not None:
                store.write_device_perf(org_id, dev_id, False, None, None, None,
                                        None, ts)
            continue

        rows = store.perf_sample_window(org_id, dev_id)
        window = [Sample(r["latency_ms"], r["packet_loss"], r["jitter_ms"], r["state"])
                 for r in rows]
        v = evaluate_perf(window, cfg, was_degraded=was_degraded)
        since = (ts if (v.degraded and v.changed)
                else (prior["since"] if (prior and v.degraded) else None))
        store.write_device_perf(org_id, dev_id, v.degraded, v.metric, v.baseline_ms,
                                v.current_ms, since, ts)

        if not v.changed:
            continue
        if v.degraded:
            title, body, payload = (f"\U0001f40c Slow link — {dev.name} ({dev.region})",
                                    v.reason, "PERF_DEGRADED")
        else:
            title, body, payload = (f"✅ Recovered — {dev.name} ({dev.region})",
                                    "Link performance back to baseline", "PERF_RECOVERED")
        AlertRouter(store, org_id, notifier, cfg).emit(
            payload, topic=store.org_role_topic(org_id, "operator"),
            title=title, body=body, priority=3, ts=ts, device_id=dev_id,
            gate=cfg.perf_alerts)
