import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Camera, RotateCw } from "lucide-react"
import { useState } from "react"

import { Chip, StatusDot, type Tone } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Switch } from "@/components/ui/switch"
import { nvrApi } from "@/lib/api"
import { ago, isDownState, isFresh } from "@/lib/format"
import type { NvrChannel, NvrChannelsResponse, OrgDevice } from "@/lib/types"
import { cn } from "@/lib/utils"

function channelTone(ch: NvrChannel): Tone {
  if (!ch.enabled) return "muted"
  if (ch.state === "online") return "success"
  if (ch.state === "offline") return ch.monitored ? "destructive" : "muted"
  return "muted"
}

function channelNote(ch: NvrChannel): string | null {
  if (!ch.enabled) return "disabled"
  if (ch.state === "offline") {
    return ch.last_online_at ? `dark ${ago(ch.last_online_at)}` : "dark"
  }
  if (ch.state === "unknown") return "state unknown"
  return null
}

function missingPieces(body: NvrChannelsResponse): string[] {
  const out: string[] = []
  if (!body.vendor) {
    out.push("Set the NVR brand on the device form — the brand picks the "
      + "recipe the channel read uses.")
  } else if (!body.profile) {
    out.push(`No recipe for brand “${body.vendor}” on this server.`)
  }
  if (!body.has_credentials) {
    out.push("Store the NVR's web login under Credentials so central can "
      + "sign in to it.")
  }
  if (!body.has_node) out.push("Assign a probe to this device.")
  if (!body.web_proxy) {
    out.push("This organisation doesn't have the device web-UI tunnel "
      + "enabled (a platform setting).")
  }
  return out
}

function ScrapeNote({ body }: { body: NvrChannelsResponse }) {
  const s = body.scrape
  if (!s) {
    const missing = missingPieces(body)
    if (missing.length === 0) {
      return (
        <p className="px-4 py-2 text-xs text-muted-foreground">
          No channel read yet — the first sweep runs within a few minutes.
        </p>
      )
    }
    return (
      <div className="flex flex-col gap-1 px-4 py-2">
        {missing.map((m) => (
          <p key={m} className="text-xs text-muted-foreground">{m}</p>
        ))}
      </div>
    )
  }
  if (s.state === "ok") {
    return (
      <p className="px-4 py-2 text-xs text-faint-foreground">
        Read off the NVR's own channel table · {ago(s.updated_at)}
      </p>
    )
  }
  const tone = s.state === "partial" || s.state === "skipped"
    ? "text-warning" : "text-destructive"
  return (
    <div className="px-4 py-2">
      <p className={cn("text-xs", tone)}>
        {s.state === "partial" ? "Partial read" : `Read ${s.state}`}
        {s.detail ? ` — ${s.detail}` : ""}
      </p>
      {s.last_ok_at && s.state !== "partial" && (
        <p className="text-xs text-faint-foreground">
          was working until {ago(s.last_ok_at)}
        </p>
      )}
    </div>
  )
}

export function CamerasPanel({ device }: { device: OrgDevice }) {
  const queryClient = useQueryClient()
  const [kicked, setKicked] = useState(false)
  const [snap, setSnap] = useState<NvrChannel | null>(null)
  const [snapUrl, setSnapUrl] = useState<string | null>(null)
  const [snapErr, setSnapErr] = useState<string | null>(null)
  const [snapBusy, setSnapBusy] = useState(false)

  const dropUrl = () => setSnapUrl((old) => {
    if (old) URL.revokeObjectURL(old)
    return null
  })

  const loadFrame = async (ch: NvrChannel) => {
    setSnapBusy(true)
    setSnapErr(null)
    try {
      const r = await fetch(
        `/api/inventory/nvr-snapshot?device_id=${device.id}`
        + `&channel_no=${ch.channel_no}&t=${Date.now()}`,
        { credentials: "same-origin" })
      if (!r.ok) {
        let msg = `the frame request failed (${r.status})`
        try { msg = (await r.json()).error ?? msg } catch { /* keep */ }
        dropUrl()
        setSnapErr(msg)
      } else {
        const blob = await r.blob()
        setSnapUrl((old) => {
          if (old) URL.revokeObjectURL(old)
          return URL.createObjectURL(blob)
        })
      }
    } catch {
      setSnapErr("the frame request failed")
    } finally {
      setSnapBusy(false)
    }
  }

  const openSnap = (ch: NvrChannel) => {
    setSnap(ch)
    dropUrl()
    setSnapErr(null)
    void loadFrame(ch)
  }
  const closeSnap = () => {
    setSnap(null)
    dropUrl()
    setSnapErr(null)
  }
  const q = useQuery({
    queryKey: ["nvr-channels", device.id],
    queryFn: () => nvrApi.channels(device.id),
    refetchInterval: 60_000,
  })
  const toggleWatch = useMutation({
    mutationFn: (ch: NvrChannel) =>
      nvrApi.setWatch(device.id, ch.channel_no, !ch.monitored),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["nvr-channels", device.id] })
      void queryClient.invalidateQueries({ queryKey: ["inventory"] })
    },
  })
  if (q.isError) {
    return (
      <p className="px-4 py-3 text-xs text-destructive">
        Couldn't load the camera list. Retrying…
      </p>
    )
  }
  if (!q.data) {
    return <p className="px-4 py-3 text-xs text-muted-foreground">Loading…</p>
  }
  const body = q.data
  const channels = body.channels
  const isDown = isDownState(device.state)
  const stale = !isFresh(body.scrape?.updated_at ?? null)
  const online = channels.filter((c) => c.enabled && c.state === "online").length
  const dark = channels.filter(
    (c) => c.enabled && c.monitored && c.state === "offline").length
  const mutedDark = channels.filter(
    (c) => c.enabled && !c.monitored && c.state === "offline").length
  const disabled = channels.filter((c) => !c.enabled).length
  const unknown = channels.filter((c) => c.enabled && c.state === "unknown").length

  const refresh = async () => {
    setKicked(true)
    try {
      await nvrApi.refresh(device.id)
      window.setTimeout(() => {
        void queryClient.invalidateQueries({
          queryKey: ["nvr-channels", device.id] })
        setKicked(false)
      }, 8_000)
    } catch {
      setKicked(false)
    }
  }

  return (
    <div className="flex flex-col">
      {isDown && (
        <p className="border-b px-4 py-2 text-xs text-warning">
          Camera states are frozen while the NVR is unreachable — as of{" "}
          {body.scrape?.updated_at ? ago(body.scrape.updated_at) : "the last read"}.
          The outage owns this page.
        </p>
      )}
      {channels.length > 0 && (
        <div className={cn("flex flex-wrap items-center gap-2 border-b px-4 py-2",
          isDown && "wisp-frozen")}>
          <Chip tone="success">{online} online</Chip>
          {dark > 0 && <Chip tone="destructive">{dark} dark</Chip>}
          {mutedDark > 0 && <Chip tone="muted">{mutedDark} dark · not watched</Chip>}
          {unknown > 0 && <Chip tone="muted">{unknown} unknown</Chip>}
          {disabled > 0 && <Chip tone="muted">{disabled} disabled</Chip>}
          <span className="ml-auto text-2xs text-faint-foreground">
            {channels.length} of {body.scrape?.channels ?? channels.length} channels
          </span>
        </div>
      )}
      <div className={cn(isDown && "wisp-frozen")}>
        {channels.map((ch) => {
          const note = channelNote(ch)
          return (
            <div key={ch.channel_no}
              className="flex h-10 items-center gap-2 border-b px-4 last:border-b-0">
              <StatusDot tone={channelTone(ch)} />
              <span className="w-9 shrink-0 font-mono text-2xs text-faint-foreground">
                CH{ch.channel_no + 1}
              </span>
              <span className={cn("min-w-0 flex-1 truncate text-xs font-medium",
                (!ch.enabled || !ch.monitored) && "text-muted-foreground",
                !ch.enabled && "italic")}>
                {ch.name || "unnamed"}
              </span>
              {note && (
                <span className={cn("shrink-0 text-2xs",
                  ch.enabled && ch.monitored && ch.state === "offline"
                    ? "text-destructive" : "text-muted-foreground")}>
                  {note}
                </span>
              )}
              <span className="shrink-0 font-mono text-2xs text-muted-foreground">
                {ch.ip_address ?? "—"}
              </span>
              {ch.enabled && body.can_refresh && (
                <Button variant="ghost" size="icon"
                  className="size-6 shrink-0 text-muted-foreground"
                  title="View a frame from this camera"
                  onClick={() => openSnap(ch)}>
                  <Camera className="size-3.5" />
                </Button>
              )}
              {ch.enabled && (
                <Switch checked={ch.monitored} className="shrink-0 scale-75"
                  title={ch.monitored
                    ? "Watched — a WhatsApp page goes out when it drops. Click to mute."
                    : "Not watched — it can go dark without a page. Click to watch."}
                  onCheckedChange={() => toggleWatch.mutate(ch)} />
              )}
            </div>
          )
        })}
        {channels.length === 0 && body.scrape?.state === "ok" && (
          <p className="px-4 py-3 text-xs text-muted-foreground">
            The NVR reports no IP cameras configured.
          </p>
        )}
      </div>
      {!isDown && stale && channels.length > 0 && (
        <p className="px-4 pt-2 text-xs text-warning">
          This list is stale — the last successful read is{" "}
          {ago(body.scrape?.updated_at ?? null)}.
        </p>
      )}
      <div className="flex items-center">
        <div className="min-w-0 flex-1"><ScrapeNote body={body} /></div>
        {body.can_refresh && (
          <Button variant="ghost" size="sm" className="mr-2 shrink-0 gap-1.5"
            disabled={body.refreshing || kicked || isDown}
            onClick={() => void refresh()}>
            <RotateCw className={cn("size-3.5",
              (body.refreshing || kicked) && "animate-spin")} />
            {body.refreshing || kicked ? "Reading…" : "Refresh now"}
          </Button>
        )}
      </div>
      <Dialog open={!!snap} onOpenChange={(open) => { if (!open) closeSnap() }}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="text-sm">
              {snap ? `CH${snap.channel_no + 1}`
                + (snap.name ? ` · ${snap.name}` : "") : ""}
            </DialogTitle>
          </DialogHeader>
          {snapBusy && (
            <p className="py-6 text-center text-xs text-muted-foreground">
              Fetching a frame off the NVR…
            </p>
          )}
          {!snapBusy && snapErr && (
            <p className="py-4 text-xs text-warning">{snapErr}</p>
          )}
          {!snapBusy && snapUrl && (
            <img src={snapUrl} alt="camera frame"
              className="w-full rounded-md border" />
          )}
          <div className="flex items-center justify-between gap-3">
            <p className="text-2xs text-faint-foreground">
              Fetched just now through the probe — nothing is stored.
            </p>
            <Button variant="ghost" size="sm" className="shrink-0 gap-1.5"
              disabled={snapBusy}
              onClick={() => snap && void loadFrame(snap)}>
              <RotateCw className={cn("size-3.5", snapBusy && "animate-spin")} />
              Refresh
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
