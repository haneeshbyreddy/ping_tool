// Is the app currently in dark mode, reactively?
//
// The theme has no provider and no shared state: `lib/theme.ts:applyTheme`
// toggles a `.dark` class on <html> and writes localStorage, and THREE separate
// components (the account menu, the mobile user menu, the Appearance card) each
// hold their own copy of the mode. So the class on the root element is the only
// signal every toggle actually agrees on — observe that rather than adding a
// fourth private copy that can drift out of step.
//
// Needed because the Google basemap now requests DIFFERENT TILES per theme
// (a styled roadmap session), so a theme flip has to reach code, not just CSS.
import { useEffect, useState } from "react"

const isDark = () =>
  typeof document !== "undefined" && document.documentElement.classList.contains("dark")

export function useDarkMode(): boolean {
  const [dark, setDark] = useState(isDark)
  useEffect(() => {
    const el = document.documentElement
    const obs = new MutationObserver(() => setDark(el.classList.contains("dark")))
    obs.observe(el, { attributes: true, attributeFilter: ["class"] })
    // the class may have been toggled between first render and this effect
    setDark(el.classList.contains("dark"))
    return () => obs.disconnect()
  }, [])
  return dark
}
