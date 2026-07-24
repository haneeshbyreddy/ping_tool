/* Runtime theme overrides — the superadmin's colour controls.
 *
 * WHY THIS EXISTS: every palette change used to be a source edit to index.css
 * plus an SPA rebuild plus a redeploy. The tokens in index.css are still the
 * SHIPPED DEFAULT and the design record (their comments carry the reasoning);
 * this module lets the superadmin write a thin OVERRIDE layer on top, stored
 * server-side in `app_settings.theme_overrides` and injected into the SPA's
 * <head> before first paint (server.py:_inject_theme).
 *
 * THE CENTRAL RULE — an untouched seed emits NOTHING. Overrides are a sparse
 * diff, never a full snapshot. Two consequences worth understanding before
 * changing anything here:
 *   1. A stock install is byte-identical to the shipped CSS. There is no
 *      "default theme" written to the DB that could drift from index.css.
 *   2. A future palette change in index.css still reaches every deployment,
 *      for every token the operator has not personally taken over. Snapshotting
 *      the whole palette on first save would freeze each install on whatever
 *      the palette happened to be that day — that is the bug this avoids.
 *
 * WHY SEEDS AND NOT A RAW TOKEN GRID: index.css encodes relationships that
 * were paid for in field feedback — the surface ladder's perceptual steps, the
 * text ramp, and above all that a tone's ink is chosen by MEASUREMENT and not
 * by taste (a filled --primary button at L*~68 fails AA with white on it, see
 * the --primary-foreground comment in index.css). Editing 40 raw tokens by
 * hand loses all of that on the first save. So the UI edits ~7 seeds and this
 * module re-derives each seed's family, preserving those relationships by
 * construction. `ADVANCED_TOKENS` is the escape hatch for the rest.
 */

export type ThemeMode = "dark" | "light"
/** Sparse token diff, e.g. `{ "--background": "#0c0e12" }`. */
export type TokenMap = Record<string, string>
export type ThemeOverrides = { dark?: TokenMap; light?: TokenMap }

/* ------------------------------------------------------------------ colour
 * OKLab, because the derivations here are all "same colour, different
 * lightness" and sRGB/HSL lightness is not perceptually even — stepping a
 * surface ladder in HSL gives visibly uneven gaps at the dark end, which is
 * exactly where this app's ladder lives.
 */

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

/** OKLab -> sRGB, pulling chroma in until the colour is actually representable.
 *  Naive clipping of out-of-gamut channels shifts HUE (clipping only red on an
 *  over-saturated orange turns it yellow); reducing chroma at constant L and
 *  hue keeps the colour recognisably the one that was asked for. */
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

/** Same hue and chroma, lightness moved by `delta` (OKLab L, 0..1). */
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

/** The ink that goes ON a filled tone — chosen by MEASUREMENT, never taste.
 *
 *  This is the load-bearing one. index.css spells out why --primary-foreground
 *  is dark ink and not white: the steel blue sits at L*~68, where white on it
 *  measures 2.5:1 and fails AA outright. Any hand-picked ink re-opens that bug
 *  the moment someone drags the primary swatch lighter. Candidates are a
 *  hue-matched near-black (a tinted ink reads as part of the tone, where pure
 *  black reads as a hole) and plain white; the higher contrast ratio wins. */
export function readableInk(tone: string): string {
  const rgb = parseHex(tone)
  if (!rgb) return "#ffffff"
  const lab = rgbToOklab(rgb)
  // L 0.205, chroma x0.35 — both measured off the shipped inks (which all land
  // at L 0.20-0.22 and stay faintly tinted; this pair reproduces --primary-
  // foreground's #0b1920 to within one step). Neither number is free to raise:
  // darker than ~0.18 or more chroma than ~0.4 and the tint clips out of gamut,
  // collapsing the ink to flat black, which reads as a hole in the button.
  const darkInk = toHex(oklabToRgb({ L: 0.205, a: lab.a * 0.35, b: lab.b * 0.35 }))
  return contrast(tone, darkInk) >= contrast(tone, "#ffffff") ? darkInk : "#ffffff"
}

/** Translucent wash of a tone — the `*-soft` badge/chip fills. */
export function softFill(hex: string, alpha: number): string {
  const rgb = parseHex(hex)
  if (!rgb) return hex
  const c = (n: number) => Math.round(clamp01(n) * 255)
  return `rgba(${c(rgb.r)},${c(rgb.g)},${c(rgb.b)},${alpha})`
}

/* ------------------------------------------------------------------- seeds
 *
 * Offsets below are MEASURED off the shipped palette, not invented: each is the
 * OKLab-L distance the real token sits from its seed today. Dark and light get
 * their own tables rather than one signed formula, because the two ladders are
 * genuinely different shapes — in light mode --card is pure white, so there is
 * no headroom above it and "elevation" has to be expressed by going DARKER for
 * interaction fills. Pretending one rule covered both is how light mode ends up
 * with an accent that is invisible against its card.
 */

/** Offsets from --card. muted is negative in BOTH modes: a well recesses. */
const SURFACE_STEPS: Record<ThemeMode, Record<string, number>> = {
  dark: { "--muted": -0.022, "--secondary": 0.017, "--popover": 0.034, "--accent": 0.07 },
  light: { "--muted": -0.05, "--secondary": -0.053, "--popover": 0, "--accent": -0.067 },
}

/** Offsets from --foreground: the four text steps, each fading toward its surface. */
const TEXT_STEPS: Record<ThemeMode, Record<string, number>> = {
  dark: { "--muted-foreground": -0.178, "--faint-foreground": -0.286, "--ghost-foreground": -0.368 },
  light: { "--muted-foreground": 0.271, "--faint-foreground": 0.389, "--ghost-foreground": 0.498 },
}

const SIDEBAR_STEP: Record<ThemeMode, number> = { dark: 0.039, light: 0.018 }
const SOFT_ALPHA: Record<ThemeMode, number> = { dark: 0.13, light: 0.1 }
/** --map-link is deliberately a step BELOW --primary: primary is what an
 *  emphasised (selected-path) link switches to, so the resting line has to
 *  leave room above it. */
const MAP_LINK_STEP: Record<ThemeMode, number> = { dark: -0.1, light: 0.03 }

export type SeedId = "canvas" | "panel" | "text" | "primary" | "success" | "warning" | "destructive"

export type Seed = {
  id: SeedId
  label: string
  /** What the operator is actually choosing, in their words. */
  hint: string
  /** The token the swatch edits directly. */
  token: string
  /** Shipped values, per mode — the "untouched" comparison point. */
  base: Record<ThemeMode, string>
  /** Everything this seed re-derives, including its own token. */
  derive: (value: string, mode: ThemeMode) => TokenMap
}

function surfaceFamily(card: string, mode: ThemeMode): TokenMap {
  const out: TokenMap = { "--card": card }
  for (const [token, d] of Object.entries(SURFACE_STEPS[mode])) out[token] = shiftL(card, d)
  out["--sidebar-accent"] = out["--accent"]
  return out
}

/** A status tone and the three tokens that always travel with it: the ink that
 *  goes on top of it, its translucent badge fill, and its chart slot.
 *
 *  Token names are passed in as LITERALS rather than built from a prefix. They
 *  read the same either way, but literals stay greppable — both for a human
 *  chasing where `--success-soft` comes from, and for the allowlist parity test
 *  (tests/unit/test_theme.py), which scans this file's token strings to prove
 *  the Python-side allowlist has not drifted. A template literal is invisible
 *  to both. */
function toneFamily(
  tokens: { tone: string; foreground: string; soft: string; chart: string },
  tone: string,
  mode: ThemeMode,
): TokenMap {
  return {
    [tokens.tone]: tone,
    [tokens.foreground]: readableInk(tone),
    [tokens.soft]: softFill(tone, SOFT_ALPHA[mode]),
    [tokens.chart]: tone,
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
    hint: "Cards, menus, wells and hover fills — the whole surface ladder steps off this.",
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
    hint: "Buttons, links, focus rings and healthy map lines — the brand colour.",
    token: "--primary",
    base: { dark: "#74aec9", light: "#2e7391" },
    derive: (v, mode) => ({
      ...toneFamily({
        tone: "--primary", foreground: "--primary-foreground",
        soft: "--primary-soft", chart: "--chart-1",
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
      soft: "--success-soft", chart: "--chart-2",
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
      soft: "--warning-soft", chart: "--chart-3",
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
      soft: "--destructive-soft", chart: "--chart-4",
    }, v, mode),
  },
]

/** Tokens the seeds do not reach — borders, the neutral chart slot, sidebar
 *  hairlines. Offered raw, because the alternative is a support conversation.
 *  Borders are ALPHA on purpose (see index.css): one token then holds the same
 *  relationship on canvas, card, popover AND raster map tiles. Editing them to
 *  hex re-introduces the drift alpha was adopted to kill, so the UI says so. */
export const ADVANCED_TOKENS: Array<{ token: string; label: string; base: Record<ThemeMode, string> }> = [
  { token: "--border", label: "Border", base: { dark: "rgba(255,255,255,0.10)", light: "rgba(0,0,0,0.11)" } },
  { token: "--border-subtle", label: "Border (internal)", base: { dark: "rgba(255,255,255,0.055)", light: "rgba(0,0,0,0.065)" } },
  { token: "--input", label: "Border (raised)", base: { dark: "rgba(255,255,255,0.14)", light: "rgba(0,0,0,0.17)" } },
  { token: "--sidebar-border", label: "Sidebar border", base: { dark: "rgba(255,255,255,0.07)", light: "rgba(0,0,0,0.09)" } },
  { token: "--chart-5", label: "Chart (neutral)", base: { dark: "#8b8f98", light: "#71717c" } },
  { token: "--map-link", label: "Map link", base: { dark: "#5e8a9e", light: "#35789a" } },
]

/** Every token the server will accept. Anything outside this set is dropped —
 *  the values land in a <style> block, so the allowlist is a security boundary
 *  and not just tidiness. Mirrored in central/api/orgs.py:_THEME_TOKENS; the
 *  two lists are pinned together by test_theme_token_allowlist_matches_spa. */
export const ALL_TOKENS: string[] = [
  ...new Set([
    ...SEEDS.flatMap((s) => Object.keys(s.derive(s.base.dark, "dark"))),
    ...ADVANCED_TOKENS.map((t) => t.token),
  ]),
].sort()

/* --------------------------------------------------------------- assembly */

/** Seed values -> the sparse override map that actually gets stored.
 *
 *  An untouched seed contributes nothing (see the file header): that is what
 *  keeps a stock install on the shipped CSS and lets future palette work reach
 *  deployments that never touched the colour in question. */
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

/** Mirror of `central/theme.py:render_css`. The preview has to emit exactly what
 *  the server will, or what you see while dragging is not what gets saved.
 *
 *  On why the selectors are `:root:not(.dark)` / `:root.dark` rather than the
 *  `:root` / `.dark` pair index.css uses, see the docstring on the Python side.
 *  Short version: `:root` and `.dark` have EQUAL specificity, so a plain
 *  `:root{}` of light overrides injected after the bundle wins in DARK mode
 *  and paints it white. These two are (0,2,0) and can never both match. */
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

/** Show a palette live, by writing the SAME mode selectors the server injects
 *  (`.dark` / `:root`) into a <style> element.
 *
 *  It must NOT be done as inline styles on <html>, which is what this did
 *  first and is a bug worth spelling out: inline styles on the root element
 *  outrank every stylesheet rule INCLUDING `.dark{}`, and they carry no notion
 *  of which mode they belong to. Previewing light-mode tokens inline therefore
 *  survived a switch to dark mode and painted the dark theme white — the theme
 *  class had no way to win. Selector-scoped CSS lets the theme class do its
 *  job, and lets BOTH modes be previewed at once instead of whichever one
 *  happened to be on screen.
 *
 *  Appended last in <head> so it outranks the server's injected block at equal
 *  specificity — the same source-order argument that block relies on against
 *  the bundle stylesheet. Empty overrides remove the element entirely rather
 *  than leaving an empty tag behind. */
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
