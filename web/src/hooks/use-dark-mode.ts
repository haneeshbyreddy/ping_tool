import { useEffect, useState } from "react"

const isDark = () =>
  typeof document !== "undefined" && document.documentElement.classList.contains("dark")

export function useDarkMode(): boolean {
  const [dark, setDark] = useState(isDark)
  useEffect(() => {
    const el = document.documentElement
    const obs = new MutationObserver(() => setDark(el.classList.contains("dark")))
    obs.observe(el, { attributes: true, attributeFilter: ["class"] })
    setDark(el.classList.contains("dark"))
    return () => obs.disconnect()
  }, [])
  return dark
}
