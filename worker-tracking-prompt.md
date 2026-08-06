# Next-session prompt — worker location tracking via Traccar Client

Build live worker location tracking. Workers run **Traccar Client** (free, open
source, Android + iOS) on their phones; it POSTs fixes to central; the owner sees
everyone on `/map`. **No APK work at all** — the `wisp-field-app` Capacitor shell
stays a pure webview and is not touched by this task.

Decisions already made with the operator (2026-08-01) — do not relitigate:

- **Traccar Client, not our own APK.** Location was the only reason left to write
  native code (push was declined — the WhatsApp bot already delivers assignments
  with a working [✅ I'm on it] button). Off-the-shelf gets years of Doze/OEM
  battery tuning we would not match, and iOS for free later.
- **On-shift only.** The tracker app's own ON/OFF switch is the real toggle —
  when it is off, nothing transmits. Do **not** build "always transmit, discard
  off-shift server-side": receiving a worker's evening and choosing not to store
  it is a much worse promise than not receiving it.
- **Plus an explicit Start/End shift button in the web app**, for the record. Two
  taps is accepted deliberately: when a worker marks on-shift and no fixes
  arrive, that gap IS the "OEM killed the service" alarm. The discrepancy is a
  feature, not redundancy.
- **Live position + short trail, 7-day retention** (not 30) — enough to answer
  "did he reach the site" and "what route did the van take today" without
  accumulating a movement archive of staff.

## 1 — Ingest

Traccar Client speaks the **OsmAnd protocol**: an HTTP request carrying
`id, lat, lon, timestamp, speed, bearing, altitude, accuracy, batt` in the
**query string**. Accept **both GET and POST**, and read params from the query
string *or* a form-encoded body — client builds differ and a fix silently
dropped because we only handled one verb is the worst possible failure here.

Route: **`POST|GET /field/track`** — a PUBLIC, machine-credentialed route, so it
belongs beside `/whatsapp/webhook` in `server.py`'s `do_GET`/`do_POST`, handled
BEFORE the session gate. It is **not** an `/api/*` route and must not go in
`api/__init__.py`'s tables or the `_WORKER_GET`/`_WORKER_POST` allowlists —
those are for session-authenticated dashboard calls and the tracker has no
cookie. Same category as `/report` and `/edge/snmp-walk`.

**Auth = a per-worker token carried in Traccar's `id` field.** This keeps the
server URL identical for every worker (one string to put on screen and in a QR)
while identity stays per-person. Reuse the node-token pattern exactly — see
`store_fleet.py:60`: store **only a SHA-256 hash**, show the plaintext once,
rotatable only, never recoverable. Resolve token → `(org_id, user_id)`; an
unknown token is a flat 401 and writes nothing.

Refusals (all of them — a tracking table that accepts junk is worse than empty):

- accuracy above a sane ceiling (a 2 km "fix" is noise, not a position)
- timestamps far outside now, in either direction
- lat/lng out of range, or the pair incomplete
- a per-token rate cap, so a looping client cannot hammer the DB

Do **not** log the query string at info level — the token rides in it. Central's
`log_message` is already `log.debug`; keep it that way.

Billing: leave this ungated, consistent with edge ingest. A lapsed bill must not
silently stop recording where staff are.

## 2 — Storage

```
worker_shifts     org_id, user_id, started_at, ended_at
worker_locations  org_id, user_id, ts, lat, lng, accuracy_m,
                  speed_mps, heading, battery_pct
```

New tables need no migration; any new COLUMN on an existing table needs
`_ensure_columns` in `CentralStore.__init__` or an existing `central.db` keeps
the old schema. Store methods go in the matching mixin (`store_fleet.py` or a
new `store_field.py` composed into `store.py`) — not in `store.py` itself.

`worker_locations` is append-only and **pruned to 7 days**, daily, the way
`rollup.py` already prunes hourly buckets. Do not ship this without the prune:
`data/releases/` is the standing example of a directory nothing prunes, and the
box has ~4.6 GB free.

Sizing sanity: at a 90 s cadence a full crew is roughly 6 MB/month. This is not
a data-volume problem; it is a retention-policy one.

## 3 — Dashboard API

- `POST /api/field/shift` — start/end own shift. Worker-writable, so add it to
  `_WORKER_POST`. `org_id` and `user_id` come from the SESSION, never the body.
- `GET /api/field/workers` — owner-only (not in `_WORKER_GET`): latest fix per
  worker + today's trail + shift state + staleness.

## 4 — Map layer

Add a **Workers** layer to the Layers popover in `routes/map-page.tsx`, following
the Subscribers layer's discipline exactly:

- **opt-in, remembered in localStorage** (mirror `REF_ONUS_KEY` around line 190)
- **its own mark**, not a device pin shape
- **stacked BELOW every device pin** — a worker dot must never outshout a device
  that is down
- **out of the clustering pass** — a site badge mixing staff with plant would
  count nonsense
- today's trail as a polyline, `interactive={false}` like every other topology
  polyline

New map logic goes in `web/src/map/` (e.g. `workers.ts`), not back into
`map-page.tsx`.

**Four states, rendered differently — this is the honesty rule and it is the
part most likely to be got wrong:**

| state | meaning |
|---|---|
| on shift, fix fresh | here, now |
| on shift, gone quiet | phone dead, no signal, or OEM killed the service |
| shift ended | went home — not a fault |
| never reported | set up but never worked |

Collapsing any two of these makes the map lie. "Last known 40 minutes ago" must
not look like "here now".

## 5 — Owner setup instructions in the web UI

Add a **"Location tracking"** panel to Settings. It belongs in the section that
already holds `users` and `assignments` (`routes/settings-page.tsx`, `SECTIONS`,
the one gated `!!c.org && c.canWrite`) — same audience, same job.

The panel must carry, per worker account:

- the **server URL**, constant for everyone: `https://hansanet.in/field/track`
- the worker's **identifier token**, with copy + rotate. Plaintext shown once on
  issue; thereafter only "issued <date>" and Rotate — same contract as node
  tokens, and say so on screen.
- a **QR code** encoding the URL + token, so a phone is provisioned by scanning
  rather than typing.
- **recommended Traccar Client settings**, spelled out because they are the
  duty cycle we designed and the defaults are not it:
  - Frequency **90 s**, Distance **30 m** (whichever comes first)
  - Accuracy **High**
  - **Offline buffering ON** — the crew drives through dead zones and this is
    what makes a fix survive one
- the **OEM warning**, prominently: on Xiaomi / Realme / Vivo / Oppo the
  autostart manager kills background services silently. Link or spell out the
  autostart-whitelist steps. This is the single most likely reason tracking will
  appear broken, and no server-side code can fix it.
- one line stating that **the app's ON/OFF switch is the shift** — when it is
  off, the phone sends nothing.

Write the instructions as steps an owner reads out to a worker over the phone.
Not a paragraph.

## Constraints

- Central is **pure stdlib** — no new dependencies for the ingest path. Generate
  the QR client-side in the SPA rather than adding a Python QR library.
- Follow the repo's honesty conventions throughout (`CLAUDE.md`): state that is
  frozen must look frozen, and "nothing is wrong" must never render like
  "nothing is measured".
- Tests: `integration/test_central_field.py` for the ingest refusals, token
  auth, org scoping, shift start/end and the prune; plus the worker-allowlist
  sweep that already exists for new routes.
- Run the suite with **`.venv/bin/python -m unittest discover -s tests`** — never
  bare `python3`, which lacks `httpx` and fakes ~12 proxy/edge failures.
- Verify the SPA with `tsc -b` (not `tsc --noEmit`, which checks nothing here)
  and `npm run build` from inside `web/`.
- Deploy is a **central restart**; the SPA build alone is not enough.

## Verify before rolling out

Install Traccar Client on **one Xiaomi or Realme handset** and leave it running a
full working day before the fleet gets it. If it survives that, it will survive
the rest; if it does not, no amount of server work fixes it and we want to know
on day one.
