import { useCallback, useEffect, useRef, useState } from "react"

/** Above this a fix is a cell-tower/wifi estimate rather than a position, and a
 *  splitter pinned that far off is a crew walking the wrong side of a road. It
 *  demotes the save button — it never blocks it. Blocking is how coordinates end
 *  up in a WhatsApp message instead of the database. Mirrors
 *  `inventory.GPS_ACCURACY_HINT_M`. */
export const GOOD_FIX_M = 25

/** Long enough for GPS to overtake the cell/wifi estimate the first callback
 *  carries, short enough that a worker isn't standing in the sun waiting. We
 *  stop early once the fix is comfortably good. */
const SETTLE_MS = 12_000
const EXCELLENT_M = 8

export interface GpsFix {
  lat: number
  lng: number
  /** metres, 95% confidence — what `coords.accuracy` means per the spec. */
  accuracy: number
  at: number
}

export type GpsPhase = "idle" | "acquiring" | "settled" | "error"

/** Acquire a position the way a handset actually produces one.
 *
 *  `getCurrentPosition` returns the FIRST thing available, which on a phone is
 *  normally a cell/wifi estimate at 30–80 m; the GPS chip overtakes it a few
 *  seconds later. Taking that first callback as the answer is the single easiest
 *  way to fill a map with pins that look precise and are not — so this watches,
 *  keeps the BEST fix seen, and reports progress while it converges.
 *
 *  It never resolves to "good enough" on its own: `phase` and `fix.accuracy` are
 *  handed up so the UI can say what it has and let the person standing there
 *  decide. */
export function useGpsFix() {
  const [fix, setFix] = useState<GpsFix | null>(null)
  const [phase, setPhase] = useState<GpsPhase>("idle")
  const [error, setError] = useState<string | null>(null)
  const watchId = useRef<number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const best = useRef<GpsFix | null>(null)

  const stop = useCallback(() => {
    if (watchId.current !== null) {
      navigator.geolocation.clearWatch(watchId.current)
      watchId.current = null
    }
    if (timer.current !== null) {
      clearTimeout(timer.current)
      timer.current = null
    }
  }, [])

  // A watch left running drains the battery of a phone that spends all day in
  // the field, so it stops on unmount as well as on settle.
  useEffect(() => stop, [stop])

  const start = useCallback(() => {
    if (!navigator.geolocation) {
      setPhase("error")
      setError("This browser can't provide a location")
      return
    }
    stop()
    best.current = null
    setFix(null)
    setError(null)
    setPhase("acquiring")

    const settle = () => {
      stop()
      setPhase(best.current ? "settled" : "error")
      if (!best.current) setError("Couldn't get a fix. Move into the open and retry")
    }

    timer.current = setTimeout(settle, SETTLE_MS)

    watchId.current = navigator.geolocation.watchPosition(
      (pos) => {
        const next: GpsFix = {
          lat: pos.coords.latitude,
          lng: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
          at: Date.now(),
        }
        // Keep the tightest fix, not the latest — accuracy does not improve
        // monotonically, and a late loose reading must not undo a good one.
        if (!best.current || next.accuracy < best.current.accuracy) {
          best.current = next
          setFix(next)
        }
        if (next.accuracy <= EXCELLENT_M) settle()
      },
      (err) => {
        // Only a failure with nothing banked is fatal: once a usable fix is in
        // hand, a later timeout is noise.
        if (best.current) { settle(); return }
        stop()
        setPhase("error")
        if (err.code === err.PERMISSION_DENIED) {
          setError(window.isSecureContext
            ? "Location is blocked. Allow it for this site in the browser's address bar, then retry"
            : "Location needs HTTPS. Open the dashboard over https to survey")
        } else if (err.code === err.TIMEOUT) {
          setError("Timed out getting a fix. Move into the open and retry")
        } else {
          setError("Your phone couldn't determine a location")
        }
      },
      { enableHighAccuracy: true, timeout: SETTLE_MS, maximumAge: 0 },
    )
  }, [stop])

  const reset = useCallback(() => {
    stop()
    best.current = null
    setFix(null)
    setError(null)
    setPhase("idle")
  }, [stop])

  return { fix, phase, error, start, reset, stop }
}
