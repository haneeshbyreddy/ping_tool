const STORAGE_KEY = "wisp-central-theme"
export type ThemeMode = "dark" | "light"

export function getStoredTheme(): ThemeMode {
  const stored = localStorage.getItem(STORAGE_KEY)
  return stored === "light" ? "light" : "dark"
}

export function applyTheme(mode: ThemeMode) {
  document.documentElement.classList.toggle("dark", mode === "dark")
  localStorage.setItem(STORAGE_KEY, mode)
}
