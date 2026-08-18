from __future__ import annotations

import logging

from wisp.central import engine as central_engine
from wisp.central import notify_policy
from wisp.central import perf as central_perf
from wisp.central import redundancy as central_redundancy
from wisp.central import rollup as central_rollup
from wisp.central import weboptics
from wisp.central.api import liveping as from_liveping
from wisp.central.api.common import now_iso
from wisp.central.dispatch import CentralAlertDispatcher
from wisp.central.optics import CentralOpticsMonitor
from wisp.central.onualert import OnuRosterAlerter
from wisp.central.ponalert import PonFaultAlerter
from wisp.central.ports import CentralPortMonitor
from wisp.ingress.probers import PingResult

log = logging.getLogger("wisp.central")


def devices(h, qs):
    org = (qs.get("org_id") or [None])[0]
    if not org:
        h._reply(400, {"error": "org_id required"})
        return
    if not h._ingest_ok(org):
        h._reply(401, {"error": "unauthorized"})
        return
    # A DEACTIVATED org is stood down: an empty topology, so the edge probes
    # nothing and pages nobody. This is the ONE place billing touches the
    # monitoring path, and it is safe only because deactivation can never
    # happen automatically — it takes a superadmin typing the org's id into a
    # confirm dialog, on an account that is already 60+ days overdue and has
    # stopped being a customer. The overdue LADDER itself never reaches here:
    # a locked org is still fully monitored, which is the invariant that
    # matters ("a lapsed bill must not silence an alarm").
    # The node keeps heartbeating and keeps posting (empty) reports, so it
    # stays live, stays updatable, and never trips the NODE_STALE watchdog.
    if h.store.org_billing(org)["deactivated"]:
        h._reply(200, {"devices": [], "canary_ip": h.cfg.canary_ip,
                       "snmp_profiles": [], "gpon_profiles": [],
                       "poll_interval_s": h.store.org_poll_interval(org)})
        return
    devs = h.store.org_device_topology(org)
    node = (qs.get("node_id") or [None])[0]
    if node:
        devs = [d for d in devs if d.get("assigned_node_id") == node]
    else:
        devs = [d for d in devs if d.get("assigned_node_id")]
    h._reply(200, {"devices": devs, "canary_ip": h.cfg.canary_ip,
                   "snmp_profiles": h.store.snmp_profiles_for_edge(org),
                   "gpon_profiles": h.store.gpon_profiles_for_edge(org),
                   "poll_interval_s": h.store.org_poll_interval(org)})


def heartbeat_reply(h, org: str, node: str, body: dict) -> dict:
    reply: dict = {"ok": True}
    try:
        from wisp.central import rollout
        rollout.maybe_auto_rollout(h.store, org, node, body.get("version"))
        rollout.evaluate(h.store, org, cfg=h.cfg)
        directive = rollout.directive_for(h.store, org, node, body.get("version"),
                                          body.get("platform"))
        if directive:
            reply["update"] = directive
    except Exception:
        log.exception("rollout directive failed for %s/%s", org, node)
    try:
        if h.store.pop_restart_request(org, node):
            reply["restart"] = True
    except Exception:
        log.exception("restart directive failed for %s/%s", org, node)
    return reply


def walk_result(h, org: str, node: str, env: dict) -> None:
    from wisp.central import inventory
    try:
        walk_id = int(env.get("walk_id"))
    except (TypeError, ValueError):
        h._reply(400, {"error": "walk_id required"})
        return
    error = env.get("error")
    error = str(error)[:500] if error else None
    varbinds = None
    if error is None:
        raw = env.get("varbinds")
        if not isinstance(raw, list):
            h._reply(400, {"error": "varbinds must be a list"})
            return
        varbinds = []
        for pair in raw[:inventory.WALK_CAP_MAX_VARBINDS]:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            varbinds.append([str(pair[0])[:256], str(pair[1])[:1024]])
    ok = h.store.complete_snmp_walk(org, node, walk_id,
                                    varbinds=varbinds, error=error,
                                    truncated=bool(env.get("truncated")))
    h._reply(200 if ok else 404, {"ok": ok})


def report(h, org: str, env: dict) -> dict:
    ts = env.get("ts") or now_iso()
    pings = env.get("pings") or {}
    results = {
        ip: PingResult(ip, v.get("latency_ms"),
                       float(v.get("loss_pct", 100.0)), v.get("jitter_ms"))
        for ip, v in pings.items()
    }
    h.store.touch_node(org, env.get("node_id", ""))
    mode = env.get("mode") or "full"
    with h.registry.org_lock(org):
        eng = h.registry.get(org)
        if mode == "recheck":
            ip_to_id = {d.ip_address: d.id for d in eng.meta.values()}
            subset = {ip_to_id[ip] for ip in results if ip in ip_to_id}
            cycle = central_engine.run_cycle(h.store, org, eng, results, ts,
                                             subset=subset)
        else:
            expected = h.store.node_expected_ips(org, env.get("node_id", ""))
            cycle = central_engine.run_cycle(h.store, org, eng, results, ts,
                                             expected_ips=expected)

    disp = CentralAlertDispatcher(h.store, org, eng, h.notifier, h.cfg)
    disp.dispatch(cycle.events, ts)
    if mode != "recheck":
        disp.sweep(ts)
        _ingest_ports(h, org, eng, env.get("ports"), ts)
        _ingest_optics(h, org, eng, env.get("optics"), ts)
        _ingest_health(h, org, eng, env.get("health"), ts)
        _ingest_snmp_status(h, org, eng, env.get("snmp_status"), ts)
        central_rollup.record_cycle(h.store, org, eng, cycle, results, ts)
        central_perf.record_and_evaluate(h.store, org, eng, cycle, results, ts,
                                         h.notifier, h.cfg)
        central_redundancy.sweep(h.store, org, eng, cycle.redundancy,
                                 cycle.states, h.notifier, ts, h.cfg)
        try:
            notify_policy.flush_digests(h.store, org, h.notifier, h.cfg, ts)
        except Exception:
            log.exception("digest flush failed for %s", org)

    reply: dict = {"ok": True}
    recheck = central_engine.compute_recheck(eng, cycle, results, h.cfg)
    if recheck:
        reply["recheck"] = recheck
    if mode != "recheck":
        walks = h.store.pending_snmp_walks(org, env.get("node_id", ""))
        if walks:
            reply["snmp_walks"] = walks
        if h.cfg.proxy_enabled:
            psessions = h.proxy.active_sessions_for(org, env.get("node_id", ""))
            if psessions:
                reply["proxy_sessions"] = psessions
            if h.store.org_web_proxy(org):
                reply["proxy_standby"] = True
        # The live-ping WAKE-UP, and the only thing live ping does to this
        # path. The probe dials central and central can never dial it, so a
        # session started while the channel is dormant is picked up on this
        # reply — which is why the dashboard prints the poll cadence as a wait
        # rather than spinning. One boolean, composed in `api/liveping`, going
        # central -> edge only: nothing a live session measures is readable
        # from here, and nothing here reaches the FSM.
        if from_liveping.wake_flag(h, org, env.get("node_id", "")):
            reply["liveping"] = True
    return reply


def _ingest_ports(h, org: str, eng, ports_by_device, ts: str) -> None:
    if not ports_by_device:
        return
    monitor = CentralPortMonitor(h.store, org, h.notifier, h.cfg)
    for raw_id, ports in ports_by_device.items():
        try:
            device_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if device_id not in eng.meta or not isinstance(ports, list):
            continue
        try:
            monitor.sync_device(device_id, ports, ts)
        except Exception:
            log.exception("SNMP port fold failed for %s/device=%d", org, device_id)


def _ingest_optics(h, org: str, eng, optics_by_device, ts: str) -> None:
    if not optics_by_device:
        return
    monitor = CentralOpticsMonitor(h.store, org, h.notifier, h.cfg)
    for raw_id, onus in optics_by_device.items():
        try:
            device_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if device_id not in eng.meta or not isinstance(onus, list):
            continue
        try:
            onus = _merge_web_optics(h, org, device_id, onus, ts)
            monitor.sync_device(device_id, onus, ts)
        except Exception:
            log.exception("GPON optics fold failed for %s/device=%d", org, device_id)
    try:
        PonFaultAlerter(h.store, org, h.notifier, h.cfg).sweep(ts)
    except Exception:
        log.exception("PON fault sweep failed for %s", org)
    try:
        OnuRosterAlerter(h.store, org, h.notifier, h.cfg).sweep(ts)
    except Exception:
        log.exception("ONU roster sweep failed for %s", org)


def _merge_web_optics(h, org: str, device_id: int, onus: list, ts: str) -> list:
    if not getattr(h.cfg, "web_optics_enabled", False):
        return onus
    try:
        rows = h.store.list_web_optics(org, device_id)
        if not rows:
            return onus
        merged, count = weboptics.merge_scraped(
            onus, rows, now_iso(), h.cfg.web_optics_max_age_s)
        if count:
            log.debug("web optics: merged %d reading(s) into %s/device=%d",
                      count, org, device_id)
        return merged
    except Exception:
        log.exception("web optics merge failed for %s/device=%d", org, device_id)
        return onus


def _ingest_health(h, org: str, eng, health_by_device, ts: str) -> None:
    if not health_by_device:
        return
    for raw_id, health in health_by_device.items():
        try:
            device_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if device_id not in eng.meta or not isinstance(health, dict):
            continue
        try:
            h.store.upsert_device_health(org, device_id, health, ts)
        except Exception:
            log.exception("SNMP health fold failed for %s/device=%d", org, device_id)


def _ingest_snmp_status(h, org: str, eng, status_by_device, ts: str) -> None:
    if not isinstance(status_by_device, dict):
        return
    rows: list[tuple[int, str, dict]] = []
    for raw_id, subsystems in status_by_device.items():
        try:
            device_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if device_id not in eng.meta or not isinstance(subsystems, dict):
            continue
        for subsystem, st in subsystems.items():
            if isinstance(st, dict):
                rows.append((device_id, str(subsystem), st))
    if not rows:
        return
    try:
        h.store.upsert_snmp_statuses(org, rows, ts)
    except Exception:
        log.exception("SNMP status fold failed for %s", org)
