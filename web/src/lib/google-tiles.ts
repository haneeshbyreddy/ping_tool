// Google Map Tiles API plumbing for the map view's Google basemaps.
//
// The key is org-scoped (orgs.google_maps_key), referrer-restricted, and ships
// to signed-in browsers BY DESIGN — central never talks to Google; the browser
// does the createSession/tile/viewport fetches itself, same trust model as the
// CARTO/Esri tile CDNs. Session tokens (~2-week expiry) are cached per map
// type in localStorage and recreated when tiles start failing.

export type GoogleMapType = "roadmap" | "satellite"

const sessionKey = (t: GoogleMapType) => `wisp:map:gsession:${t}`

interface CachedSession {
  session: string
  expiry: number // unix seconds, from Google's createSession reply
}

export function loadGoogleSession(mapType: GoogleMapType): string | null {
  try {
    const raw = localStorage.getItem(sessionKey(mapType))
    if (!raw) return null
    const v = JSON.parse(raw) as CachedSession
    // 10-minute guard so a token can't expire mid-pan
    return v.session && Date.now() / 1000 < v.expiry - 600 ? v.session : null
  } catch {
    return null
  }
}

export function clearGoogleSession(mapType: GoogleMapType): void {
  try {
    localStorage.removeItem(sessionKey(mapType))
  } catch {
    /* noop */
  }
}

export async function createGoogleSession(apiKey: string, mapType: GoogleMapType): Promise<string> {
  const cached = loadGoogleSession(mapType)
  if (cached) return cached
  const res = await fetch(
    `https://tile.googleapis.com/v1/createSession?key=${encodeURIComponent(apiKey)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // language/region shape label choices; the org key's billing is India-based
      body: JSON.stringify({ mapType, language: "en-IN", region: "IN" }),
    },
  )
  if (!res.ok) throw new Error(`createSession replied ${res.status}`)
  const data = (await res.json()) as { session?: string; expiry?: string }
  if (!data.session) throw new Error("createSession returned no session token")
  try {
    localStorage.setItem(
      sessionKey(mapType),
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
