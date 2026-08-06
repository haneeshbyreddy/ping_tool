import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import QRCode from "qrcode"
import {
  BatteryWarning, Check, Copy, KeyRound, Navigation, RotateCw, TriangleAlert, X,
} from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { fieldApi, ApiError } from "@/lib/api"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { FieldAccount, FieldWorker } from "@/lib/types"
import { workerState } from "@/map/workers"
import { Chip, type Tone } from "@/components/status-badge"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

// Worker location tracking, set up by the owner (central/field.py).
//
// Workers run the off-the-shelf Traccar Client — free, open source, Android and
// iOS — rather than anything of ours. So the owner's job here is not "enable a
// feature": it is provisioning somebody else's phone, over the phone. The panel
// is written as STEPS AN OWNER READS OUT, not a paragraph, because that is
// literally how it gets used.

/** What the tracker has to be set to. These are the duty cycle we designed and
 *  the app's defaults are NOT it — 90 s / 30 m is what makes a trail readable
 *  without draining a handset, and offline buffering is what makes a fix survive
 *  the dead zones the crew drives through every day. */
const TRACCAR_SETTINGS: Array<[string, string]> = [
  ["Frequency", "90 seconds"],
  ["Distance", "30 metres (whichever comes first)"],
  ["Accuracy", "High"],
  ["Offline buffering", "ON (this is what saves fixes in a dead zone)"],
]

function CopyButton({ value, label }: { value: string; label: string }) {
  const [done, setDone] = useState(false)
  return (
    <Button
      variant="ghost" size="icon" className="size-7 shrink-0"
      title={`Copy ${label}`}
      onClick={() => {
        navigator.clipboard.writeText(value).then(
          () => { setDone(true); setTimeout(() => setDone(false), 1500) },
          () => toast.error("Couldn't copy. Select the text instead."),
        )
      }}>
      {done ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
    </Button>
  )
}

/** The QR a phone is provisioned by scanning.
 *
 *  Generated CLIENT-SIDE (`qrcode`, already a dependency for the 2FA enrolment)
 *  rather than server-side, because central is pure stdlib and this feature is
 *  not worth a Python QR library. It encodes the URL WITH the token, so what
 *  the tech scans is the whole credential — which is also why it only ever
 *  exists in the browser tab that just issued the token, and never after. */
function TokenQr({ url }: { url: string }) {
  const [src, setSrc] = useState("")
  useEffect(() => {
    QRCode.toDataURL(url, { width: 220, margin: 1 }).then(setSrc).catch(() => setSrc(""))
  }, [url])
  if (!src) return null
  return <img src={src} alt="Tracker setup QR code" className="size-40 rounded-md bg-white p-1.5" />
}

/** The one-time reveal. Same contract as a probe token, and it says so on
 *  screen: only a SHA-256 hash is stored, so this string cannot be shown again —
 *  a lost one is replaced by rotating, never recovered. */
function IssuedToken({ username, token, serverUrl, onDone }: {
  username: string; token: string; serverUrl: string; onDone: () => void
}) {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-warning/40 bg-warning/[0.06] p-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-semibold">{username}&rsquo;s identifier</p>
          <p className="text-xs text-warning">
            Shown once. We store only a hash of it. If it&rsquo;s lost, rotate for a new
            one; it can never be read back.
          </p>
        </div>
        <Button variant="ghost" size="icon" className="size-7" onClick={onDone} title="Done">
          <X className="size-3.5" />
        </Button>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:gap-4">
        <TokenQr url={`${serverUrl}?id=${encodeURIComponent(token)}`} />
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex flex-col gap-1">
            <p className="text-2xs font-medium tracking-wide text-muted-foreground uppercase">
              Server URL · the same for everyone
            </p>
            <div className="flex items-center gap-1">
              <code className="min-w-0 flex-1 truncate rounded bg-muted px-2 py-1 font-mono text-xs">
                {serverUrl}
              </code>
              <CopyButton value={serverUrl} label="server URL" />
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <p className="text-2xs font-medium tracking-wide text-muted-foreground uppercase">
              Device identifier · {username} only
            </p>
            <div className="flex items-center gap-1">
              <code className="min-w-0 flex-1 truncate rounded bg-muted px-2 py-1 font-mono text-xs">
                {token}
              </code>
              <CopyButton value={token} label="identifier" />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Scanning the QR fills both in at once. Otherwise type the URL into
            Traccar Client&rsquo;s <span className="font-medium">Server URL</span> and
            the identifier into <span className="font-medium">Device identifier</span>.
          </p>
        </div>
      </div>
    </div>
  )
}

/** Live state per account, in words.
 *
 *  The same four states the map draws, from the same `workerState` — one rule,
 *  or the panel and the map would disagree about whose phone is working. That
 *  disagreement is the whole failure this feature is meant to surface, so the
 *  two surfaces reporting it differently would be worse than either being
 *  absent. */
function LiveChip({ w, freshS, now }: { w: FieldWorker; freshS: number; now: number }) {
  const state = workerState(w, freshS, now)
  const seen = w.last_fix ? ago(w.last_fix.ts) : null
  const map: Record<string, [Tone, string]> = {
    live: ["success", `here now · ${seen}`],
    // The alarm. On shift and nothing arriving is almost always the handset's
    // OEM battery manager, which no server-side code can fix — so the chip
    // names it rather than saying "offline".
    quiet: ["warning", seen ? `on shift, gone quiet · ${seen}` : "on shift, never reported"],
    off: ["muted", seen ? `off shift · last seen ${seen}` : "off shift"],
    never: ["muted", "never reported"],
  }
  const [tone, label] = map[state]
  return <Chip tone={tone}>{label}</Chip>
}

export function FieldTrackingCard({ org }: { org: string }) {
  const queryClient = useQueryClient()
  const [issued, setIssued] = useState<{ username: string; token: string } | null>(null)
  const [revoking, setRevoking] = useState<FieldAccount | null>(null)
  const now = Date.now()

  const { data, isLoading } = useQuery({
    queryKey: ["field-tokens", org],
    queryFn: () => fieldApi.tokens(org),
    enabled: !!org,
  })
  // The live half. Fetched here as well as on the map because THIS is where an
  // owner comes when tracking looks broken, and "issued 3 weeks ago" with no
  // word on whether anything has ever arrived is exactly the gap that makes a
  // setup panel useless.
  const live = useQuery({
    queryKey: ["field-workers", org],
    queryFn: () => fieldApi.workers(org),
    enabled: !!org,
    refetchInterval: 60_000,
  })
  const byUser = new Map((live.data?.workers ?? []).map((w) => [w.user_id, w]))
  const freshS = live.data?.fresh_s ?? 300

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["field-tokens"] })
    queryClient.invalidateQueries({ queryKey: ["field-workers"] })
  }
  const issue = useMutation({
    mutationFn: (a: FieldAccount) => fieldApi.issueToken(a.user_id, org),
    onSuccess: (r, a) => { setIssued({ username: a.username, token: r.token }); invalidate() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't issue an identifier"),
  })
  const revoke = useMutation({
    mutationFn: (a: FieldAccount) => fieldApi.revokeToken(a.user_id, org),
    onSuccess: () => { toast.success("Tracker identifier switched off"); invalidate() },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't revoke"),
  })

  const accounts = data?.accounts ?? []
  const serverUrl = data?.server_url ?? ""

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Navigation className="size-4 text-muted-foreground" /> Location tracking
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="max-w-lg text-xs text-muted-foreground">
          Workers run <span className="font-medium">Traccar Client</span> (free, both
          app stores) on their own phones. You see everyone on the{" "}
          <span className="font-medium">Map</span> under Layers &rarr; Workers.
          Positions are kept{" "}
          <span className="font-medium">{data?.retention_days ?? 7} days</span>, then
          deleted.
        </p>

        {/* The rule that makes the whole design honest, stated first because it
            is the one an owner has to be able to repeat to a worker: the app's
            own switch is the shift. Nothing is received when it is off — we did
            NOT build "always transmit and discard", because receiving somebody's
            evening and choosing not to store it is a far worse promise. */}
        <div className="flex gap-2 rounded-lg border border-border-strong bg-muted/40 p-3">
          <Navigation className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
          <p className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">
              The app&rsquo;s ON/OFF switch is the shift.
            </span>{" "}
            When a worker switches it off, the phone sends nothing at all, so there
            is no off-shift data here to keep or discard. The{" "}
            <span className="font-medium">Start shift</span> button on the Survey
            screen is a separate, deliberate record: when somebody marks on-shift
            and no positions arrive, that gap is how you find out their phone
            killed the tracker.
          </p>
        </div>

        {issued && (
          <IssuedToken username={issued.username} token={issued.token}
            serverUrl={serverUrl} onDone={() => setIssued(null)} />
        )}

        {/* Per-account setup. Driven off the login accounts, not off the issued
            credentials — a list of only the ones already set up can never show
            you who is still missing. */}
        <div className="flex flex-col gap-0 overflow-hidden rounded-lg border">
          {isLoading && <div className="p-3"><Skeleton className="h-10 w-full" /></div>}
          {!isLoading && accounts.length === 0 && (
            <p className="p-3 text-xs text-muted-foreground">
              No active accounts in this org yet. Add one under Login accounts above.
            </p>
          )}
          {accounts.map((a) => {
            const w = byUser.get(a.user_id)
            const active = !!a.issued_at && !a.revoked_at
            return (
              <div key={a.user_id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t px-3 py-2.5 first:border-t-0">
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold">{a.username}</p>
                  <p className="text-2xs text-muted-foreground capitalize">{a.role}</p>
                </div>
                {/* Three different facts, never collapsed: does a credential
                    exist, was it withdrawn, and is anything actually arriving. */}
                {active
                  ? <span className="text-2xs text-muted-foreground">
                      identifier issued {ago(a.issued_at!)}
                    </span>
                  : a.revoked_at
                    ? <span className="text-2xs text-muted-foreground">
                        revoked {ago(a.revoked_at)}
                      </span>
                    : <span className="text-2xs text-faint-foreground">not set up</span>}
                {active && w && <LiveChip w={w} freshS={freshS} now={now} />}
                {active && w?.last_fix?.battery_pct != null && w.last_fix.battery_pct <= 20 && (
                  <span className="flex items-center gap-1 text-2xs text-warning">
                    <BatteryWarning className="size-3.5" /> {w.last_fix.battery_pct}%
                  </span>
                )}
                <div className="ml-auto flex shrink-0 items-center gap-1">
                  <Button variant="outline" size="sm" disabled={issue.isPending}
                    title={active
                      ? "Issue a NEW identifier. The old one stops working immediately."
                      : "Issue this account's tracker identifier"}
                    onClick={() => issue.mutate(a)}>
                    {active
                      ? <><RotateCw className="size-3.5" /> Rotate</>
                      : <><KeyRound className="size-3.5" /> Set up</>}
                  </Button>
                  {active && (
                    <Button variant="ghost" size="icon" className="size-7"
                      title="Switch this identifier off" onClick={() => setRevoking(a)}>
                      <X className="size-3.5" />
                    </Button>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        {/* Read this out to a worker. Steps, not a paragraph — the owner is on
            the phone to somebody standing next to a van. */}
        <div className="flex flex-col gap-2">
          <p className="text-xs font-semibold">Setting up a worker&rsquo;s phone</p>
          <ol className="flex list-decimal flex-col gap-1.5 pl-5 text-xs text-muted-foreground">
            <li>Install <span className="font-medium text-foreground">Traccar Client</span> from the Play Store (or the App Store).</li>
            <li>Open it and go to <span className="font-medium text-foreground">Settings</span>.</li>
            <li>
              Scan the QR from their <span className="font-medium text-foreground">Set up</span> button
              above, or type the Server URL and Device identifier in by hand.
            </li>
            <li>
              Set these. The app&rsquo;s defaults are not right for us:
              <ul className="mt-1 flex flex-col gap-0.5">
                {TRACCAR_SETTINGS.map(([k, v]) => (
                  <li key={k} className="flex gap-1.5">
                    <span className="font-medium text-foreground">{k}:</span>
                    <span>{v}</span>
                  </li>
                ))}
              </ul>
            </li>
            <li>
              Go back and turn the big <span className="font-medium text-foreground">service switch ON</span>.
              That switch is the shift: off means the phone sends nothing.
            </li>
            <li>
              Check the <span className="font-medium text-foreground">Map</span> &rarr; Layers
              &rarr; Workers within a couple of minutes. If nothing shows up, it is
              almost certainly the next bit.
            </li>
          </ol>
        </div>

        {/* The single most likely reason tracking will look broken, and no
            server-side code can fix any of it. It gets its own block, and a
            warning tone, because an owner who does not know this will conclude
            the feature is broken and stop using it. */}
        <div className="flex gap-2 rounded-lg border border-warning/40 bg-warning/[0.06] p-3">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" />
          <div className="flex flex-col gap-1.5 text-xs">
            <p className="font-semibold text-warning">
              Xiaomi / Redmi / Poco, Realme, Vivo, Oppo and OnePlus phones kill the
              tracker silently
            </p>
            <p className="text-muted-foreground">
              Their &ldquo;autostart&rdquo; and battery managers stop background apps with
              no warning and no error, so the app still looks switched on. On the
              worker&rsquo;s phone, do all three:
            </p>
            <ul className="flex list-disc flex-col gap-1 pl-4 text-muted-foreground">
              <li>
                <span className="font-medium text-foreground">Autostart:</span>{" "}
                Settings &rarr; Apps &rarr; Traccar Client &rarr; turn{" "}
                <span className="font-medium">Autostart</span> on (Xiaomi calls it
                Autostart, Oppo/Realme call it Startup manager, Vivo calls it
                Auto-start).
              </li>
              <li>
                <span className="font-medium text-foreground">Battery:</span> set
                battery usage to <span className="font-medium">Unrestricted</span> /
                &ldquo;No restrictions&rdquo; / &ldquo;Allow background activity&rdquo;.
              </li>
              <li>
                <span className="font-medium text-foreground">Recents lock:</span> open
                the recent-apps view, and lock/pin the Traccar card so clearing
                recents doesn&rsquo;t close it.
              </li>
            </ul>
            <p className="text-muted-foreground">
              A worker who marks a shift and shows &ldquo;gone quiet&rdquo; here almost
              always needs this done.
            </p>
          </div>
        </div>

        <ConfirmDialog
          open={!!revoking}
          onOpenChange={(o) => { if (!o) setRevoking(null) }}
          title={`Switch off ${revoking?.username ?? ""}'s tracker identifier?`}
          description="Their phone stops being able to report immediately. Their existing positions stay until they age out of the retention window. You can issue a new identifier at any time."
          onConfirm={() => { if (revoking) revoke.mutate(revoking) }}
        />
      </CardContent>
    </Card>
  )
}

/** The worker's own Start/End shift control (`/survey`).
 *
 *  Two taps to be on shift — the app's switch AND this — is DELIBERATE, not
 *  redundancy. The whole point is the discrepancy: when this says on-shift and
 *  no fixes arrive, that gap is the "the OEM battery manager killed the service"
 *  alarm, and nothing on the server could detect it otherwise.
 */
export function ShiftButton({ className }: { className?: string }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  // A shift belongs to an ORG account — a superadmin has no org and so no shift
  // of its own (the server 400s). Gated here rather than left to fail quietly,
  // so the platform admin's Survey page isn't polling an endpoint that can never
  // answer it.
  const canHaveShift = !!user && !user.is_superadmin && !!user.org_id
  const { data, isLoading } = useQuery({
    queryKey: ["field-shift"],
    queryFn: () => fieldApi.shift(),
    enabled: canHaveShift,
    refetchInterval: 120_000,
  })
  const set = useMutation({
    mutationFn: (action: "start" | "end") => fieldApi.setShift(action),
    onSuccess: (_r, action) => {
      queryClient.invalidateQueries({ queryKey: ["field-shift"] })
      queryClient.invalidateQueries({ queryKey: ["field-workers"] })
      toast.success(action === "start" ? "On shift" : "Shift ended")
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "Couldn't update your shift"),
  })

  if (!canHaveShift || isLoading || !data) return null
  const on = data.on_shift

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <Button
        variant={on ? "outline" : "default"}
        size="sm"
        disabled={set.isPending}
        onClick={() => set.mutate(on ? "end" : "start")}>
        <Navigation className={cn("size-3.5", on && "text-success")} />
        {on ? "End shift" : "Start shift"}
      </Button>
      {/* Never claims the tracker is running — this button knows only what was
          declared. Saying "on shift" while the phone sends nothing is the exact
          lie the two-tap design exists to expose, so the copy stays about the
          declaration, and the missing half is named when it is missing. */}
      <p className="text-2xs text-muted-foreground">
        {on
          ? `on shift · started ${ago(data.started_at)}`
          : "your location isn't being recorded"}
        {!data.has_token && " · no tracker set up for you yet"}
      </p>
    </div>
  )
}
