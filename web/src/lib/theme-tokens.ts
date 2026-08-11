export type ThemeMode = "dark" | "light"
export type TokenMap = Record<string, string>
export type ThemeOverrides = { dark?: TokenMap; light?: TokenMap }

type Rgb = { r: number; g: number; b: number }
type Oklab = { L: number; a: number; b: number }

const toLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
const toSrgb = (c: number) => (c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055)
const clamp01 = (n: number) => (n < 0 ? 0 : n > 1 ? 1 : n)

export function parseHex(hex: string): Rgb | null {
  let h = hex.trim().replace(/^#/, "")
  if (h.length === 3) h = h.split("").map((c) => c + c).join("")
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return null
  const n = parseInt(h, 16)
  return { r: ((n >> 16) & 255) / 255, g: ((n >> 8) & 255) / 255, b: (n & 255) / 255 }
}

export function toHex({ r, g, b }: Rgb): string {
  const part = (c: number) => Math.round(clamp01(c) * 255).toString(16).padStart(2, "0")
  return `#${part(r)}${part(g)}${part(b)}`
}

function rgbToOklab({ r, g, b }: Rgb): Oklab {
  const lr = toLinear(r), lg = toLinear(g), lb = toLinear(b)
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb)
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb)
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb)
  return {
    L: 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    a: 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    b: 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  }
}

function oklabToRgbRaw({ L, a, b }: Oklab): Rgb {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3
  return {
    r: toSrgb(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    g: toSrgb(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    b: toSrgb(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  }
}

function oklabToRgb(lab: Oklab): Rgb {
  const inGamut = (c: Rgb) => c.r >= -1e-4 && c.r <= 1.0001 && c.g >= -1e-4
    && c.g <= 1.0001 && c.b >= -1e-4 && c.b <= 1.0001
  let raw = oklabToRgbRaw(lab)
  if (inGamut(raw)) return raw
  let lo = 0, hi = 1
  for (let i = 0; i < 18; i++) {
    const mid = (lo + hi) / 2
    raw = oklabToRgbRaw({ L: lab.L, a: lab.a * mid, b: lab.b * mid })
    if (inGamut(raw)) lo = mid; else hi = mid
  }
  return oklabToRgbRaw({ L: lab.L, a: lab.a * lo, b: lab.b * lo })
}

export function shiftL(hex: string, delta: number, chromaScale = 1): string {
  const rgb = parseHex(hex)
  if (!rgb) return hex
  const lab = rgbToOklab(rgb)
  return toHex(oklabToRgb({
    L: Math.max(0, Math.min(1, lab.L + delta)),
    a: lab.a * chromaScale,
    b: lab.b * chromaScale,
  }))
}

function relLuminance({ r, g, b }: Rgb): number {
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b)
}

export function contrast(a: string, b: string): number {
  const ra = parseHex(a), rb = parseHex(b)
  if (!ra || !rb) return 1
  const la = relLuminance(ra), lb = relLuminance(rb)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

export function readableInk(tone: string): string {
  const rgb = parseHex(tone)
  if (!rgb) return "#ffffff"
  const lab = rgbToOklab(rgb)
  const darkInk = toHex(oklabToRgb({ L: 0.205, a: lab.a * 0.35, b: lab.b * 0.35 }))
  return contrast(tone, darkInk) >= contrast(tone, "#ffffff") ? darkInk : "#ffffff"
}

export function softFill(hex: string, alpha: number): string {
  const rgb = parseHex(hex)
  if (!rgb) return hex
  const c = (n: number) => Math.round(clamp01(n) * 255)
  return `rgba(${c(rgb.r)},${c(rgb.g)},${c(rgb.b)},${alpha})`
}

const SURFACE_STEPS: Record<ThemeMode, Record<string, number>> = {
  dark: { "--muted": -0.022, "--secondary": 0.017, "--popover": 0.034, "--accent": 0.07 },
  light: { "--muted": -0.05, "--secondary": -0.053, "--popover": 0, "--accent": -0.067 },
}

const TEXT_STEPS: Record<ThemeMode, Record<string, number>> = {
  dark: { "--muted-foreground": -0.178, "--faint-foreground": -0.286, "--ghost-foreground": -0.368 },
  light: { "--muted-foreground": 0.271, "--faint-foreground": 0.389, "--ghost-foreground": 0.498 },
}

const SIDEBAR_STEP: Record<ThemeMode, number> = { dark: 0.039, light: 0.018 }
const SOFT_ALPHA: Record<ThemeMode, number> = { dark: 0.13, light: 0.1 }
const MAP_LINK_STEP: Record<ThemeMode, number> = { dark: -0.1, light: 0.03 }

export type SeedId = "canvas" | "panel" | "text" | "primary" | "success" | "warning" | "destructive"

export type Seed = {
  id: SeedId
  label: string
  hint: string
  token: string
  base: Record<ThemeMode, string>
  derive: (value: string, mode: ThemeMode) => TokenMap
}

function surfaceFamily(card: string, mode: ThemeMode): TokenMap {
  const out: TokenMap = { "--card": card }
  for (const [token, d] of Object.entries(SURFACE_STEPS[mode])) out[token] = shiftL(card, d)
  out["--sidebar-accent"] = out["--accent"]
  return out
}

function toneFamily(
  tokens: { tone: string; foreground: string; soft: string },
  tone: string,
  mode: ThemeMode,
): TokenMap {
  return {
    [tokens.tone]: tone,
    [tokens.foreground]: readableInk(tone),
    [tokens.soft]: softFill(tone, SOFT_ALPHA[mode]),
  }
}

export const SEEDS: Seed[] = [
  {
    id: "canvas",
    label: "Canvas",
    hint: "The field behind every panel, and the sidebar that sits just above it.",
    token: "--background",
    base: { dark: "#0c0e12", light: "#f4f4f6" },
    derive: (v, mode) => ({
      "--background": v,
      "--sidebar": shiftL(v, SIDEBAR_STEP[mode]),
    }),
  },
  {
    id: "panel",
    label: "Panels",
    hint: "Cards, menus, wells and hover fills. The whole surface ladder steps off this.",
    token: "--card",
    base: { dark: "#1c1f24", light: "#ffffff" },
    derive: surfaceFamily,
  },
  {
    id: "text",
    label: "Text",
    hint: "Body text; the muted, faint and decorative steps fade out from it.",
    token: "--foreground",
    base: { dark: "#e6e4e0", light: "#16171c" },
    derive: (v, mode) => {
      const out: TokenMap = {
        "--foreground": v,
        "--card-foreground": v,
        "--popover-foreground": v,
        "--accent-foreground": v,
        "--secondary-foreground": v,
        "--sidebar-foreground": v,
        "--sidebar-accent-foreground": v,
      }
      for (const [token, d] of Object.entries(TEXT_STEPS[mode])) out[token] = shiftL(v, d)
      return out
    },
  },
  {
    id: "primary",
    label: "Accent",
    hint: "Buttons, links, focus rings and healthy map lines. The brand colour.",
    token: "--primary",
    base: { dark: "#74aec9", light: "#2e7391" },
    derive: (v, mode) => ({
      ...toneFamily({
        tone: "--primary", foreground: "--primary-foreground",
        soft: "--primary-soft",
      }, v, mode),
      "--ring": v,
      "--sidebar-primary": v,
      "--sidebar-primary-foreground": readableInk(v),
      "--sidebar-ring": v,
      "--map-link": shiftL(v, MAP_LINK_STEP[mode], 0.8),
    }),
  },
  {
    id: "success",
    label: "Healthy",
    hint: "Everything up: online devices, resolved outages, healthy links.",
    token: "--success",
    base: { dark: "#5fbe83", light: "#2f7d4f" },
    derive: (v, mode) => toneFamily({
      tone: "--success", foreground: "--success-foreground",
      soft: "--success-soft",
    }, v, mode),
  },
  {
    id: "warning",
    label: "Degraded",
    hint: "Packet loss, latency drift, capacity and staleness warnings.",
    token: "--warning",
    base: { dark: "#e3ac57", light: "#8a6410" },
    derive: (v, mode) => toneFamily({
      tone: "--warning", foreground: "--warning-foreground",
      soft: "--warning-soft",
    }, v, mode),
  },
  {
    id: "destructive",
    label: "Down",
    hint: "Outages and destructive actions. Keep this the loudest colour on screen.",
    token: "--destructive",
    base: { dark: "#e27a6b", light: "#a8432e" },
    derive: (v, mode) => toneFamily({
      tone: "--destructive", foreground: "--destructive-foreground",
      soft: "--destructive-soft",
    }, v, mode),
  },
]

export const ADVANCED_TOKENS: Array<{ token: string; label: string; base: Record<ThemeMode, string> }> = [
  { token: "--border", label: "Border", base: { dark: "rgba(255,255,255,0.10)", light: "rgba(0,0,0,0.11)" } },
  { token: "--border-subtle", label: "Border (internal)", base: { dark: "rgba(255,255,255,0.055)", light: "rgba(0,0,0,0.065)" } },
  { token: "--input", label: "Border (raised)", base: { dark: "rgba(255,255,255,0.14)", light: "rgba(0,0,0,0.17)" } },
  { token: "--sidebar-border", label: "Sidebar border", base: { dark: "rgba(255,255,255,0.07)", light: "rgba(0,0,0,0.09)" } },
  { token: "--chart-1", label: "Chart 1 (optical)", base: { dark: "#47878b", light: "#52878a" } },
  { token: "--chart-2", label: "Chart 2 (traffic)", base: { dark: "#4f7495", light: "#6c8ca9" } },
  { token: "--chart-3", label: "Chart 3 (vitals)", base: { dark: "#717ca7", light: "#747da2" } },
  { token: "--chart-4", label: "Chart 4 (plant)", base: { dark: "#776994", light: "#8f83a8" } },
  { token: "--chart-5", label: "Chart 5 (fleet)", base: { dark: "#957296", light: "#937594" } },
  { token: "--map-link", label: "Map link", base: { dark: "#5e8a9e", light: "#35789a" } },
]

export const ALL_TOKENS: string[] = [
  ...new Set([
    ...SEEDS.flatMap((s) => Object.keys(s.derive(s.base.dark, "dark"))),
    ...ADVANCED_TOKENS.map((t) => t.token),
  ]),
].sort()

export function buildOverrides(
  seeds: Partial<Record<SeedId, string>>,
  advanced: TokenMap,
  mode: ThemeMode,
): TokenMap {
  const out: TokenMap = {}
  for (const seed of SEEDS) {
    const value = seeds[seed.id]
    if (!value || value.toLowerCase() === seed.base[mode].toLowerCase()) continue
    Object.assign(out, seed.derive(value, mode))
  }
  for (const { token, base } of ADVANCED_TOKENS) {
    const value = advanced[token]
    if (value && value.trim() && value.trim() !== base[mode]) out[token] = value.trim()
  }
  return out
}

export function renderCss(overrides: ThemeOverrides): string {
  let css = ""
  for (const mode of ["dark", "light"] as const) {
    const tokens = overrides[mode]
    if (!tokens) continue
    const entries = Object.entries(tokens).sort(([a], [b]) => a.localeCompare(b))
    if (!entries.length) continue
    css += (mode === "dark" ? ":root.dark" : ":root:not(.dark)")
      + "{" + entries.map(([t, v]) => `${t}:${v};`).join("") + "}"
  }
  return css
}

const PREVIEW_ID = "wisp-theme-preview"

export function applyPreview(overrides: ThemeOverrides): void {
  const css = renderCss(overrides)
  let el = document.getElementById(PREVIEW_ID) as HTMLStyleElement | null
  if (!css) {
    el?.remove()
    return
  }
  if (!el) {
    el = document.createElement("style")
    el.id = PREVIEW_ID
    document.head.appendChild(el)
  }
  el.textContent = css
}
