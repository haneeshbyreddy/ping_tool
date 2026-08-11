import { List, LayoutGrid } from "lucide-react"
import { cn } from "@/lib/utils"

export type ViewMode = "list" | "grid"
const VIEW_KEY = "wisp:network:view"

export function loadView(): ViewMode {
  try {
    return localStorage.getItem(VIEW_KEY) === "grid" ? "grid" : "list"
  } catch {
    return "list"
  }
}

export function saveView(v: ViewMode) {
  try { localStorage.setItem(VIEW_KEY, v) } catch { /* private mode / quota */ }
}

export function ViewToggle({ view, onChange }: { view: ViewMode; onChange: (v: ViewMode) => void }) {
  return (
    <div className="flex items-center gap-0.5 rounded-md border p-0.5">
      {([["list", List], ["grid", LayoutGrid]] as const).map(([mode, Icon]) => (
        <button key={mode} type="button" onClick={() => onChange(mode)}
          aria-pressed={view === mode} title={`${mode.charAt(0).toUpperCase()}${mode.slice(1)} view`}
          className={cn("flex size-6 items-center justify-center rounded-sm transition-colors",
            view === mode ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>
          <Icon className="size-3.5" />
        </button>
      ))}
    </div>
  )
}
