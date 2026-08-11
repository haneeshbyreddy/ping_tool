export type GoogleMapType = "roadmap" | "satellite"

const hiDpi = () => typeof window !== "undefined" && window.devicePixelRatio > 1

const NIGHT_STYLE = [
  { elementType: "geometry", stylers: [{ color: "#15181d" }] },
  { elementType: "labels.text.stroke", stylers: [{ color: "#15181d" }] },
  { elementType: "labels.text.fill", stylers: [{ color: "#30353d" }] },
  { featureType: "administrative.locality", elementType: "labels.text.fill", stylers: [{ color: "#454a51" }] },
  { featureType: "poi", elementType: "labels.text.fill", stylers: [{ color: "#373c42" }] },
  { featureType: "poi.park", elementType: "geometry", stylers: [{ color: "#141d19" }] },
  { featureType: "poi.park", elementType: "labels.text.fill", stylers: [{ color: "#2d3d31" }] },
  { featureType: "road", elementType: "geometry", stylers: [{ color: "#24262d" }] },
  { featureType: "road", elementType: "geometry.stroke", stylers: [{ color: "#0f1114" }] },
  { featureType: "road", elementType: "labels.text.fill", stylers: [{ color: "#40454c" }] },
  { featureType: "road.highway", elementType: "geometry", stylers: [{ color: "#292e36" }] },
  { featureType: "road.highway", elementType: "geometry.stroke", stylers: [{ color: "#0f1114" }] },
  { featureType: "road.highway", elementType: "labels.text.fill", stylers: [{ color: "#484c53" }] },
  { featureType: "transit", elementType: "geometry", stylers: [{ color: "#1d2026" }] },
  { featureType: "transit.station", elementType: "labels.text.fill", stylers: [{ color: "#373c42" }] },
  { featureType: "water", elementType: "geometry", stylers: [{ color: "#0a0e15" }] },
  { featureType: "water", elementType: "labels.text.fill", stylers: [{ color: "#232830" }] },
  { featureType: "water", elementType: "labels.text.stroke", stylers: [{ color: "#0a0e15" }] },
]

const DAY_STYLE = [
  { elementType: "geometry", stylers: [{ color: "#eaeaef" }] },
  { elementType: "labels.text.stroke", stylers: [{ color: "#eaeaef" }] },
  { elementType: "labels.text.fill", stylers: [{ color: "#c2c8d1" }] },
  { featureType: "administrative.locality", elementType: "labels.text.fill", stylers: [{ color: "#acb4c0" }] },
  { featureType: "poi", elementType: "labels.text.fill", stylers: [{ color: "#bec5cf" }] },
  { featureType: "poi.park", elementType: "geometry", stylers: [{ color: "#dfe7df" }] },
  { featureType: "poi.park", elementType: "labels.text.fill", stylers: [{ color: "#b5cab7" }] },
  { featureType: "road", elementType: "geometry", stylers: [{ color: "#f9f9fc" }] },
  { featureType: "road", elementType: "geometry.stroke", stylers: [{ color: "#dcdce3" }] },
  { featureType: "road", elementType: "labels.text.fill", stylers: [{ color: "#b5bcc6" }] },
  { featureType: "road.highway", elementType: "geometry", stylers: [{ color: "#f9f9fc" }] },
  { featureType: "road.highway", elementType: "geometry.stroke", stylers: [{ color: "#d8d8e2" }] },
  { featureType: "road.highway", elementType: "labels.text.fill", stylers: [{ color: "#afb6c1" }] },
  { featureType: "transit", elementType: "geometry", stylers: [{ color: "#e0e0e7" }] },
  { featureType: "transit.station", elementType: "labels.text.fill", stylers: [{ color: "#bec5cf" }] },
  { featureType: "water", elementType: "geometry", stylers: [{ color: "#d7dee6" }] },
  { featureType: "water", elementType: "labels.text.fill", stylers: [{ color: "#c2ccd9" }] },
  { featureType: "water", elementType: "labels.text.stroke", stylers: [{ color: "#d7dee6" }] },
]

const LABELS_OFF = [
  { elementType: "labels", stylers: [{ visibility: "off" }] },
]

const ICONS_OFF = [
  { featureType: "poi", elementType: "labels.icon", stylers: [{ visibility: "off" }] },
  { featureType: "transit", elementType: "labels.icon", stylers: [{ visibility: "off" }] },
  { featureType: "road", elementType: "labels.icon", stylers: [{ visibility: "off" }] },
]

function styleArray(t: GoogleMapType, dark: boolean, labels: boolean) {
  if (t !== "roadmap") return null
  return [...ICONS_OFF, ...(dark ? NIGHT_STYLE : DAY_STYLE), ...(labels ? [] : LABELS_OFF)]
}

const variantOf = (t: GoogleMapType, dark: boolean, labels: boolean): string =>
  t !== "roadmap" ? "" : `${dark ? "n" : ""}${labels ? "" : "p"}` || "b"

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

const sessionKey = (t: GoogleMapType, dark: boolean, labels: boolean) => {
  const v = variantOf(t, dark, labels)
  return `wisp:map:gsession:${t}:${hiDpi() ? "2x" : "1x"}`
    + (v ? `:s${v}-${styleTag(t, dark, labels)}` : "")
}

function pruneStaleStyledSessions(keep: string, variant: string): void {
  const prefix = `:s${variant}-`
  try {
    for (const k of Object.keys(localStorage))
      if (k.startsWith("wisp:map:gsession:") && k !== keep
          && (k.includes(prefix) || k.includes(":night-")))
        localStorage.removeItem(k)
  } catch {
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
  }
  return data.session
}

export function googleTileUrl(session: string, apiKey: string): string {
  return `https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session=${encodeURIComponent(session)}&key=${encodeURIComponent(apiKey)}`
}

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
