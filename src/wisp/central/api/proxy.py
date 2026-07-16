"""Device web-UI proxy routes (webplan.md, M0 tunnel + M1 sessions/security).

Three actors:

  * ``session_create`` / ``session_close`` / ``sessions_list`` / ``audit_list``
    — dashboard: an org member opens a tunnel session against one of their
    devices; every session is persisted (``proxy_sessions``), every proxied
    request audited (``proxy_audit``), owners see both.
  * ``browser_request`` — GET/POST /api/proxy/<sid>/<path>: a browser request,
    parked on the hub and forwarded to the edge, its device reply streamed back
    RAW (not JSON), with Location/Set-Cookie/body references rewritten into the
    session prefix. Session-scoped, org-checked on every call.
  * ``edge_next`` / ``edge_reply`` — /edge/proxy/next (long-poll pickup) and
    /edge/proxy/reply (result upload): edge-credentialed, same auth as /report.

Security spine (webplan.md §6): cfg.proxy_enabled is the fleet kill switch AND
orgs.web_proxy is the per-org opt-in (superadmin-set) — both must be on. Only
owner/operator (or superadmin) can open a session; sessions expire, activity
extends them; the edge re-checks every target IP against its own device list.
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timezone

from wisp.central import proxy as proxy_mod
from wisp.central.api.common import org_or_400, reader_or_401
from wisp.central.proxy import MAX_INFLIGHT_PER_SESSION, parse_ports

log = logging.getLogger("wisp.central")

# Never forwarded device->browser: connection-scoped, recomputed by us, or —
# content-encoding — already undone: httpx on the edge decompresses the body, so
# forwarding the device's "Content-Encoding: gzip" with plain bytes would make
# the browser try to gunzip uncompressed content.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
    "proxy-authorization", "proxy-authenticate", "content-length",
    "content-encoding",
})

# How often browser activity syncs the session's sliding expiry to the DB row —
# per-asset writes would hammer SQLite for nothing (the hub is authoritative).
_DB_TOUCH_EVERY_S = 20.0

# Roles that may OPEN a session (webplan.md §6.5) — techs browse the dashboard,
# not device admin UIs.
_PROXY_ROLES = ("owner", "operator")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def session_create(h, user, body) -> None:
    if not h.cfg.proxy_enabled:
        h._reply(404, {"error": "web proxy is disabled"})
        return
    try:
        device_id = int(body.get("device_id"))
    except (TypeError, ValueError):
        h._reply(400, {"error": "device_id required"})
        return
    org = h.store.device_org(device_id)
    if org is None:
        h._reply(404, {"error": "device not found"})
        return
    if not (user["is_superadmin"] or user["org_id"] == org):
        h._reply(403, {"error": "forbidden"})
        return
    if not (user["is_superadmin"] or user.get("role") in _PROXY_ROLES):
        h._reply(403, {"error": "operator role or above required"})
        return
    if not h.store.org_web_proxy(org):
        h._reply(403, {"error": "web proxy is not enabled for this organization"})
        return
    dev = h.store.get_org_device(org, device_id)
    if not dev:
        h._reply(404, {"error": "device not found"})
        return
    ip = (dev.get("ip_address") or "").strip()
    node = dev.get("assigned_node_id")
    if not ip or not node:
        h._reply(400, {"error": "device has no IP or assigned probe"})
        return
    allowed_ports = parse_ports(h.cfg.proxy_mgmt_ports)
    try:
        port = int(body.get("port") or 80)
    except (TypeError, ValueError):
        h._reply(400, {"error": "bad port"})
        return
    if port not in allowed_ports:
        h._reply(400, {"error": f"port {port} not in proxy_mgmt_ports"})
        return
    scheme = "https" if port == 443 else "http"
    sess = h.proxy.open_session(
        org_id=org, device_id=device_id, node_id=node, device_ip=ip,
        device_port=port, scheme=scheme, created_by=user["id"],
        ttl_s=h.cfg.proxy_session_ttl_s)
    sess.db_synced_at = sess.created_at
    h.store.create_proxy_session(sess.sid, org, device_id, node, user["id"],
                                 _iso(sess.expires_at))
    log.info("proxy session %s opened by user=%s for %s/device=%d (%s:%d)",
             sess.sid[:8], user["id"], org, device_id, ip, port)
    h._reply(200, {"sid": sess.sid, "url": f"/api/proxy/{sess.sid}/",
                   "device_id": device_id, "expires_at": sess.expires_at})


def session_close(h, user, body) -> None:
    sid = str(body.get("sid") or "")
    row = h.store.proxy_session_row(sid)
    if row is None:
        h._reply(404, {"error": "session not found"})
        return
    org = row["org_id"]
    same_org = user["org_id"] == org
    may_close = (user["is_superadmin"]
                 or (same_org and user.get("role") == "owner")
                 or (same_org and row["created_by"] == user["id"]))
    if not may_close:
        h._reply(403, {"error": "forbidden"})
        return
    h.proxy.close_session(sid)
    closed = h.store.close_proxy_session(sid, "closed")
    log.info("proxy session %s closed by user=%s", sid[:8], user["id"])
    h._reply(200, {"ok": True, "was_open": closed})


def sessions_list(h, qs) -> None:
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    rows = h.store.list_proxy_sessions(org)
    for r in rows:
        # DB says open; only the hub knows whether the tunnel survived a
        # central restart. Not live + still 'open' = row-only zombie.
        r["live"] = r["status"] == "open" and h.proxy.has_session(r["sid"])
    h._reply(200, {"sessions": rows})


def audit_list(h, qs) -> None:
    user = reader_or_401(h)
    if not user:
        return
    if not (user["is_superadmin"] or user.get("role") == "owner"):
        h._reply(403, {"error": "forbidden"})
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    try:
        limit = int((qs.get("limit") or [200])[0])
    except (TypeError, ValueError):
        limit = 200
    h._reply(200, {"audit": h.store.list_proxy_audit(org, min(limit, 1000))})


def edge_next(h, qs) -> None:
    org = (qs.get("org_id") or [None])[0]
    node = (qs.get("node_id") or [None])[0]
    if not org or not node:
        h._reply(400, {"error": "org_id and node_id required"})
        return
    if not h._ingest_ok(org, node):
        h._reply(401, {"error": "unauthorized"})
        return
    payload = h.proxy.next_request(org, node, h.cfg.proxy_poll_hold_s)
    h._reply(200, {"request": payload})


def _norm_header_pairs(raw) -> list[tuple[str, str]]:
    """Edge sends headers as [[k, v], ...] so repeated names (multiple
    Set-Cookie) survive; a plain dict (M0 shape, tests) is accepted too."""
    if isinstance(raw, dict):
        return [(str(k), str(v)) for k, v in raw.items()]
    out: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append((str(item[0]), str(item[1])))
    return out


def edge_reply(h, org: str, node: str, env: dict) -> None:
    try:
        req_id = int(env.get("req_id"))
    except (TypeError, ValueError):
        h._reply(400, {"error": "req_id required"})
        return
    body_b64 = env.get("body_b64") or ""
    if not isinstance(body_b64, str) or len(body_b64) > h.cfg.proxy_max_body_bytes:
        h._reply(413, {"error": "proxied response too large"})
        return
    try:
        status = int(env.get("status") or 502)
    except (TypeError, ValueError):
        status = 502
    response = {"status": status, "headers": _norm_header_pairs(env.get("headers")),
                "body_b64": body_b64, "error": env.get("error")}
    ok = h.proxy.deliver(req_id, org, node, response)
    h._reply(200, {"ok": ok})


def browser_request(h, method: str, sid: str, rest: str, query: str,
                    body: bytes) -> None:
    if not h.cfg.proxy_enabled:
        h._reply(404, {"error": "web proxy is disabled"})
        return
    user = h._reader()
    if not user:
        h._reply(401, {"error": "unauthorized"})
        return
    sess = h.proxy.get_session(sid)
    if sess is None:
        # the hub already forgot it; make the DB record agree (best-effort)
        h.store.close_proxy_session(sid, "expired")
        h._reply(404, {"error": "session not found or expired"})
        return
    if not (user["is_superadmin"] or user["org_id"] == sess.org_id):
        h._reply(403, {"error": "forbidden"})
        return
    if not h.store.org_web_proxy(sess.org_id):
        # superadmin revoked the capability mid-session: kill it now, not at TTL
        h.proxy.close_session(sid)
        h.store.close_proxy_session(sid, "closed")
        h._reply(403, {"error": "web proxy has been disabled for this organization"})
        return
    if h._billing_blocked(f"/api/proxy/{sid}/", user):
        return
    if h.proxy.inflight(sid) >= MAX_INFLIGHT_PER_SESSION:
        h._reply(429, {"error": "too many concurrent requests on this session"})
        return
    # activity slides the expiry window; the DB row follows at most every ~20s
    new_exp = h.proxy.extend_session(sess, h.cfg.proxy_session_ttl_s)
    if time.time() - sess.db_synced_at >= _DB_TOUCH_EVERY_S:
        sess.db_synced_at = time.time()
        h.store.touch_proxy_session(sid, _iso(new_exp))
    path = "/" + rest + (("?" + query) if query else "")
    response = h.proxy.submit(
        sess, method=method, path=path, headers={}, body=body,
        timeout=h.cfg.proxy_request_timeout_s)
    if response is None:
        audit_status = 504
    elif response.get("error"):
        audit_status = 502
    else:
        audit_status = int(response.get("status", 502))
    try:
        h.store.record_proxy_audit(sid, sess.org_id, sess.device_id, user["id"],
                                   method, path, audit_status)
    except Exception:  # the reply still goes out — audit must not eat it
        log.exception("proxy audit write failed for session %s", sid[:8])
    if response is None:
        h._reply(504, {"error": "device did not respond in time"})
        return
    if response.get("error"):
        h._reply(502, {"error": f"edge fetch failed: {response['error']}"})
        return
    raw = base64.b64decode(response.get("body_b64") or "")
    pairs = [(k, v) for k, v in _norm_header_pairs(response.get("headers"))
             if k.lower() not in _HOP_BY_HOP]
    pairs = proxy_mod.rewrite_headers(sid, sess, pairs)
    ctype = next((v for k, v in pairs if k.lower() == "content-type"), "")
    raw = proxy_mod.rewrite_body(sid, ctype, raw)
    h._raw_reply(response.get("status", 502), pairs, raw)
