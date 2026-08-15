// One subscriber's own Rx and presence history (Wave 2).
//
// Kept out of lib/api.ts and lib/types.ts on purpose: `request` is exported for
// exactly this, and one component's endpoint plus its reply shape travelling
// together is easier to keep honest than a wrapper in one file and its types in
// another. Everything here mirrors the pinned contract verbatim.
//
// Time on the wire: `since`/`until`/`recording_since` and the outage windows are
// ISO naive UTC strings (toUtcDate); bucket and event stamps are epoch SECONDS,
// the historian's own convention. Nothing here converts — the chart does, once.
import { request } from "@/lib/api"

export type OnuHistoryTier = "hour" | "day"

// A bucket is one slot on the tier's grid. It is present only if a walk landed:
// a MISSING bucket is a gap ("the OLT's walk did not arrive"), never a zero.
// `samples` counts the walks; `online` how many of them saw the ONU up; `rx_n`
// how many carried a dBm. Those are three different questions and the chart
// renders them as three different sentences — an ONU that was walked while dark
// (rx_n 0, online 0) must never look like one nobody walked at all.
export interface OnuHistoryBucket {
  t: number
  samples: number
  online: number
  rx_n: number
  rx_avg: number | null
  rx_min: number | null
  rx_max: number | null
}

// The PON's median per bucket: the drops.py "compare against the drops beside
// it, never a modelled budget" doctrine given a time axis. No vendor here
// publishes launch power, so an absolute budget would be a guess wearing a
// decimal point.
//
// IT IS THE WHOLE PON, THIS ONU INCLUDED (hist_pon_* is written per PON, not
// per PON-minus-one), so it is labelled "PON median" everywhere and NEVER
// "sibling median" — on a PON with a handful of drops this one pulls the line
// it is being read against, and a leave-one-out claim would be false.
// `rx_n` counts the sweeps that contributed a median; `rx_med` is null at 0.
export interface OnuSiblingBucket {
  t: number
  rx_med: number | null
  rx_n: number
}

// The ONU's own state transitions. RAW vendor vocabulary, never normalised
// (online/offline/dying_gasp/los/unknown), because `unknown` is a real state
// this fleet reports on purpose. `old: null` means FIRST SEEN, which is a
// different sentence from "it was unknown" and must not be printed as one.
export interface OnuStateEvent {
  ts: number
  old: string | null
  new: string
}

// The OLT's own down windows, restated on this drop's axis, which is why the
// chart shades them and says so: nothing was measured then, and none of it is
// the subscriber's fault. Includes UNREACHABLE spans (the OLT's parent was
// down), which is correct for explaining a gap and is why the copy says "not
// reachable" rather than claiming the box itself was proven down.
export interface OnuOutageWindow {
  start: string
  end: string | null
}

export interface OnuHistoryReply {
  since: string
  until: string
  tier: OnuHistoryTier
  recording_since: string | null
  onu: { onu_key: string; pon_port: string | null }
  // Per-OLT, from the server. NEVER re-derive a severity from rx_dbm here:
  // thresholds are per box, so a second rule would call one drop healthy while
  // the Optical tab calls it critical.
  thresholds: { warn: number; crit: number }
  buckets: OnuHistoryBucket[]
  sibling: OnuSiblingBucket[]
  events: OnuStateEvent[]
  outages: OnuOutageWindow[]
}

export const onuHistoryApi = {
  get: (deviceId: number, onuKey: string, days: number) =>
    request<OnuHistoryReply>(
      `/api/history/onu?device_id=${deviceId}`
      + `&onu=${encodeURIComponent(onuKey)}&days=${days}`),
}
