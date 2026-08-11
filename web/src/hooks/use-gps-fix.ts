import { useCallback, useEffect, useRef, useState } from "react"

export const GOOD_FIX_M = 25

const SETTLE_MS = 12_000
const EXCELLENT_M = 8

export interface GpsFix {
  lat: number
  lng: number
  accuracy: number
  at: number
}

export type GpsPhase = "idle" | "acquiring" | "settled" | "error"

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
        if (!best.current || next.accuracy < best.current.accuracy) {
          best.current = next
          setFix(next)
        }
        if (next.accuracy <= EXCELLENT_M) settle()
      },
      (err) => {
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
