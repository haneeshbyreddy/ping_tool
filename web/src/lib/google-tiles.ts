// Google Map Tiles API plumbing for the map view's Google basemaps.
//
// The key is org-scoped (orgs.google_maps_key), referrer-restricted, and ships
// to signed-in browsers BY DESIGN — central never talks to Google; the browser
// does the createSession/tile/viewport fetches itself, same trust model as the
// CARTO/Esri tile CDNs. Session tokens (~2-week expiry) are cached per map
// type in localStorage and recreated when tiles start failing.

export type GoogleMapType = "roadmap" | "satellite"

// HiDPI: scaleFactor2x + highDpi returns 512px tiles for the SAME coverage;
// Leaflet still lays them out at 256 CSS px, so a dense display (retina, or
// Windows at 125–150% scaling) gets its full pixel budget instead of an
// upscaled 256 raster — that upscale is the "Google view looks blurry"
// complaint. dpr 1 keeps plain tiles: downscaling 512→256 softens label
// hinting rather than sharpening it. The session token encodes the scale, so
// the cache key carries it too.
const hiDpi = () => typeof window !== "undefined" && window.devicePixelRatio > 1

// THE CEILING RULES (2026-08-02) — read this before touching any hex below.
//
// Dulled three times in one day at the operator's request; the third ask was
// "even more dull, just slightly visible enough to grasp it". Both style arrays
// are now solved against TWO ceilings, because geometry and writing fail
// differently and one flat number can't serve both:
//
//   GEOMETRY  ≤ --border, the app's own hairline (1.35:1 dark, 1.28:1 light).
//             Roads become exactly as loud as the edge of a panel: structure you
//             can trace when you follow it, never something that competes.
//   LABELS    ≤ ~2.05:1 dark, ~1.75:1 light. This is a LEGIBILITY FLOOR, not a
//             taste choice — below it place names stop resolving as text at all,
//             and a name nobody can read is worse than no name, because it still
//             costs ink and attention. If the map should be quieter than this,
//             the honest control is the EXISTING Layers → "Google labels"
//             switch, which removes the writing outright. Don't chase it with
//             colour past this point.
//
// Where that leaves us, measured (previous two passes in brackets):
//
//                loudest LABEL              loudest GEOMETRY   vs destructive
//   dark         2.06:1  [3.04 ← 4.60]      1.30:1  [1.44]     −66%  [−50, −25]
//   light        1.74:1  [2.39 ← 3.81]      1.18:1  [1.24]     −65%  [−52, −24]
//
// The earlier rule — rank under --ghost-foreground, the app's quietest text step
// — is now comfortably satisfied rather than binding, and the one before that
// ("rank under the status tones") was never the real constraint at all: what
// exposed it was --map-link, the colour EVERY cable on this map is drawn in,
// sitting at 4.75:1 while a road NAME sat at 4.60. A 3% gap between Google's
// writing and our own network drawing is not a hierarchy.
//
// Every ladder was re-solved, never clipped at the top: values are scaled in
// LINEAR RGB, which preserves hue and saturation exactly and moves only the
// level, so the internal ordering — water < base < park < poi < road < locality
// < highway — survives each pass, flatter and lower each time. The top is
// compressed hardest on purpose: across the three passes dark highway labels
// went 4.60 → 3.04 → 2.06 while road labels went 3.41 → 2.60 → 1.84, because a
// route number is not more important than a town name and it was being drawn as
// though it were.
//
// Road GEOMETRY is dulled AGAINST THE GROUND, never against its own casing —
// that fill/casing pair is what actually draws a road. Dulling road-vs-ground
// makes the network recede; dulling road-vs-casing makes it DISSOLVE, and a
// light-mode ground at #f2f2f4 already proved once that a dissolved network
// reads as a broken map rather than a quiet one. That is also why light mode's
// near-white road fill stops at #f9f9fc and only its hard highway casing came
// down again this pass: on a light ground the ribbon IS the road.
//
// So: DO NOT nudge one value here. These are a solved set. If any of them moves,
// re-run the whole ratio table against BOTH ceilings — --border for geometry,
// the label floor for text — and the status tones after that.
//
// ---------------------------------------------------------------------------
//
// Dark roadmap: Google's published night-mode array with its GEOMETRY intact and
// its LABEL TEXT dimmed (2026-07-22 — stock shipped first, operator found the
// text too contrasty within the hour; dimmed twice more since, 08-01 and 08-02).
//
// Stock's labels measured 5.36–9.29:1 against the navy they sat on, i.e. louder
// than every status tone — the basemap's road names were louder than a device
// being down, which inverts the one rule this map has. "Matching Google's
// sample" has never been the goal.
//
// Deliberately NEUTRAL, not tan: the stock gold (#f3d19c highways, #d59563
// localities) sat right on top of --warning amber, so a glance couldn't separate
// "major road" from "degraded". labels.text.stroke stays equal to the geometry
// it sits on — a stroke matching the background is what keeps DIM text crisp
// without raising its fill contrast, and it is what makes values this low
// legible at all.
//
// GROUND is #15181d (2026-08-01): the app's slate family, seated between
// --background (#0c0e12) and --card (#1c1f24) — deeper than a card, because a
// backdrop is something you look INTO, but above the canvas so the map area is
// still distinguishable from the chrome around it. It was Google's night navy
// #242f3e, which measured LIGHTER than --popover: the map's BACKDROP was sitting
// above the highest surface in the product, hue-shifted blue, inside a
// warm-slate near-black app.
//
// Three mechanism constraints. (1) `styles` is valid ONLY for mapType roadmap —
// satellite is photography and the API ignores styling on it, so there is no
// dark satellite and the Layers menu must not imply one. (2) An OVERSIZED style
// array is dropped SILENTLY: "if your style array exceeds the maximum number of
// characters then no style is applied" — no error, tiles just come back light.
// Google doesn't publish the limit; ours is ~1.5 KB, so leave headroom and
// eyeball a real tile after editing. (3) Absolute `color` throughout, per
// Google's own advice: the relative stylers (hue/saturation/lightness) drift
// whenever they change the base map.
const NIGHT_STYLE = [
  { elementType: "geometry", stylers: [{ color: "#15181d" }] },
  { elementType: "labels.text.stroke", stylers: [{ color: "#15181d" }] },
  { elementType: "labels.text.fill", stylers: [{ color: "#30353d" }] },
  { featureType: "administrative.locality", elementType: "labels.text.fill", stylers: [{ color: "#454a51" }] },
  { featureType: "poi", elementType: "labels.text.fill", stylers: [{ color: "#373c42" }] },
  { featureType: "poi.park", elementType: "geometry", stylers: [{ color: "#141d19" }] },
  { featureType: "poi.park", elementType: "labels.text.fill", stylers: [{ color: "#2d3d31" }] },
  { featureType: "road", elementType: "geometry", stylers: [{ color: "#24262d" }] },
  // The road CASING is deliberately NOT dulled with the fill. At these levels it
  // is the only thing giving a road an edge — dull both together and the network
  // stops being traceable at all, which is dissolution, not quiet.
  { featureType: "road", elementType: "geometry.stroke", stylers: [{ color: "#0f1114" }] },
  { featureType: "road", elementType: "labels.text.fill", stylers: [{ color: "#40454c" }] },
  // The highway FILL. It kept Google's stock tan #746855 until 2026-08-01 —
  // nearly double the loudest basemap geometry, in the amber hue family, at the
  // scale of a river, on a screen whose one rule is "status tones are loudest".
  // Neutralised then (1.73:1), then 1.44, now 1.30 — right under --border, so a
  // trunk road is at most as loud as a panel edge. It stays a clear step above
  // ordinary road (1.18:1) so the hierarchy a crew navigates by survives, and
  // cool, so nothing on the basemap can be mistaken for --warning. Re-measure,
  // don't eyeball, if it moves again.
  { featureType: "road.highway", elementType: "geometry", stylers: [{ color: "#292e36" }] },
  { featureType: "road.highway", elementType: "geometry.stroke", stylers: [{ color: "#0f1114" }] },
  { featureType: "road.highway", elementType: "labels.text.fill", stylers: [{ color: "#484c53" }] },
  { featureType: "transit", elementType: "geometry", stylers: [{ color: "#1d2026" }] },
  { featureType: "transit.station", elementType: "labels.text.fill", stylers: [{ color: "#373c42" }] },
  // Water is the DARKEST thing on the map, not a blue slab — it was #17263c, a
  // saturated navy that on the old ground read as a lit surface. Now it sits
  // just under the land (1.09:1) and carries its meaning by HUE, which is what
  // a night map should do: you notice a lake because it is a hole, not a glow.
  { featureType: "water", elementType: "geometry", stylers: [{ color: "#0a0e15" }] },
  { featureType: "water", elementType: "labels.text.fill", stylers: [{ color: "#232830" }] },
  { featureType: "water", elementType: "labels.text.stroke", stylers: [{ color: "#0a0e15" }] },
]

// Light roadmap, styled for the first time (2026-08-01). Until now light mode
// sent NO style array at all and got Google's stock map: beige land, a big pale
// gold landuse wash, and CYAN water — three saturated colours under an app
// whose light theme is neutral grey and white. Same complaint as the night
// navy, arriving from the other direction: the map looked like a different
// product embedded in the page.
//
// The rule that had to be re-derived here rather than copied: on a LIGHT ground
// the basemap's own labels are dark ink, so they are automatically
// high-contrast — stock locality text measured 7.37:1 against this ground while
// --destructive manages 4.99:1. In other words light mode broke the hierarchy
// WORSE than dark mode ever did, silently, because nobody had measured it. The
// loudest label ran 3.81:1 after that first pass, 2.39 after the second and
// 1.74 now — at the label legibility floor named at the top of this file.
//
// DULLING GOES THE OTHER WAY HERE and it is easy to get backwards: on a light
// ground, quieter means LIGHTER. Every label hex below moved UP the ramp toward
// the ground while the dark array's moved DOWN toward its own.
//
// Ground is #eaeaef — just under --muted (#eeeef2), the app's recessed-well
// tone, with --card white above it. So the map is the DEEPEST surface in light
// mode exactly as it is in dark. Roads are defined by their CASING rather than
// by a fill hue, which is what keeps a road network legible on grey without
// adding a colour — and it is why the fill could come off pure white (#ffffff,
// the single brightest object on the map, brighter than any pin's ring) down to
// #f9f9fc without the network thinning: the fill/casing pair still separates at
// 1.30:1. Don't chase that further by lightening the CASINGS. A first cut at a
// #f2f2f4 ground put white roads at 1.12:1 against it and the whole network read
// as a ghost — the failure mode on this side is dissolution, not glare.
// Water carries its meaning by hue at 1.13:1 — never a cyan slab.
const DAY_STYLE = [
  { elementType: "geometry", stylers: [{ color: "#eaeaef" }] },
  { elementType: "labels.text.stroke", stylers: [{ color: "#eaeaef" }] },
  { elementType: "labels.text.fill", stylers: [{ color: "#c2c8d1" }] },
  { featureType: "administrative.locality", elementType: "labels.text.fill", stylers: [{ color: "#acb4c0" }] },
  { featureType: "poi", elementType: "labels.text.fill", stylers: [{ color: "#bec5cf" }] },
  { featureType: "poi.park", elementType: "geometry", stylers: [{ color: "#dfe7df" }] },
  { featureType: "poi.park", elementType: "labels.text.fill", stylers: [{ color: "#b5cab7" }] },
  // The road FILL and its CASING both stay put on this side. On a light ground
  // the near-white ribbon IS the road — dulling it toward the ground removes the
  // network rather than quieting it, which is the one failure mode light mode
  // has already demonstrated. Everything dulled here is writing, plus the one
  // hard edge below.
  { featureType: "road", elementType: "geometry", stylers: [{ color: "#f9f9fc" }] },
  { featureType: "road", elementType: "geometry.stroke", stylers: [{ color: "#dcdce3" }] },
  { featureType: "road", elementType: "labels.text.fill", stylers: [{ color: "#b5bcc6" }] },
  { featureType: "road.highway", elementType: "geometry", stylers: [{ color: "#f9f9fc" }] },
  // The highway CASING was the hard edge that made a trunk road the loudest
  // GEOMETRY in light mode (1.37:1 against the ground, where an ordinary road
  // manages 1.14). Softened to 1.24 and now 1.18 — under --border (1.28), so a
  // trunk road is at most as loud as a panel edge, while still just clear of
  // ordinary road (1.14) so the hierarchy survives.
  { featureType: "road.highway", elementType: "geometry.stroke", stylers: [{ color: "#d8d8e2" }] },
  { featureType: "road.highway", elementType: "labels.text.fill", stylers: [{ color: "#afb6c1" }] },
  { featureType: "transit", elementType: "geometry", stylers: [{ color: "#e0e0e7" }] },
  { featureType: "transit.station", elementType: "labels.text.fill", stylers: [{ color: "#bec5cf" }] },
  { featureType: "water", elementType: "geometry", stylers: [{ color: "#d7dee6" }] },
  { featureType: "water", elementType: "labels.text.fill", stylers: [{ color: "#c2ccd9" }] },
  { featureType: "water", elementType: "labels.text.stroke", stylers: [{ color: "#d7dee6" }] },
]

// Google's own labels and POI markers, switched off in ONE rule.
//
// A dense town's roadmap ships more Google pins than we draw — restaurants,
// shops, bus stops — each an icon plus its name, and on a wall map they compete
// directly with the device pins and the cable routes that are the entire point.
// `elementType: "labels"` with no featureType covers every feature's text AND
// its icon, which is what makes this one line rather than a per-feature list:
// a POI marker is a label, not geometry.
//
// GEOMETRY IS DELIBERATELY LEFT ALONE. Switching `poi` off wholesale would take
// park fills and building footprints with it, and those are what a crew
// navigates by once the names are gone — a blank map is not the ask. Roads,
// water and parks stay; only the writing goes.
const LABELS_OFF = [
  { elementType: "labels", stylers: [{ visibility: "off" }] },
]

// Google's own PIN-SHAPED marks, off ALWAYS on roadmap (2026-08-01).
//
// This is the loudest thing the basemap was doing and it survived every earlier
// pass because it is not text and not geometry: a POI icon is a white disc with
// a dark ring and a glyph in it, which is — to the pixel — the visual grammar
// of a device pin. A rural viewport here renders five temple discs against ONE
// OLT, and they read as five bright somethings that turn out to mean nothing to
// an ISP. In light mode they ARE white circles on pale ground, i.e. louder than
// any pin we draw. Whatever else competes for attention on this map, the one
// thing that must not is a mark the operator can never act on.
//
// SEPARATE from LABELS_OFF, and that separation is the whole point. The Layers
// switch is all-or-nothing by design (`elementType: "labels"` deliberately
// covers text AND icon in one rule), so it could only ever trade the discs for
// a map with no place names — and the names are what a crew navigates by once
// the imagery is off. `labels.icon` splits the two: the writing stays, the
// competing marks go. That is why this is not simply "default the Layers toggle
// to off".
//
// HIGHWAY SHIELDS GO TOO, which was NOT the first call. They were kept once as
// "road furniture" — shield-shaped and sitting on the line they name — and that
// argument died on a real tile: one town viewport renders eight of them, they
// are YELLOW, and yellow is --warning. So the basemap was scattering
// warning-coloured badges over a screen whose entire job is to make one amber
// chip findable. Repetition finished the case: the same route number is stamped
// five times along one road, which is wayfinding for a driver deciding a turn
// and pure noise for an operator reading a network.
//
// What survives is the road NAME text and the geometry weight — a trunk road
// still reads as a trunk road, and "Sai Pratap Nagar Main Rd" still labels
// itself. Only the numbered badges go. If a crew ever wants route numbers back
// this is one line.
//
// Geometry is untouched here as everywhere: park fills, water and building
// footprints all survive — a blank map is not the ask.
const ICONS_OFF = [
  { featureType: "poi", elementType: "labels.icon", stylers: [{ visibility: "off" }] },
  { featureType: "transit", elementType: "labels.icon", stylers: [{ visibility: "off" }] },
  { featureType: "road", elementType: "labels.icon", stylers: [{ visibility: "off" }] },
]

/** The style array for this map type + theme + label choice, or null for an
 *  unstyled session.
 *
 *  SATELLITE NEVER GETS ONE — the Tile API ignores `styles` on photography, so
 *  there is no dark satellite and no label-stripped satellite. It needs neither:
 *  a `satellite` session carries no labels in the first place (labels on imagery
 *  are an explicit `layerTypes: ["layerRoadmap"]` overlay we don't request). The
 *  Layers menu must not offer a switch that would do nothing there. */
function styleArray(t: GoogleMapType, dark: boolean, labels: boolean) {
  if (t !== "roadmap") return null
  // ICONS_OFF is unconditional; LABELS_OFF goes LAST, because later rules win
  // and it has to override the night array's own label colours rather than be
  // overridden by them.
  return [...ICONS_OFF, ...(dark ? NIGHT_STYLE : DAY_STYLE), ...(labels ? [] : LABELS_OFF)]
}

/** Which styled variant this is, for the cache key. "" = unstyled (satellite
 *  only). `n` = night, `p` = plain (labels stripped); both can apply at once,
 *  and `b` is the base roadmap — which is STYLED now too, since ICONS_OFF
 *  applies to every roadmap session.
 *
 *  That fallback is load-bearing, not tidiness: a light, labelled roadmap used
 *  to be the one unstyled variant and so keyed WITHOUT a `:s…` suffix. Leaving
 *  it that way would have let every browser holding such a token keep serving
 *  POI-covered tiles for the fortnight until it expired — the exact silent-stale
 *  failure the style hash exists to prevent, and invisible to whoever shipped
 *  the change. */
const variantOf = (t: GoogleMapType, dark: boolean, labels: boolean): string =>
  t !== "roadmap" ? "" : `${dark ? "n" : ""}${labels ? "" : "p"}` || "b"

// Fingerprint of the style array, so EDITING the palette busts the cache by
// itself. A session token bakes in the style it was created with and lives ~2
// weeks, so without this a dimmed palette would keep serving the OLD tiles to
// everyone who had already loaded the map — invisible to whoever made the edit
// (their own token is fresh) and unreportable by anyone else ("it just didn't
// change"). Deriving it beats a hand-bumped version constant for the obvious
// reason: nobody remembers to bump it. Cheap 32-bit string hash; this guards a
// cache key, it is not security.
const _tags = new Map<string, string>()
function styleTag(t: GoogleMapType, dark: boolean, labels: boolean): string {
  const v = variantOf(t, dark, labels)
  if (!v) return ""
  let tag = _tags.get(v)
  if (!tag) {
    const s = JSON.stringify(styleArray(t, dark, labels))
    let h = 0
    for (let i = 0; i < s.length; i++) h = (Math.imul(31, h) + s.charCodeAt(i)) | 0
    tag = (h >>> 0).toString(36)
    _tags.set(v, tag)
  }
  return tag
}

// The session token encodes the style and the scale, so the cache key carries
// BOTH. Miss the theme half and a flip silently reuses the light session for up
// to two weeks; miss the style half and a palette edit never reaches a browser
// that already has a token. The VARIANT rides the key beside the hash for the
// same reason at a coarser grain: night and label-stripped are different
// sessions, and each has to be able to expire without evicting the other.
const sessionKey = (t: GoogleMapType, dark: boolean, labels: boolean) => {
  const v = variantOf(t, dark, labels)
  return `wisp:map:gsession:${t}:${hiDpi() ? "2x" : "1x"}`
    + (v ? `:s${v}-${styleTag(t, dark, labels)}` : "")
}

/** Drop styled tokens minted from a SUPERSEDED revision of the SAME variant, so
    editing the palette doesn't leave an orphan per revision sitting in
    localStorage forever. Scoped to the variant on purpose — pruning across
    variants would make every theme or label flip pay for a fresh createSession.
    Legacy `:night-` keys (before variants existed) are swept unconditionally;
    nothing mints them any more. */
function pruneStaleStyledSessions(keep: string, variant: string): void {
  const prefix = `:s${variant}-`
  try {
    for (const k of Object.keys(localStorage))
      if (k.startsWith("wisp:map:gsession:") && k !== keep
          && (k.includes(prefix) || k.includes(":night-")))
        localStorage.removeItem(k)
  } catch {
    /* private mode — nothing cached, nothing to prune */
  }
}

interface CachedSession {
  session: string
  expiry: number // unix seconds, from Google's createSession reply
}

export function loadGoogleSession(
  mapType: GoogleMapType, dark = false, labels = true,
): string | null {
  try {
    const raw = localStorage.getItem(sessionKey(mapType, dark, labels))
    if (!raw) return null
    const v = JSON.parse(raw) as CachedSession
    // 10-minute guard so a token can't expire mid-pan
    return v.session && Date.now() / 1000 < v.expiry - 600 ? v.session : null
  } catch {
    return null
  }
}

export function clearGoogleSession(
  mapType: GoogleMapType, dark = false, labels = true,
): void {
  try {
    localStorage.removeItem(sessionKey(mapType, dark, labels))
  } catch {
    /* noop */
  }
}

export async function createGoogleSession(
  apiKey: string, mapType: GoogleMapType, dark = false, labels = true,
): Promise<string> {
  const cached = loadGoogleSession(mapType, dark, labels)
  if (cached) return cached
  const styles = styleArray(mapType, dark, labels)
  const res = await fetch(
    `https://tile.googleapis.com/v1/createSession?key=${encodeURIComponent(apiKey)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // language/region shape label choices; the org key's billing is India-based
      body: JSON.stringify({
        mapType, language: "en-IN", region: "IN",
        ...(hiDpi() ? { scale: "scaleFactor2x", highDpi: true } : {}),
        ...(styles ? { styles } : {}),
      }),
    },
  )
  if (!res.ok) throw new Error(`createSession replied ${res.status}`)
  const data = (await res.json()) as { session?: string; expiry?: string }
  if (!data.session) throw new Error("createSession returned no session token")
  const key = sessionKey(mapType, dark, labels)
  if (styles) pruneStaleStyledSessions(key, variantOf(mapType, dark, labels))
  try {
    localStorage.setItem(
      key,
      JSON.stringify({ session: data.session, expiry: Number(data.expiry) || 0 }),
    )
  } catch {
    /* private mode — the session just won't persist */
  }
  return data.session
}

export function googleTileUrl(session: string, apiKey: string): string {
  return `https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session=${encodeURIComponent(session)}&key=${encodeURIComponent(apiKey)}`
}

// ToS-required attribution: the viewport endpoint returns the copyright line
// for what's currently on screen. Callers debounce; this is per-move, not
// per-tile.
export async function fetchGoogleAttribution(
  session: string,
  apiKey: string,
  zoom: number,
  b: { north: number; south: number; east: number; west: number },
): Promise<string> {
  const params = new URLSearchParams({
    session,
    key: apiKey,
    zoom: String(Math.max(0, Math.round(zoom))),
    north: String(b.north),
    south: String(b.south),
    east: String(b.east),
    west: String(b.west),
  })
  const res = await fetch(`https://tile.googleapis.com/tile/v1/viewport?${params.toString()}`)
  if (!res.ok) throw new Error(`viewport replied ${res.status}`)
  const data = (await res.json()) as { copyright?: string }
  return data.copyright || "Map data ©Google"
}
