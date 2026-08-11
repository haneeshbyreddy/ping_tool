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

function TokenQr({ url }: { url: string }) {
  const [src, setSrc] = useState("")
  useEffect(() => {
    QRCode.toDataURL(url, { width: 220, margin: 1 }).then(setSrc).catch(() => setSrc(""))
  }, [url])
  if (!src) return null
  return <img src={src} alt="Tracker setup QR code" className="size-40 rounded-md bg-white p-1.5" />
}

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

function LiveChip({ w, freshS, now }: { w: FieldWorker; freshS: number; now: number }) {
  const state = workerState(w, freshS, now)
  const seen = w.last_fix ? ago(w.last_fix.ts) : null
  const map: Record<string, [Tone, string]> = {
    live: ["success", `here now · ${seen}`],
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

export function ShiftButton({ className }: { className?: string }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
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
      <p className="text-2xs text-muted-foreground">
        {on
          ? `on shift · started ${ago(data.started_at)}`
          : "your location isn't being recorded"}
        {!data.has_token && " · no tracker set up for you yet"}
      </p>
    </div>
  )
}
