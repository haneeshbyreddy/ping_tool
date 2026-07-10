# Map view — veteran-ISP improvements

State as of 2026-07-10 (second batch). Everything below the wishlist is code
complete, `tsc`/build clean, backend suite green (543 OK), SPA rebuilt into
`central/static/`, and **visually verified** via a Playwright pass against the
seeded scratch server (login → map → trouble cycle → low-zoom labels → fan-out →
impact rings → PON spokes → spoke-click → coords round-trip → fullscreen).
Deploying still needs `sudo systemctl restart wisp-central` — the FIRST batch
changed the backend (outage join in `list_org_devices`) and that restart hasn't
happened yet; this batch is SPA-only on top of it.

## Verified (Playwright, seeded scratch server)

First batch — all confirmed working:

1. **Jump to the problem** — trouble counter cycles to each trouble pin and
   opens its panel (verified: flies to ap-lake-02, "for 47m" duration shown).
2. **Outage duration** — pin hover "down for 43m", live duration in the panel.
3. **Blast radius** — downstream rings + brightened links + "Feeds N downstream"
   (verified: 5 impact rings off core-router).
4. **Zoom-aware labels** — below z12 only trouble + selected keep labels.
5. **Co-located pin fan-out** — same-cabinet devices spread ~15 m, display-only.
6. **Trouble-only filter**, 7. **coords row** (copy / typed GPS entry round-trip
   verified), 8. **Navigate**, 9. **link distance**, 10. **fullscreen**,
   11. **maintenance pins** — all as described in the first batch.

Fix found during verification: the map control buttons (fit/locate/fullscreen)
sat UNDER the open device panel on desktop and were unclickable — they now
slide left of the panel (`md:right-[calc(380px+1.5rem)]`) while a device is
selected, Google-Maps style.

## PON on the map — Phase 1 + 2 BUILT (2026-07-10)

- **Phase 1 — OLT pin optical ring**: amber (`onus_warn`) / red (`onus_crit`)
  ring around an OLT's pin dot (`.wisp-pin--optic-warn/crit`, a `::after` so it
  stacks with the selection/impact box-shadow rings). Suppressed when the OLT is
  in maintenance, hard-down (the red pulse owns the pin), or the optics walk is
  stale (`isFresh(optics_updated_at)`, 900 s). Hover title says "N ONUs weak
  signal".
- **Phase 2 — PON radial overlay**: selecting a placed optical OLT fetches
  `/api/inventory/optics` (same react-query key as the Optical tab — one fetch)
  and fans out one spoke per ONU: length = `distance_m` in true map meters
  (60 m floor so ends don't hide under the pin; median-of-known fallback when
  unranged), angle = even spread stable-sorted by pon_port/onu_id. Colors: ok
  faint green, warn amber, crit red; LOS red-dashed, offline muted-dashed,
  dying_gasp pulsing (`.wisp-spoke--gasp`). Capped at 64 spokes — over the cap
  trouble outranks health and a "+N more" pill (click → Optical tab) shows the
  remainder. Spoke-end dots are cached divIcons; clicking one opens the device
  panel's Optical tab with THAT ONU's PON group open and the row highlighted +
  scrolled into view (`focusOnuId` threaded map-page → DeviceDetail →
  OpticalPanel; offline/LOS rows are surfaced even though they have no Rx).
  Verified end-to-end: 80-ONU seed → 64 spokes + "+16 more", crit spoke click
  landed on cust-0-1-09 · -28.9 dBm · 0.74 km, gasp spoke pulses.
- **Leaflet gotcha baked into the code**: `pathOptions.className` never reaches
  the SVG (react-leaflet applies pathOptions via `setStyle`, which ignores
  `className`) — spoke class rides as a TOP-LEVEL `<Polyline className>` prop
  and the key includes the tone so a state change remounts the path.
- **Phase 3 — manual ONU placement (real wires): still not built, by design.**
  `lat/lng` on `onu_optics` + placement flow; bonus diagnostic (ranged distance
  must be ≥ straight-line). Only worth it if Phase 2 proves insufficient in the
  field.

Perf note: divIcons (pins, spoke ends, "+N more") are all cached by html string
(`_iconCache`) because `useNow()` re-renders every second — without the cache
every marker's DOM node is swapped each tick and the down-pulse restarts.

## Future wishlist (deliberately not built yet)

- **KML/CSV export** of placed devices for planning tools (client-side blob, ~30
  lines).
- **Legend** — tiny collapsible key for pin colors/rings (maybe unnecessary if the
  UI stays self-evident).
- **Measure tool** — click-two-points ruler for link planning.
- **Link metadata** — fiber vs wireless styling needs a `kind` on links/devices;
  schema change, decide if worth it.
- **Marker clustering** — only needed past ~200 placed devices; revisit with
  leaflet.markercluster then, not before.
- **Outage-history heat** — tint pins by 7-day downtime (reliability data already
  exists at `/api/analytics`).
- **Probe coverage view** — color pins by `assigned_node_id` to spot coverage gaps.
- **Auto-follow trouble** — optional NOC-wall mode where a fresh DOWN pans the map
  there automatically (SSE already delivers the trigger).
