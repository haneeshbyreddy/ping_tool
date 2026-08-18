"""Live ping routes: start, read, stop, and the probe's own exchange.

Three dashboard routes and one edge route. See `wisp.central.liveping` for the
FSM-isolation argument these handlers exist inside; the short version is that
nothing in this file imports the engine, the dispatcher or the notifier, and
the edge handler's whole job is to hand a list of `(seq, rtt)` pairs to an
in-memory ring.

WHO MAY USE IT
--------------
Owners, workers and superadmins. Workers were the ARGUMENT for the feature —
"a technician standing at the device" is the worker role — so blocking them
would ship the feature to everyone except the person it is for. It is safe to
open because of what it cannot do, the `/survey` precedent:

* it writes nothing (no device row, no state, no event, no outage);
* it cannot page anyone (no notifier is reachable from here);
* it cannot reach the FSM (structurally, see the module above);
* a worker's target must pass `visible_device_ids`, so the data layer already
  narrows them to their assigned devices before the route is even entered.

That is a deliberate decision on both layers, not a default: the route names
are in `_WORKER_GET`/`_WORKER_POST` in server.py, and the SPA shows the panel
to workers. Changing one half without the other gives a button that 403s.
"""

from __future__ import annotations

import logging
import time

from wisp.central import liveping as hub_mod
from wisp.central.api.common import (device_read_scope, in_scope, q_int_or,
                                     reader_or_401, visible_device_ids, DENIED)
from wisp.version import version_tuple

log = logging.getLogger("wisp.central.liveping")

# Room for the whole ring in one read; the panel normally asks for the handful
# of samples past its cursor.
_MAX_READ = 600


def can_live_ping(user: dict, org: str | None) -> bool:
    """Its own predicate, so it cannot drift into `_can_write`.

    Identity before role: a superadmin is `org_id IS NULL` and its role column
    is meaningless.
    """
    if user["is_superadmin"]:
        return True
    if org is None or user["org_id"] != org:
        return False
    return user.get("role") in ("owner", "worker")


def _device_org_or_denied(h, user, device_id: int):
    """Resolve the device to its org, refusing anything out of scope.

    The target is ALWAYS a device row resolved this way and never an IP the
    client typed. The remote diag walk sets the precedent — it refuses target
    IPs outside the node's device list — and the reason is sharper here: an
    accepted IP would turn a monitoring dashboard into a packet source anyone
    with a login could aim at any address on the internet.
    """
    org = h.store.device_org(device_id)
    if org is None:
        h._reply(404, {"error": "device not found"})
        return DENIED
    if not can_live_ping(user, org):
        h._reply(403, {"error": "forbidden"})
        return DENIED
    if not in_scope(visible_device_ids(h, user, org), device_id):
        h._reply(403, {"error": "forbidden"})
        return DENIED
    return org


def _node_facts(h, org: str, node_id: str | None) -> dict:
    """Version and freshness for the probe that would run the session.

    The version gate is here because half the fleet will lack `POST
    /edge/liveping` for a while, and a request nobody will ever pick up looks
    exactly like a hang. The button has to be able to say "probe needs
    v0.16.0" BEFORE it is pressed, so this rides the status read.
    """
    facts = {"node_id": node_id, "node_version": None, "node_seen": None,
             "node_stale": False, "supported": False,
             "needs_version": hub_mod.MIN_EDGE_VERSION}
    if not node_id:
        return facts
    row = next((r for r in h.store.node_versions(org)
                if r.get("node_id") == node_id), None)
    if row is None:
        return facts
    facts["node_version"] = row.get("version")
    facts["node_seen"] = row.get("last_seen")
    facts["supported"] = (version_tuple(row.get("version"))
                          >= version_tuple(hub_mod.MIN_EDGE_VERSION))
    try:
        from wisp.core.analytics import _parse
        from datetime import datetime, timezone
        seen = _parse(row.get("last_seen"))
        age = (datetime.now(timezone.utc).replace(tzinfo=None) - seen).total_seconds()
        facts["node_stale"] = age > h.cfg.central_node_stale_s
    except Exception:
        facts["node_stale"] = False
    return facts


def _wait_hint_s(h, org: str) -> int:
    """How long the operator may wait for the first packet, in seconds.

    The probe dials central; central cannot dial it. So a session that starts
    while the channel is dormant is picked up on the probe's next report, and
    that cadence is the wait. The panel prints this number rather than
    spinning: "waiting for the probe · it checks in about every 30 s" is a
    sentence a technician can act on, and a spinner is not.
    """
    try:
        org_s = h.store.org_poll_interval(org)
    except Exception:
        org_s = None
    if org_s:
        return int(min(120, max(10, int(org_s))))
    # Not `poll_interval_s`: that is the raw env value, and a small fleet runs
    # the ADAPTIVE cadence instead (`Config.effective_interval`). Printing the
    # raw number told a technician on a small fleet to expect a wait several
    # times longer than the real one, and drove the panel's own "the probe has
    # not picked this up" threshold with it. Central cannot see the edge's CLI
    # override, which outranks both — so this stays a hint, and the panel says
    # "about".
    try:
        count = len(h.store.org_device_topology(org))
    except Exception:
        count = 0
    return int(h.cfg.effective_interval(count) or 60)


def status(h, qs) -> None:
    """GET /api/liveping?device_id=N[&after=K]

    One read serves both questions the panel has: "can this device be live
    pinged at all" (before the button) and "what has arrived since sample K"
    (after it). Keeping them together is what stops the button rendering
    enabled against a probe that would never answer.
    """
    user = reader_or_401(h)
    if not user:
        return
    scoped = device_read_scope(h, user, qs)
    if scoped is None:
        return
    device_id, org = scoped
    if not can_live_ping(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    dev = h.store.get_org_device(org, device_id) or {}
    node = dev.get("assigned_node_id")
    body = {
        "device_id": device_id,
        "enabled": bool(h.cfg.liveping_enabled),
        "max_s": h.liveping.max_s,
        "wait_hint_s": _wait_hint_s(h, org),
        "org_live": h.liveping.org_live_count(org),
        "org_max": h.liveping.max_per_org,
        "session": None,
        "samples": [],
        "cursor": 0,
    }
    body.update(_node_facts(h, org, node))
    if not dev.get("ip_address"):
        body["supported"] = False
        body["unprobed"] = True
    sess = h.liveping.for_device(org, device_id)
    if sess is not None:
        now = time.time()
        after = q_int_or(qs, "after", 0)
        body["session"] = sess.public(now)
        samples, cursor = h.liveping.read(sess, after)
        body["samples"] = samples[-_MAX_READ:]
        body["cursor"] = cursor
    h._reply(200, body)


def start(h, user, body) -> None:
    """POST /api/liveping/start {device_id}"""
    if not h.cfg.liveping_enabled:
        h._reply(404, {"error": "live ping is disabled on this server"})
        return
    try:
        device_id = int(body.get("device_id"))
    except (TypeError, ValueError):
        h._reply(400, {"error": "device_id required"})
        return
    org = _device_org_or_denied(h, user, device_id)
    if org is DENIED:
        return
    dev = h.store.get_org_device(org, device_id)
    if not dev:
        h._reply(404, {"error": "device not found"})
        return
    ip = dev.get("ip_address")
    if not ip:
        h._reply(400, {"error": "this device has no IP address to ping"})
        return
    node = dev.get("assigned_node_id")
    if not node:
        h._reply(400, {"error": "device has no assigned probe"})
        return
    facts = _node_facts(h, org, node)
    if not facts["supported"]:
        # Never a spinner. The client renders this verbatim.
        h._reply(409, {"error": f"probe needs v{hub_mod.MIN_EDGE_VERSION}",
                       **facts})
        return
    # Aggregation gear gets the slower rung. Decided HERE, not on the edge,
    # because central holds the whole org topology and a probe only sees its
    # own slice — a device whose children live on another node would read as a
    # leaf edge-side and get a cadence its ICMP rate limiter answers with
    # phantom loss, on the very box the technician is standing next to.
    infra = hub_mod.is_infra(device_id, h.store.org_device_parents(org))
    sess, err = h.liveping.start(
        org_id=org, node_id=node, device_id=device_id, device_ip=ip,
        infra=infra, started_by=user["username"])
    if sess is None:
        h._reply(429, {"error": err or "cannot start a live ping session"})
        return
    log.info("live ping %s opened by %s for %s/device=%d (%s, %d ms)",
             sess.sid[:8], user["username"], org, device_id, ip, sess.interval_ms)
    h._reply(200, {"session": sess.public(time.time()),
                   "wait_hint_s": _wait_hint_s(h, org),
                   "max_s": h.liveping.max_s, **facts})


def stop(h, user, body) -> None:
    """POST /api/liveping/stop {sid}

    Anyone who could have started it may stop it, including the other operator
    watching the same device. A session is a property of the DEVICE, not of
    the viewer, and it auto-stops within five minutes regardless.
    """
    sid = str(body.get("sid") or "")
    sess = h.liveping.get(sid)
    if sess is None:
        h._reply(200, {"ok": True, "was_live": False})
        return
    org = _device_org_or_denied(h, user, sess.device_id)
    if org is DENIED:
        return
    # The org the ROUTE authorized, not the one the session carries: the two
    # cannot differ today, and the check is worth nothing if the mutation
    # reads a different value from the one that was checked.
    was = h.liveping.stop(sid, org)
    h._reply(200, {"ok": True, "was_live": was,
                   "session": sess.public(time.time())})


def wake_flag(h, org: str, node: str) -> bool:
    """Should the report reply tell this probe to open its live-ping channel?

    Composed here, and read from `api/edge.report` in one line, so the data
    only ever flows central -> edge. Nothing a live session produces is
    readable from the report path.
    """
    if not h.cfg.liveping_enabled or not node:
        return False
    try:
        return h.liveping.node_has_work(org, node)
    except Exception:
        log.debug("live ping wake check failed for %s/%s", org, node, exc_info=True)
        return False


def edge_exchange(h, org: str, node: str, env: dict) -> None:
    """POST /edge/liveping — the probe's only live-ping call.

    Samples up, the current session set down, one round trip. Declarative:
    the reply is the WHOLE truth about what this probe should be pinging, so
    a stop cannot be lost in transit — a session simply stops being in the
    set. There is no command log and no stored state on either side, which is
    what makes a restart on either end a clean end to a live session rather
    than a stuck one.

    This handler reaches `h.liveping` and nothing else. It never builds a
    `PingResult`, never touches `h.registry`, and never calls into
    `central_engine`.
    """
    if not h.cfg.liveping_enabled:
        h._reply(200, {"sessions": [], "token": 0, "disabled": True})
        return
    raw = env.get("samples")
    delivered = False
    if isinstance(raw, dict):
        for sid, samples in list(raw.items())[:16]:
            if not isinstance(samples, list) or not samples:
                continue
            delivered = True
            h.liveping.ingest(org, node, str(sid), samples[:_MAX_READ])
    for item in (env.get("refusals") or [])[:16]:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("sid") or "")
        detail = str(item.get("error") or "")[:200]
        # The probe refusing a target is a FINDING, not a failure to hide: it
        # means central named an IP this node does not probe. Recorded as the
        # stop reason so the panel says so instead of showing an empty stream.
        if h.liveping.stop(sid, org, reason=hub_mod.STOP_REFUSED,
                          detail=detail, node_id=node):
            log.warning("live ping %s refused by %s/%s: %s", sid[:8], org, node, detail)
        delivered = True
    h.liveping.mark_picked_up(org, node)
    try:
        token = int(env.get("token") or 0)
    except (TypeError, ValueError):
        token = 0
    # Park for what the EDGE asked for, clamped to our own ceiling. The edge
    # asks for ~0 when it has packets to ship and a long hold only when idle,
    # so ignoring the request makes every exchange time out on the client's
    # own deadline (`hold_s + 10`) and turns a one-line-per-second stream into
    # a burst every ~14 s. Absent or junk = an older probe that does not send
    # it: fall back to the configured hold, which is what it used to get.
    ceiling = float(h.cfg.liveping_poll_hold_s)
    try:
        asked = float(env.get("hold_s"))
    except (TypeError, ValueError):
        asked = ceiling
    if not 0.0 <= asked <= ceiling:      # also catches NaN
        asked = ceiling
    token, sessions = h.liveping.exchange(
        org, node, token=token, hold_s=asked, deliver=delivered)
    h._reply(200, {"token": token, "sessions": sessions})
