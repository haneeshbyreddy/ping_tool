/* Settings -> Platform -> Appearance. Superadmin-only, server-wide.
 *
 * Server-wide rather than per-org, matching the Google Maps key: this is the
 * product's look, not a tenant preference, and per-org palettes would mean the
 * screenshots in a support conversation no longer match what anyone else sees.
 *
 * The editing model is SEEDS, not a grid of raw tokens — see the header of
 * lib/theme-tokens.ts for why (short version: the ladder steps and the
 * measured ink choice are worth more than the freedom to set 44 hex codes by
 * hand, and hand-setting them loses those on the first save). Advanced is
 * there for the handful of tokens no seed reaches.
 */
import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Palette, RotateCcw } from "lucide-react"
import { toast } from "sonner"

import { adminApi, ApiError } from "@/lib/api"
import { applyTheme, getStoredTheme } from "@/lib/theme"
import {
  ADVANCED_TOKENS, SEEDS, applyPreview, buildOverrides, contrast, parseHex,
  readableInk, type SeedId, type ThemeMode, type ThemeOverrides, type TokenMap,
} from "@/lib/theme-tokens"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Segmented } from "@/components/ui/segmented"
import { Skeleton } from "@/components/ui/skeleton"

type SeedMap = Partial<Record<SeedId, string>>

/** Recover the seed values from a stored token map. The store holds the
 *  DERIVED tokens (that is what the page needs), so each seed reads back off
 *  the one token it owns, falling back to the shipped value when absent —
 *  which is also how an untouched seed stays untouched across a save. */
function seedsFrom(tokens: TokenMap, mode: ThemeMode): SeedMap {
  const out: SeedMap = {}
  for (const seed of SEEDS) out[seed.id] = tokens[seed.token] ?? seed.base[mode]
  return out
}

function advancedFrom(tokens: TokenMap, mode: ThemeMode): TokenMap {
  const out: TokenMap = {}
  for (const { token, base } of ADVANCED_TOKENS) out[token] = tokens[token] ?? base[mode]
  return out
}

/** Colour swatch + hex field. The native picker is the fast path (drag and the
 *  whole app repaints live); the text field is how you paste a hex from a
 *  brand doc, which is the other half of how this actually gets used. */
function SwatchField({ value, onChange, id }: {
  value: string
  onChange: (v: string) => void
  id: string
}) {
  const [text, setText] = useState(value)
  useEffect(() => { setText(value) }, [value])
  const valid = !!parseHex(text)
  return (
    <div className="flex items-center gap-2">
      <input
        type="color"
        id={id}
        aria-label="Colour picker"
        value={parseHex(value) ? value : "#000000"}
        onChange={(e) => onChange(e.target.value)}
        className="size-8 shrink-0 cursor-pointer rounded-md border bg-transparent p-0.5"
      />
      <Input
        value={text}
        spellCheck={false}
        aria-invalid={!valid}
        className="h-8 w-28 font-mono text-2xs"
        onChange={(e) => {
          setText(e.target.value)
          if (parseHex(e.target.value)) onChange(e.target.value)
        }}
      />
    </div>
  )
}

/** Live contrast readout for the tone seeds.
 *
 *  Not decoration: the whole reason a colour picker is safe to hand over is
 *  that the consequences are visible while choosing. A tone dragged pale enough
 *  to fail against the panel behind it is the one mistake that actually hurts —
 *  it is how an outage row stops being legible — so it gets called out here
 *  rather than discovered on a wall display during an incident. 4.5:1 is the
 *  WCAG AA floor for body text. */
function ContrastNote({ tone, panel }: { tone: string; panel: string }) {
  if (!parseHex(tone) || !parseHex(panel)) return null
  const onPanel = contrast(tone, panel)
  const ink = readableInk(tone)
  const ok = onPanel >= 4.5
  return (
    <span className={ok ? "text-faint-foreground" : "text-warning"}>
      {onPanel.toFixed(1)}:1 on panels{ok ? "" : " — too low to read as text"}
      <span className="ml-2 text-ghost-foreground">
        ink {ink === "#ffffff" ? "white" : "dark"}
      </span>
    </span>
  )
}

export function AppearanceCard() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: adminApi.settings,
  })

  // Edit the mode you are actually looking at, or the preview is meaningless.
  const [mode, setMode] = useState<ThemeMode>(() => getStoredTheme())
  const [seeds, setSeeds] = useState<Record<ThemeMode, SeedMap>>({ dark: {}, light: {} })
  const [advanced, setAdvanced] = useState<Record<ThemeMode, TokenMap>>({ dark: {}, light: {} })
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (!data) return
    const stored = data.theme_overrides ?? {}
    setSeeds({
      dark: seedsFrom(stored.dark ?? {}, "dark"),
      light: seedsFrom(stored.light ?? {}, "light"),
    })
    setAdvanced({
      dark: advancedFrom(stored.dark ?? {}, "dark"),
      light: advancedFrom(stored.light ?? {}, "light"),
    })
    setDirty(false)
  }, [data])

  const overrides = useMemo(() => ({
    dark: buildOverrides(seeds.dark, advanced.dark, "dark"),
    light: buildOverrides(seeds.light, advanced.light, "light"),
  }), [seeds, advanced])

  // Repaint the real app as they drag. BOTH modes go in every time: the
  // preview is mode-scoped CSS, so the inactive mode's tokens sit harmlessly
  // behind the theme class until it is switched on. Previewing only the
  // on-screen mode is what made a saved light palette leak into dark mode.
  useEffect(() => { applyPreview(overrides) }, [overrides])

  // What is actually stored server-side, tracked in a ref so the unmount
  // cleanup below reads the current value rather than the one captured when
  // the effect was created.
  const savedRef = useRef<ThemeOverrides>({})
  useEffect(() => { if (data) savedRef.current = data.theme_overrides ?? {} }, [data])

  // On unmount, fall back to the SAVED palette rather than clearing outright.
  // Both halves matter: abandoned edits must not outlive this card (a preview
  // left standing is indistinguishable from a saved theme, and the next save
  // would be judged against a palette that was never stored), but a save that
  // just succeeded must not visually revert the moment you navigate away — the
  // server's injected block was rendered at page load and still holds the old
  // colours until a reload, so the preview element is what keeps them agreeing.
  useEffect(() => () => applyPreview(savedRef.current), [])

  const save = useMutation({
    mutationFn: () => adminApi.saveSettings({ theme_overrides: overrides }),
    onSuccess: () => {
      toast.success("Colours saved for all organizations")
      savedRef.current = overrides
      setDirty(false)
      queryClient.invalidateQueries({ queryKey: ["admin-settings"] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Save failed"),
  })

  function setSeed(id: SeedId, value: string) {
    setSeeds((s) => ({ ...s, [mode]: { ...s[mode], [id]: value } }))
    setDirty(true)
  }

  function setAdvancedToken(token: string, value: string) {
    setAdvanced((a) => ({ ...a, [mode]: { ...a[mode], [token]: value } }))
    setDirty(true)
  }

  /** Back to the shipped palette for THIS mode — clears the seeds rather than
   *  writing the defaults in as overrides, so these tokens resume following
   *  index.css and pick up any future design work. */
  function resetMode() {
    setSeeds((s) => ({
      ...s,
      [mode]: Object.fromEntries(SEEDS.map((x) => [x.id, x.base[mode]])) as SeedMap,
    }))
    setAdvanced((a) => ({
      ...a,
      [mode]: Object.fromEntries(ADVANCED_TOKENS.map((t) => [t.token, t.base[mode]])),
    }))
    setDirty(true)
  }

  if (isLoading) return <Skeleton className="h-64 w-full" />

  const active = seeds[mode]
  const panel = active.panel ?? SEEDS.find((s) => s.id === "panel")!.base[mode]
  const changed = Object.keys(overrides[mode]).length

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Palette className="size-4 text-muted-foreground" /> Appearance (all organizations)
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          {/* Switching which palette you edit also switches the app into it.
              A theme editor where the tab and the visible theme can disagree is
              how you end up tuning light mode, flipping to dark, and finding it
              wrong — keeping them locked together removes that state entirely. */}
          <Segmented
            value={mode}
            onChange={(m) => { setMode(m as ThemeMode); applyTheme(m as ThemeMode) }}
            options={[{ value: "dark", label: "Dark" }, { value: "light", label: "Light" }]}
          />
          <span className="text-2xs text-faint-foreground">
            {changed === 0 ? "Using the built-in palette" : `${changed} tokens overridden`}
          </span>
        </div>

        <div className="flex flex-col gap-3">
          {SEEDS.map((seed) => (
            <div key={seed.id} className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
              <div className="w-full sm:w-40">
                <Label htmlFor={`seed-${seed.id}`} className="text-xs">{seed.label}</Label>
              </div>
              <SwatchField
                id={`seed-${seed.id}`}
                value={active[seed.id] ?? seed.base[mode]}
                onChange={(v) => setSeed(seed.id, v)}
              />
              <p className="flex-1 text-2xs text-faint-foreground">
                {["primary", "success", "warning", "destructive"].includes(seed.id)
                  ? <ContrastNote tone={active[seed.id] ?? seed.base[mode]} panel={panel} />
                  : seed.hint}
              </p>
            </div>
          ))}
        </div>

        <p className="max-w-xl text-xs text-muted-foreground">
          Each colour re-derives the tokens that hang off it — panels set the whole
          surface ladder, text sets its muted and faint steps, and every tone gets a
          readable ink and badge fill computed for it. Anything you leave alone keeps
          following the built-in palette, so future design updates still reach it.
        </p>

        <div className="flex flex-col gap-3 border-t pt-3">
          <button
            type="button"
            onClick={() => setShowAdvanced((v) => !v)}
            className="w-fit text-2xs text-muted-foreground hover:text-foreground"
          >
            {showAdvanced ? "Hide" : "Show"} individual tokens
          </button>
          {showAdvanced && (
            <div className="flex flex-col gap-2.5">
              <p className="max-w-xl text-2xs text-faint-foreground">
                Borders are translucent on purpose — one value then sits correctly on
                the canvas, on panels and over map tiles. Replacing them with solid
                colours will look right on one surface and wrong on the others.
              </p>
              {ADVANCED_TOKENS.map(({ token, label, base }) => (
                <div key={token} className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  <div className="w-full sm:w-40">
                    <Label className="text-xs">{label}</Label>
                  </div>
                  <Input
                    value={advanced[mode][token] ?? base[mode]}
                    spellCheck={false}
                    className="h-8 w-56 font-mono text-2xs"
                    onChange={(e) => setAdvancedToken(token, e.target.value)}
                  />
                  <code className="text-2xs text-ghost-foreground">{token}</code>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center gap-2">
          <Button size="sm" disabled={save.isPending || !dirty} onClick={() => save.mutate()}>
            {dirty ? "Save colours" : "Saved"}
          </Button>
          <Button size="sm" variant="ghost" onClick={resetMode}>
            <RotateCcw className="size-3.5" /> Reset {mode}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
