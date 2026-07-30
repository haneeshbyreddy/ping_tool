import * as React from "react"

const MOBILE_BREAKPOINT = 768

/** Read the viewport SYNCHRONOUSLY on first render, not in an effect.
 *
 *  The old version started `undefined` and settled after mount, so the first
 *  paint always claimed "desktop". That was harmless while this only drove the
 *  sidebar's drawer/rail choice, but a worker's mobile session now renders a
 *  DIFFERENT shell — an effect-settled value flashes the full desktop chrome
 *  for a frame before collapsing to the survey view. There is no SSR here (a
 *  HashRouter SPA), so `window` is always present; the guard is belt-and-braces. */
function query(): boolean {
  if (typeof window === "undefined") return false
  return window.innerWidth < MOBILE_BREAKPOINT
}

export function useIsMobile() {
  const [isMobile, setIsMobile] = React.useState<boolean>(query)

  React.useEffect(() => {
    const mql = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`)
    const onChange = () => setIsMobile(query())
    mql.addEventListener("change", onChange)
    onChange()
    return () => mql.removeEventListener("change", onChange)
  }, [])

  return isMobile
}
