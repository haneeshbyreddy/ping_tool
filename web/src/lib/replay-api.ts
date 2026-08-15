// The replay endpoint's wrapper and reply types. Kept out of lib/api.ts and
// lib/types.ts deliberately: this is one page's read, and `request` is
// exported precisely so a feature can own its own surface.
import { aq, request } from "@/lib/api"
import { buildReconstruction, type EntityInput, type Reconstruction } from "@/lib/replay"

export interface ReplaySpan {
  outage_id: number
  device_id: number
  start: number
  end: number | null
  state: string
}

export interface ReplayFloor {
  device_id: number
  since: number | null
}

export interface ReplayBlind {
  device_id: number
  start: number
  end: number | null
}

export interface ReplayReply {
  since: number
  until: number
  now: number
  days: number
  org_since: number | null
  devices: ReplayFloor[]
  spans: ReplaySpan[]
  blind: ReplayBlind[]
}

export const REPLAY_WINDOWS = [1, 7] as const
export type ReplayDays = (typeof REPLAY_WINDOWS)[number]

export const replayApi = {
  window: (org: string | null | undefined, days: number) =>
    request<ReplayReply>(`/api/history/replay?days=${days}${aq(org)}`),
}

// The one place the wire shape becomes the reconstruction's input. An
// UNREACHABLE span renders as down-family (the live map does) but is marked
// `own: false`, so it can never be COUNTED as this device's own downtime —
// the rule `device_reliability` keeps, carried onto the client so the
// accumulation layer cannot restate one OLT's outage on every box behind it.
export function reconstruct(reply: ReplayReply): Reconstruction {
  const spans = new Map<number, EntityInput["down"]>()
  for (const s of reply.spans) {
    const list = spans.get(s.device_id) ?? []
    list.push({ start: s.start, end: s.end, own: s.state !== "UNREACHABLE" })
    spans.set(s.device_id, list)
  }
  const blind = new Map<number, EntityInput["down"]>()
  for (const b of reply.blind) {
    const list = blind.get(b.device_id) ?? []
    list.push({ start: b.start, end: b.end })
    blind.set(b.device_id, list)
  }
  const entities: EntityInput[] = reply.devices.map((d) => ({
    id: d.device_id,
    since: d.since,
    down: spans.get(d.device_id) ?? [],
    blind: blind.get(d.device_id) ?? [],
  }))
  return buildReconstruction(entities, {
    since: reply.since, until: reply.until, now: reply.now,
    orgSince: reply.org_since,
  })
}
