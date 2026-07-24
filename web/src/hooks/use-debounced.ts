import { useEffect, useState } from "react"

/** Trailing-edge debounce of a changing value — the settled value only, so a
    query keyed on it fires once the user stops typing rather than per keystroke.
    Shared by the map's place search and the Network page's ONU/MAC search; both
    are polite clients of something they shouldn't hammer (Nominatim, a fleet's
    full onu_optics table). */
export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}
