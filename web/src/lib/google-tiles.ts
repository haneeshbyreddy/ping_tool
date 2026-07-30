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

// Dark roadmap: Google's published night-mode array with its GEOMETRY intact and
// its LABEL TEXT dimmed (2026-07-22 — stock shipped first, operator found the
// text too contrasty within the hour).
//
// The stock array's labels were measured against the navy on which they sit:
// highway 9.29:1, locality/POI/transit 5.36:1, road 5.45:1. Every status tone
// the overlay uses is QUIETER than that (destructive 4.66:1, warning 6.64:1,
// success 5.92:1) — so the basemap's road names were literally louder than a
// device being down, which inverts the one rule this map has. The values below
// put the loudest label at 4.55:1, under every status tone, keeping the
// backdrop a backdrop. Don't restore the stock numbers without redoing that
// comparison; "matching Google's sample" is not the goal, ranking below the
// alarms is.
//
// Also deliberately NEUTRAL now, not tan: the stock gold (#f3d19c highways,
// #d59563 localities) sat right on top of --warning amber, so a glance couldn't
// separate "major road" from "degraded". Geometry keeps Google's navies —
// nobody objected to those, and they're what makes it read as a night map.
// labels.text.stroke stays equal to the geometry it sits on: a stroke matching
// the background is what keeps DIM text crisp without raising its fill contrast.
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
  { elementType: "geometry", stylers: [{ color: "#242f3e" }] },
  { elementType: "labels.text.stroke", stylers: [{ color: "#242f3e" }] },
  { elementType: "labels.text.fill", stylers: [{ color: "#5d6675" }] },
  { featureType: "administrative.locality", elementType: "labels.text.fill", stylers: [{ color: "#828a98" }] },
  { featureType: "poi", elementType: "labels.text.fill", stylers: [{ color: "#6b7280" }] },
  { featureType: "poi.park", elementType: "geometry", stylers: [{ color: "#263c3f" }] },
  { featureType: "poi.park", elementType: "labels.text.fill", stylers: [{ color: "#587a61" }] },
  { featureType: "road", elementType: "geometry", stylers: [{ color: "#38414e" }] },
  { featureType: "road", elementType: "geometry.stroke", stylers: [{ color: "#212a37" }] },
  { featureType: "road", elementType: "labels.text.fill", stylers: [{ color: "#79818f" }] },
  { featureType: "road.highway", elementType: "geometry", stylers: [{ color: "#746855" }] },
  { featureType: "road.highway", elementType: "geometry.stroke", stylers: [{ color: "#1f2835" }] },
  { featureType: "road.highway", elementType: "labels.text.fill", stylers: [{ color: "#8f96a3" }] },
  { featureType: "transit", elementType: "geometry", stylers: [{ color: "#2f3948" }] },
  { featureType: "transit.station", elementType: "labels.text.fill", stylers: [{ color: "#6b7280" }] },
  { featureType: "water", elementType: "geometry", stylers: [{ color: "#17263c" }] },
  { featureType: "water", elementType: "labels.text.fill", stylers: [{ color: "#4a5462" }] },
  { featureType: "water", elementType: "labels.text.stroke", stylers: [{ color: "#17263c" }] },
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
  // LABELS_OFF goes LAST: later rules win, so it overrides the night array's
  // own label colours rather than being overridden by them.
  const arr = [...(dark ? NIGHT_STYLE : []), ...(labels ? [] : LABELS_OFF)]
  return arr.length ? arr : null
}

/** Which styled variant this is, for the cache key. "" = unstyled.
 *  `n` = night, `p` = plain (labels stripped); both can apply at once. */
const variantOf = (t: GoogleMapType, dark: boolean, labels: boolean): string =>
  t !== "roadmap" ? "" : `${dark ? "n" : ""}${labels ? "" : "p"}`

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
