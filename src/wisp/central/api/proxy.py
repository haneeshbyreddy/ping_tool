from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone

from wisp.central import auth
from wisp.central import proxy as proxy_mod
from wisp.central.api.common import org_or_400, reader_or_401
from wisp.central.proxy import MAX_INFLIGHT_PER_SESSION, ProxySession, parse_ports
from wisp.central.secretbox import DecryptError

log = logging.getLogger("wisp.central")

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
    "proxy-authorization", "proxy-authenticate", "content-length",
    "content-encoding",
})

_CSP_HEADERS = frozenset({
    "content-security-policy", "content-security-policy-report-only",
    "x-content-security-policy", "x-webkit-csp",
})

_DB_TOUCH_EVERY_S = 20.0

_FWD_REQ_HEADERS = ("Accept", "Accept-Language", "Cache-Control", "Content-Type",
                    "If-Modified-Since", "If-None-Match", "Pragma", "Range",
                    "User-Agent", "X-Requested-With")


def _device_origin(sess) -> str:
    return f"{sess.scheme}://{sess.device_ip}:{sess.device_port}"


def _forward_headers(h, sid: str, sess) -> dict:
    out: dict[str, str] = {}
    for name in _FWD_REQ_HEADERS:
        v = h.headers.get(name)
        if v:
            out[name] = v
    raw_cookie = h.headers.get("Cookie") or ""
    kept = [c.strip() for c in raw_cookie.split(";")
            if c.strip() and not c.strip().startswith(auth.SESSION_COOKIE + "=")]
    if kept:
        out["Cookie"] = "; ".join(kept)
    origin = _device_origin(sess)
    prefix = f"/api/proxy/{sid}"
    ref = h.headers.get("Referer") or ""
    at = ref.find(prefix)
    if at != -1:
        out["Referer"] = origin + (ref[at + len(prefix):] or "/")
    if h.headers.get("Origin"):
        out["Origin"] = origin
    if getattr(sess, "injected_auth", None):
        out["Authorization"] = sess.injected_auth
    else:
        authz = h.headers.get("Authorization") or ""
        if authz.startswith("Basic "):
            out["Authorization"] = authz
    return out


def _resolve_injected_auth(h, org_id: str, device_id: int) -> str | None:
    row = h.store.get_device_webui_credentials(org_id, device_id)
    if not row or (row.get("auth_mode") or "form") != "basic":
        return None
    enc = row.get("password_enc")
    if not enc:
        return None
    try:
        password = h.secretbox.decrypt(enc)
    except DecryptError:
        log.warning("proxy: could not decrypt stored login for device=%d "
                    "(key rotated?) — opening tunnel without auth injection",
                    device_id)
        return None
    user = row.get("username") or ""
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return "Basic " + token


def _resolve_autofill(h, org_id: str, device_id: int) -> tuple[str, str] | None:
    row = h.store.get_device_webui_credentials(org_id, device_id)
    if not row or (row.get("auth_mode") or "form") != "form":
        return None
    enc = row.get("password_enc")
    if not enc:
        return None
    try:
        password = h.secretbox.decrypt(enc)
    except DecryptError:
        log.warning("proxy: could not decrypt stored login for device=%d "
                    "(key rotated?) — opening tunnel without autofill", device_id)
        return None
    return (row.get("username") or "", password)

_PROXY_ROLES = ("owner",)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _resolve_web_endpoint(h, dev: dict, body: dict) -> tuple[str, int, str, str | None]:
    web_ip = (dev.get("web_ip") or "").strip()
    web_port = dev.get("web_port")
    web_scheme = (dev.get("web_scheme") or "").strip().lower()
    if web_ip or web_port or web_scheme:
        ip = web_ip or (dev.get("ip_address") or "").strip()
        if not ip:
            return "", 0, "", "device has no IP"
        try:
            port = int(web_port) if web_port else (443 if web_scheme == "https" else 80)
        except (TypeError, ValueError):
            return "", 0, "", "device has a bad web port configured"
        if not (1 <= port <= 65535):
            return "", 0, "", "device has a bad web port configured"
        scheme = web_scheme or ("https" if port == 443 else "http")
        return ip, port, scheme, None
    ip = (dev.get("ip_address") or "").strip()
    if not ip:
        return "", 0, "", "device has no IP"
    try:
        port = int(body.get("port") or 80)
    except (TypeError, ValueError):
        return "", 0, "", "bad port"
    if port not in parse_ports(h.cfg.proxy_mgmt_ports):
        return "", 0, "", f"port {port} not in proxy_mgmt_ports"
    return ip, port, "https" if port == 443 else "http", None


_PREFLIGHT_TIMEOUT_S = 8.0


def _preflight_candidates(cfg, dev: dict, ip: str, port: int,
                          scheme: str) -> list[tuple[str, int, str]]:
    if (dev.get("web_ip") or dev.get("web_port") or dev.get("web_scheme")):
        if (dev.get("web_scheme") or "").strip():
            return [(ip, port, scheme)]
        return [(ip, port, "https"), (ip, port, "http")]
    allowed = parse_ports(cfg.proxy_mgmt_ports)
    cands = [(ip, p, s) for p, s in ((443, "https"), (80, "http")) if p in allowed]
    cands.sort(key=lambda c: 0 if c[1] == port else 1)
    return cands or [(ip, port, scheme)]


def _parse_preflight_reply(resp: dict | None) -> list | None:
    if not resp or resp.get("error"):
        return None
    try:
        doc = json.loads(base64.b64decode(resp.get("body_b64") or ""))
    except (ValueError, TypeError):
        return None
    if not (isinstance(doc, dict) and doc.get("preflight")
            and isinstance(doc.get("results"), list)):
        return None
    return doc["results"]


def preflight_endpoint(proxy, cfg, org: str, node: str, device_id: int,
                       dev: dict, ip: str, port: int, scheme: str
                       ) -> tuple[str, int, str, str | None]:

    if not proxy.polled_recently(org, node, cfg.proxy_poll_hold_s + 5.0):
        return ip, port, scheme, None
    cands = _preflight_candidates(cfg, dev, ip, port, scheme)
    probe = ProxySession(
        sid="preflight", org_id=org, device_id=device_id, node_id=node,
        device_ip=ip, device_port=port, scheme=scheme, created_by=0,
        created_at=time.time(), expires_at=time.time() + _PREFLIGHT_TIMEOUT_S)
    resp = proxy.submit(
        probe, method="GET", path="/", headers={}, body=b"",
        timeout=_PREFLIGHT_TIMEOUT_S,
        extra={"kind": "preflight",
               "candidates": [[c[0], c[1], c[2]] for c in cands]})
    results = _parse_preflight_reply(resp)
    if results is None:
        return ip, port, scheme, None
    ok: dict[tuple[str, int, str], bool] = {}
    for row in results:
        try:
            ok[(str(row[0]), int(row[1]), str(row[2]))] = bool(row[3])
        except (TypeError, ValueError, IndexError):
            continue
    for cand in cands:
        if ok.get(cand):
            return cand[0], cand[1], cand[2], None
    if not any(cand in ok for cand in cands):
        return ip, port, scheme, None
    tried = ", ".join(f"{s}://{i}:{p}" for i, p, s in cands)
    return ip, port, scheme, (
        "device web UI unreachable from the probe. Nothing answered at "
        f"{tried}. Check that the web UI is enabled and the address/port is "
        "right (Web UI settings on the device row).")


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
        h._reply(403, {"error": "owner role required"})
        return
    if not h.store.org_web_proxy(org):
        h._reply(403, {"error": "web proxy is not enabled for this organization"})
        return
    dev = h.store.get_org_device(org, device_id)
    if not dev:
        h._reply(404, {"error": "device not found"})
        return
    node = dev.get("assigned_node_id")
    if not node:
        h._reply(400, {"error": "device has no assigned probe"})
        return
    ip, port, scheme, err = _resolve_web_endpoint(h, dev, body)
    if err:
        h._reply(400, {"error": err})
        return
    ip, port, scheme, err = preflight_endpoint(
        h.proxy, h.cfg, org, node, device_id, dev, ip, port, scheme)
    if err:
        h._reply(502, {"error": err})
        return
    replaced = h.proxy.close_sessions_for(org, node)
    if h.store.close_node_proxy_sessions(org, node) or replaced:
        log.info("proxy: replacing open session(s) %s on %s/%s",
                 [s[:8] for s in replaced] or "(db-only)", org, node)
    sess = h.proxy.open_session(
        org_id=org, device_id=device_id, node_id=node, device_ip=ip,
        device_port=port, scheme=scheme, created_by=user["id"],
        ttl_s=h.cfg.proxy_session_ttl_s,
        cache=proxy_mod.AssetCache(
            max_entries=h.cfg.proxy_cache_max_entries,
            max_bytes=h.cfg.proxy_cache_max_bytes,
            ttl_s=h.cfg.proxy_cache_ttl_s))
    sess.db_synced_at = sess.created_at
    sess.injected_auth = _resolve_injected_auth(h, org, device_id)
    sess.autofill = _resolve_autofill(h, org, device_id)
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
        h.store.close_proxy_session(sid, "expired")
        h._reply(404, {"error": "session not found or expired"})
        return
    if not (user["is_superadmin"] or user["org_id"] == sess.org_id):
        h._reply(403, {"error": "forbidden"})
        return
    if not (user["is_superadmin"] or user.get("role") in _PROXY_ROLES):
        h._reply(403, {"error": "owner role required"})
        return
    if not h.store.org_web_proxy(sess.org_id):
        h.proxy.close_session(sid)
        h.store.close_proxy_session(sid, "closed")
        h._reply(403, {"error": "web proxy has been disabled for this organization"})
        return
    if h._billing_blocked(f"/api/proxy/{sid}/", user):
        return
    if rest.strip("/") == proxy_mod.AUTOFILL_PATH:
        af = getattr(sess, "autofill", None)
        if af:
            h._reply(200, {"u": af[0], "p": af[1]})
        else:
            h._reply(404, {"error": "not found"})
        return
    path = "/" + rest + (("?" + query) if query else "")
    new_exp = h.proxy.extend_session(sess, h.cfg.proxy_session_ttl_s)
    if time.time() - sess.db_synced_at >= _DB_TOUCH_EVERY_S:
        sess.db_synced_at = time.time()
        h.store.touch_proxy_session(sid, _iso(new_exp))
    cached = None
    use_cache = h.cfg.proxy_cache_enabled and proxy_mod.cacheable_path(method, path)
    ckey = proxy_mod.cache_key(path) if use_cache else path
    if use_cache:
        cached = sess.cache.get(ckey)
    if cached is not None:
        _audit(h, sid, sess, user, method, path, cached[0])
        _finish(h, sid, sess, cached[0], cached[1], cached[2])
        return
    if h.proxy.inflight(sid) >= MAX_INFLIGHT_PER_SESSION:
        h._reply(429, {"error": "too many concurrent requests on this session"})
        return
    response = h.proxy.submit(
        sess, method=method, path=path, headers=_forward_headers(h, sid, sess),
        body=body, timeout=h.cfg.proxy_request_timeout_s)
    if response is None:
        audit_status = 504
    elif response.get("error"):
        audit_status = 502
    else:
        audit_status = int(response.get("status", 502))
    _audit(h, sid, sess, user, method, path, audit_status)
    if response is None:
        log.warning("proxy %s: %s %s timed out after %.0fs (device=%d %s:%d)",
                    sid[:8], method, path, h.cfg.proxy_request_timeout_s,
                    sess.device_id, sess.device_ip, sess.device_port)
        h._reply(504, {"error": "device did not respond in time"})
        return
    if response.get("error"):
        log.warning("proxy %s: %s %s failed on device=%d (%s://%s:%d): %s",
                    sid[:8], method, path, sess.device_id, sess.scheme,
                    sess.device_ip, sess.device_port, response["error"])
        narrowed = h.proxy.note_failure(sess.org_id, sess.device_id,
                                        response["error"])
        if narrowed is not None:
            log.info("proxy: %s:%d is dropping connections — narrowing device=%d "
                     "to %d concurrent request(s)", sess.device_ip,
                     sess.device_port, sess.device_id, narrowed)
        h._reply(502, {"error": f"edge fetch failed: {response['error']}"})
        return
    status = int(response.get("status", 502))
    raw = base64.b64decode(response.get("body_b64") or "")
    pairs = [(k, v) for k, v in _norm_header_pairs(response.get("headers"))
             if k.lower() not in _HOP_BY_HOP]
    if use_cache:
        refusal = proxy_mod.cache_refusal(status, pairs)
        if refusal is None:
            sess.cache.put(ckey, status, pairs, raw)
        else:
            _log_cache_refusal(sess, path, refusal)
    _finish(h, sid, sess, status, pairs, raw)


def _log_cache_refusal(sess, path: str, reason: str) -> None:

    if reason in sess.cache_refusals:
        return
    sess.cache_refusals.add(reason)
    log.info("proxy %s: not caching device=%d assets — %s (first seen on %s)",
             sess.sid[:8], sess.device_id, reason, path)


def _audit(h, sid: str, sess, user, method: str, path: str, status: int) -> None:
    try:
        h.store.record_proxy_audit(sid, sess.org_id, sess.device_id, user["id"],
                                   method, path, status)
    except Exception:
        log.exception("proxy audit write failed for session %s", sid[:8])


def _finish(h, sid: str, sess, status: int, pairs: list, raw: bytes) -> None:
    autofill = getattr(sess, "autofill", None)
    if autofill:
        pairs = [(k, v) for k, v in pairs if k.lower() not in _CSP_HEADERS]
    pairs = proxy_mod.rewrite_headers(sid, sess, pairs)
    ctype = next((v for k, v in pairs if k.lower() == "content-type"), "")
    raw = proxy_mod.rewrite_body(sid, ctype, raw)
    if autofill:
        raw = proxy_mod.inject_autofill(ctype, raw, sid)
    h._raw_reply(status, pairs, raw)
