# webplan.md — Device Web-UI Proxy (reverse tunnel through the edge)

**Goal:** From the dashboard, click a switch/OLT and drive its native web UI in the
browser, with traffic flowing **browser → central → edge → device** and back. The
edge is already central's hands on the LAN for SNMP; this makes it central's hands
for HTTP too.

**Status:** **M0 + M1 BUILT and green (2026-07-16)** — activation is the per-org
`orgs.web_proxy` capability flag (default off, superadmin-set via `POST /api/org`).
**Central-driven since v0.15.8 (2026-07-16):** `WISP_PROXY_ENABLED` now defaults ON
and is only the emergency kill switch (`=0`, honored per edge and on central) — the
original double-dark shipped every fresh edge with the tunnel off, and the missing
env var surfaced as an undiagnosable 504 on every session (the Rapid Networks
Edge_1 incident; 504 = edge never picked up, 502 = edge fetched and failed). The
edge machinery is still DORMANT until a `/report` reply carries a live session, so
default-on costs zero idle long-polls. Full suite: 754 passing
(`tests/integration/test_central_proxy.py`, `tests/unit/test_webproxy.py`).
**Field-proven 2026-07-16:** full login flow on HILL-OLT-1 (C-Data, via
EDGE_SAGAR on v0.15.7) through the tunnel — POSTs, cookies, asset fan-out all
held. Gotcha: the OLT refuses port 80; create the session with `port: 443`
(a port-80 attempt surfaces as `502 edge fetch failed: All connection attempts
failed` — the edge's httpx ConnectError relayed).

**Field-test fixes + M3 BUILT (2026-07-16, central-only — works with the
deployed v0.15.7 edge):**

- **Request headers now forward browser→device** (`api/proxy.py:_forward_headers`
  — the field test showed login bouncing because `headers={}` dropped the
  device's session cookie). Allow-list + special cases: device cookies travel,
  central's `wisp_central_session` is stripped; Referer/Origin rewritten to the
  device origin (firmware CSRF checks); Authorization forwarded only for
  `Basic` (a bearer would be central's own token). Everything else (Host,
  Accept-Encoding, Content-Length…) stays recomputed by httpx on the edge.
- **Escape rescue** (`server.py:_proxy_rescue`): a JS-built root-absolute URL
  (missing icons in the field test — the documented M2 gap) lands on central as
  an unknown route; if the Referer names a LIVE session it 307s back inside the
  prefix (method+body preserved, full auth re-runs on the tunnel route). Wired
  into the do_GET 404 fallthrough and do_POST unknown-route branch.
- **M3 dashboard UI** (`web/src/components/web-proxy.tsx`): a compact "Web UI"
  button on the RIGHT of the device panel's Health/Optical/Ports tab row
  (`WebUiButton` in DeviceDetail — so it shows on the Network drill-in AND the
  Map panel; its own right-aligned row when the device has no tabs). Dropdown
  offers http/https, last-working port first (remembered in localStorage);
  blank tab opened synchronously so popup blockers don't eat it. Moved OUT of
  the three-dots menu (2026-07-16, user ask) — that menu is write-actions only
  again. Owner-only + org flag gated. Settings → "Device web UI sessions"
  card (open/recent sessions, live badge, Close, owner-only audit trail);
  Organizations page superadmin "web UI" toggle per org
  (`orgsApi.save({web_proxy})`).

- **One tunnel per probe (2026-07-16):** `session_create` replaces whatever was
  open on that node — `ProxyHub.close_sessions_for` +
  `store.close_node_proxy_sessions` (the DB sweep also retires restart
  zombies). Newest wins; the operator never hunts a forgotten session in
  Settings. Menu declutter: http/https live in ONE "Open web UI" submenu row
  (last-used first — kept a submenu even for remembered devices so a firmware
  port move stays switchable), separators between webUI / edit-group / Delete.
- **Live indicator = a row capability icon** (`WebUiLiveIcon`, user-specified
  form): a pulsing green globe beside the optics/ports icons on the Network
  tree row AND grid card while that device's tunnel is live; click jumps back
  into the session tab. (A panel strip version was built first and replaced
  same day — the user wanted the ports/optics icon idiom.) Rides the same
  ["proxy-sessions", org] query as the Settings card (one 15s poll shared by
  every row); opening from the panel button invalidates it so the icon
  appears immediately.

Tests: 758 passing (new: header forwarding/cookie-strip, GET+POST rescue,
rescue-requires-live-session, one-session-per-node). Remaining: M2
wildcard-host (only if a vendor UI defeats prefix+rescue), WebSockets (out of
scope v1). Grounded in the existing diagnostic-SNMP-walk path
(`api/edge.py:report` → reply `snmp_walks` → `main.py:_DiagWalkRunner` →
`/edge/snmp-walk`), the same request/response-over-an-outbound-channel pattern.

### What M0 shipped

| Piece | File |
| --- | --- |
| Parking hub (cross-thread desk, TTL sessions, cross-node reply guard) | `src/wisp/central/proxy.py` |
| Routes: `session_create` / `browser_request` / `edge_next` / `edge_reply` | `src/wisp/central/api/proxy.py` |
| Wiring (`/edge/proxy/next`, `/edge/proxy/reply`, `/api/proxy/<sid>/*`, `_raw_reply`, hub attach) | `src/wisp/central/server.py`, `api/__init__.py` |
| Edge tunnel worker (long-poll pool, allow-list + port gate, device fetch) | `src/wisp/ingress/webproxy.py` |
| Edge client `proxy_next` / `proxy_reply` (long-poll timeout) | `src/wisp/runtime/central_client.py` |
| Daemon wiring (dark-by-default, dedicated client, live `lambda: devices` allow-list) | `apps/daemon/main.py` |
| Config knobs (`proxy_enabled`, `proxy_mgmt_ports`, hold/workers/ttl/timeout/max-body) | `src/wisp/config.py` |

### What M1 shipped (2026-07-16)

| Piece | Where |
| --- | --- |
| `proxy_sessions` + `proxy_audit` tables, `orgs.web_proxy` flag | `store.py` `_SCHEMA`/`_ensure_columns`, `store_proxy.py` mixin |
| Role gate (owner/superadmin open AND drive; owner/creator close; audit view owner-only) | `api/proxy.py` |
| Per-org capability flag, checked on create AND every proxied request (revoke kills live sessions) | `api/proxy.py`, `api/orgs.py:update` (superadmin-only field) |
| TTL slides on activity (hub authoritative, DB row synced ≤ every 20 s) | `central/proxy.py:extend_session`, `api/proxy.py` |
| Audit row per proxied request incl. 502/504; pruned at 60 days | `store_proxy.py`, `api/proxy.py:browser_request` |
| Per-session in-flight cap (16 → 429) so one session can't starve central's threads | `central/proxy.py:MAX_INFLIGHT_PER_SESSION` |
| Session routes: `GET /api/proxy/sessions` (with `live`), `GET /api/proxy/audit`, `POST /api/proxy/close` | `api/proxy.py`, `api/__init__.py`, `server.py:_PROXY_EXACT` |
| Dormant-until-session activation: live sessions ride the `/report` reply (`proxy_sessions`, RELATIVE `ttl_s`); edge workers spin up on the key, stand down after | `api/edge.py:report`, `ingress/webproxy.py:notify_sessions`, `apps/daemon/main.py` |
| Best-effort rewriting: `Location` (root-absolute + device-origin), `Set-Cookie Path=`, root-absolute `href/src/action` + CSS `url(/…)` in html/css bodies. Headers travel as PAIRS end-to-end so multiple `Set-Cookie` survive; `Content-Encoding` stripped (httpx already decompressed on the edge). **No `<base href>` injection** — the proxy preserves the device's path hierarchy, so a base tag would re-anchor relative URLs and break subdirectory pages (deliberate deviation from §7). | `central/proxy.py:rewrite_headers/rewrite_body`, `api/proxy.py` |

**Still NOT done:** JS-built absolute URLs (M2 wildcard-host problem), dashboard UI
(M3), WebSockets (out of scope v1).

---

## 1. The core idea, and why it fits the architecture

A device web UI is an HTTP conversation. We already move commands to the edge and
results back without the edge ever accepting an inbound connection — the edge
**pulls** work from central over a connection it dialed. A web proxy is the same,
with two upgrades:

1. **Latency** — the 30–60 s `/report` cadence is fine for a one-shot walk, useless
   for a UI where every click needs a sub-second reply. We add a dedicated
   **long-poll channel** the edge holds open.
2. **Volume/streaming** — a single page pulls dozens of assets; we need a few
   concurrent in-flight requests and byte-accurate (not JSON-mangled) bodies.

**The `edge dials central, never the reverse` invariant is preserved.** The edge
opens the long-poll connection *outbound*; central parks a browser request on it and
releases it when one arrives. No inbound port, no reverse dial, no new firewall hole
on the ISP side. This is exactly how ngrok / Cloudflare Tunnel / Teleport work.

```
  Browser                Central (hansanet.in)              Edge (on ISP LAN)         Device
    │  GET /api/proxy/<sid>/<path>                                                       │
    ├───────────────────────────────▶│                                                  │
    │                                 │  park request, keyed by session sid              │
    │                                 │◀───── long-poll GET /edge/proxy/next ────────────┤ (held open)
    │                                 │  release parked request ─────────────────────────▶│
    │                                 │                                    fetch http://dev-ip/path
    │                                 │                                                  ├──────▶│
    │                                 │                                                  │◀──────┤
    │                                 │◀──── POST /edge/proxy/reply {sid,req_id,body} ────┤       │
    │◀── rewritten response ──────────┤                                                  │       │
```

---

## 2. Activation model (idle cost must be zero)

The edge must **not** hold open long-polls for every node all the time — each held
connection ties up a `ThreadingHTTPServer` worker thread on central (and the mTLS
handshake already runs in the worker thread, see `server.py` `finish_request`
override). So the tunnel is **dormant until a session is requested**:

- Operator clicks "Open web UI" on a device → `POST /api/proxy/session` creates a
  `proxy_sessions` row (org, device_id, node_id, ttl, created_by, status=pending).
- The pending session rides the **next `/report` reply** under a new
  `proxy_sessions` key — same delivery mechanism as `snmp_walks` and the heartbeat
  `update` directive (`api/edge.py:report`, lines 126–131). The edge never accepts
  inbound to learn about it.
- On seeing it, the edge spins up its long-poll workers for that session's TTL, then
  tears them down. **Cold start ≤ one report interval** (show the operator a
  "connecting… up to 60 s" state; after that, interactive latency is good).
- Optional later optimization: also carry the pending-session hint on the heartbeat
  reply if the heartbeat cadence is tighter than `/report`, to shorten cold start.

Sessions auto-expire (`ttl`, default ~10 min, extended on activity) so a forgotten
tab doesn't hold a tunnel open forever.

---

## 3. Wire format

### 3.1 Browser ↔ central (normal HTTP, session-scoped path)

```
GET/POST/... /api/proxy/<sid>/<device-path...>
```

`<sid>` is an opaque, unguessable session id (also the isolation boundary). Central
maps `<sid>` → (org, device_id, node_id) and forwards `<device-path...>`, method,
headers (filtered), and body down the tunnel. Requires a live dashboard session
cookie scoped to that org — **every proxied byte is org-checked**, same as
`_scope_org` everywhere else.

### 3.2 Central → edge (long-poll pickup)

Edge holds `GET /edge/proxy/next?node_id=…&sid=…` (auth: the same edge credential as
`/report` — bearer / node token / mTLS via `_ingest_ok`). Central holds the
connection up to ~25 s. Returns one parked request:

```json
{"req_id": 811, "method": "GET", "path": "/cgi-bin/luci/...",
 "headers": {...filtered...}, "body_b64": null, "device_ip": "10.0.0.2", "port": 80,
 "scheme": "http"}
```

or `204 No Content` on timeout (edge immediately re-polls). Run a small pool
(K = 4–6) of these loops per active session so a page's asset burst has concurrency.

### 3.3 Edge → central (reply upload)

```
POST /edge/proxy/reply
{"org_id","node_id","sid","req_id","status":200,
 "headers":{...}, "body_b64":"...", "truncated":false}
```

Bodies are **base64** (binary-safe: images, gzip, fonts). Server double-bounds the
upload size (like `walk_result`'s `WALK_CAP_*`). Large bodies stream in chunks
(`req_id` + `seq`) rather than one giant JSON — or cap proxied response size and
refuse the rest (a management UI rarely serves >a few MB per asset).

---

## 4. Central-side components

- **`central/proxy.py`** (new): the session registry + request-parking hub. An
  in-memory map `sid → ProxySession`, each holding `asyncio`/`threading` primitives:
  a queue of parked browser requests and a map of `req_id → future` awaiting the
  edge's reply. Pure process-memory (a tunnel is inherently live/stateful — do NOT
  put in-flight requests in SQLite). Only the **session record** (audit/TTL) is
  persisted.
- **New tables** (via `_ensure_columns` pattern is for columns; these are new tables,
  which need nothing — see CLAUDE.md "New tables need nothing"):
  - `proxy_sessions(id sid, org_id, device_id, node_id, created_by, created_at,
    expires_at, status, last_active_at)`
  - `proxy_audit(id, sid, org_id, device_id, user_id, method, path, status, ts)` —
    **every proxied request logged** (this is non-negotiable, see §6).
- **New routes** in `api/__init__.py`:
  - GET: `/edge/proxy/next` (edge long-poll), `/api/proxy/<sid>/*` (browser — needs a
    prefix match, not the exact-path table; handled specially in `server.py`
    `do_GET`/`do_POST` like the static/SSE cases, not the exact-path dict).
  - POST: `/api/proxy/session` (dashboard, `fn(h, user, body)`), `/edge/proxy/reply`
    (edge — added to the `do_POST` edge special-case block alongside
    `/edge/snmp-walk`, since it's edge-credentialed, not session-cookie'd).
- **`api/edge.py:report`**: append pending `proxy_sessions` to the reply dict next to
  `snmp_walks`.
- Handlers live in a new `api/proxy.py` module + route-table rows (follow the
  "adding an endpoint = a function + a table row" rule).

**Transaction rule holds:** parking/forwarding never touches a DB write lock during
the network wait (same as `CentralAlertDispatcher` sends outside the txn).

---

## 5. Edge-side components

- **`ingress/webproxy.py`** (new): a `_ProxyTunnel` background asyncio task, sibling
  to `_DiagWalkRunner`. Activated by the `proxy_sessions` reply key
  (`run_cycle_central_brain` already threads `walk_runner`; add `proxy_tunnel` the
  same way). For each active session it runs K long-poll workers:
  1. `GET /edge/proxy/next` (via the existing `HttpCentralClient` transport, but with
     a longer read timeout than `ship_timeout_s` — long-poll needs ~30 s).
  2. On a request: fetch `http(s)://<device_ip>:<port><path>` with `httpx`
     (`verify=False` — LAN devices have self-signed/junk certs), forwarding method,
     filtered headers, body.
  3. `POST /edge/proxy/reply` with the base64 body.
- **The allow-list invariant carries over, verbatim.** `_DiagWalkRunner.accept`
  already refuses any target IP not in the node's current device list
  (`allowed = {d["ip_address"] for d in devices}`, `main.py:261`). The proxy tunnel
  **must** apply the identical check: central names a `device_id`, the edge resolves
  it to an IP from its own device list, and refuses anything else. **No raw-IP
  proxying, no arbitrary port** — clamp to the device's management IP + a
  configured management port. This is what keeps the edge from becoming a
  general-purpose LAN pivot.
- Runs as a **background task, never inline in the probe cycle** (same rule as SNMP —
  a slow device UI must never stall or slow a probe sweep).
- New `HttpCentralClient` methods: `proxy_next(...)`, `proxy_reply(...)` alongside
  `report`/`heartbeat`/`walk_result` in `runtime/central_client.py`.

---

## 6. Security — the real gate (read before building)

This feature **changes what the edge is**: from a read-only sensor with an explicit
"no lateral-movement primitive, never accepts inbound" posture, to an authenticated
tunnel into customer management LANs, with device credentials transiting central. If
central is compromised, so is every ISP's core gear. That is a large blast-radius
increase, and these controls are **requirements, not nice-to-haves**:

1. **Allow-list only.** Proxy solely to IPs in the node's device list, solely to the
   configured management port(s). Reuse the `_DiagWalkRunner.accept` gate. No
   raw-IP/arbitrary-port path, ever.
2. **Org scoping on every request.** `<sid>` → org, checked against the caller's
   session on every proxied byte. Superadmin `org=None` handled explicitly (don't
   test with `if not org` — see `api/common.py:DENIED` note in CLAUDE.md).
3. **Full audit trail.** `proxy_audit` row per request: who, device, method, path,
   status, ts. Surfaced in the dashboard.
4. **Explicit, expiring, opt-in sessions.** No always-on tunnel. Operator opens a
   session per device; it auto-expires; closing the tab/idle TTL tears it down.
5. **Role-gated.** Restrict who can open a proxy session (operator+; consider
   superadmin-only for a first cut).
6. **Size + rate bounds** server-side on both directions (reuse the `walk_result`
   double-bound philosophy).
7. **Never gated by billing, but DO gate by an org capability flag** (`cfg`-style or
   an org column) so it can be turned off fleet-wide instantly if abused.
8. **Credentials:** the tech logs into the device *through* the tunnel; central
   should avoid storing device UI creds. If we ever cache them, encrypt at rest and
   scope per device — but v1 should pass-through only.

---

## 7. URL rewriting — the annoying engineering part

Serving the UI under `/api/proxy/<sid>/…` breaks device pages that use **absolute**
paths (`/cgi-bin/...`), absolute redirects, and hardcoded links, because they point
back at central's root. Mitigations, in order of reliability:

- **Rewrite `Location:` headers** on 3xx to stay inside the prefix. (Reliable.)
- **Inject `<base href="/api/proxy/<sid>/">`** into HTML `<head>`. (Fixes most
  relative-from-root cases.)
- **Rewrite `Set-Cookie` `Path=`** into the prefix so device sessions bind correctly.
- **Rewrite absolute URLs in HTML/CSS** bodies (`src=/…`, `href=/…`, `url(/…)`).
  (Mostly works; regex-fragile.)
- **JS-constructed absolute URLs** (`fetch('/api/…')`, `location='/x'`) are the part
  that *can't* be reliably rewritten. Some modern OLT SPAs will partially break.

**Cleaner alternative that avoids rewriting entirely:** give each session its own
**hostname** (`<sid>.proxy.hansanet.in`) via a wildcard DNS record + wildcard TLS
cert. Then absolute paths "just work" because the origin is the device. Costs a
wildcard cert and DNS, but removes the whole rewriting mess. **Recommended if we go
past a prototype.**

WebSockets (some newer UIs) aren't carried by a request/response tunnel; scope them
out of v1 and detect+message ("this device's UI uses live sockets, not yet
supported") rather than failing silently.

---

## 8. Dashboard (web/ → central/static/)

- On the device detail panel (`components/device-detail.tsx`, shared by tree + map),
  add an **"Open web UI"** action for devices with a management IP (gated on the
  capability flag + role). Opens the proxied UI in a new tab (or an in-app
  `<iframe>` with a connection-state overlay for the cold-start wait).
- A **session indicator** + "close session" control; a small **audit view**
  (who opened what, when) for owners/superadmin.
- `lib/api.ts` + `lib/types.ts`: `POST /api/proxy/session`, session status types.
- Theme: connection/cold-start state is neutral, not green (matches the
  "resolved-pending-postmortem renders neutral" taste).

---

## 9. Config additions (`config.py`, frozen dataclass, `WISP_*`)

- `WISP_PROXY_ENABLED` (bool, default ON since v0.15.8 — `=0` is the kill switch;
  the org flag + dormant activation are the real gates).
- `WISP_PROXY_MGMT_PORTS` (allowed device ports, default `80,443`).
- `WISP_PROXY_SESSION_TTL_S` (default 600).
- `WISP_PROXY_POLL_HOLD_S` (long-poll hold, default 25).
- `WISP_PROXY_WORKERS` (K concurrent long-polls per session, default 4).
- `WISP_PROXY_MAX_BODY_BYTES` (per-response cap).
- Remember `Config` is **shared edge+central** — grep both before renaming.

---

## 10. Phasing

- **M0 — spike (prove the tunnel): ✅ DONE 2026-07-16.** Round trip proven with an
  automated test harness (stub device); the on-site test against a real switch/OLT
  on the ISP LAN is the remaining manual step (set `WISP_PROXY_ENABLED=1` +
  `WISP_PROXY_MGMT_PORTS` on that edge, open a session, browse). Confirms the
  long-poll channel + allow-list gate end to end.
- **M1 — sessions + security: ✅ DONE 2026-07-16.** `proxy_sessions`, allow-list
  gate, org scoping, audit log, TTL, capability flag, dormant-until-session
  activation, best-effort rewriting. Still path-prefix.
- **M2 — rewriting hardening OR wildcard-host mode:** pick the wildcard-hostname
  approach if M1's rewriting proves too fragile on the real fleet (C-Data/DBC EPON
  UIs are the likely pain).
- **M3 — dashboard polish:** in-app iframe, session indicator, audit view,
  cold-start UX.
- **Out of scope v1:** WebSockets, storing device credentials, file up/download
  larger than the body cap.

---

## 11. Testing

- `unit/test_webproxy` — edge tunnel: allow-list refusal (target not in device
  list), header/body round-trip, base64 correctness, size cap. Inject a fake device
  HTTP server + a fake central client (follow `test_probers`/`walker` doubles).
- `integration/test_central_proxy` — session create → report reply carries it →
  parked request released to a fake edge long-poll → reply forwarded to browser;
  org-scope refusal; audit row written; TTL expiry. Inject a recording notifier and a
  fake edge, no real network (house pattern).
- Manual: `tsc --noEmit`, `npm run build`, Playwright against a seeded DB + a stub
  device UI.

---

## 12. Known gaps / honest caveats

- Path-prefix rewriting will not fully tame JS-built absolute URLs; wildcard-host is
  the real fix.
- Long-polls consume central worker threads while a session is live — fine for a
  handful of concurrent techs, not for hundreds of always-open tunnels. The
  dormant-until-requested model is what keeps this bounded; don't make it always-on.
- Cold start is up to one report interval unless we add the heartbeat-reply hint.
- This is a genuine threat-model change for the edge. The allow-list + audit +
  opt-in-session + capability-flag controls are the price of admission; shipping any
  subset of them is not acceptable.
