import { useRef, useState } from "react"
import { X } from "lucide-react"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

/* Chip-style editor for org_devices.tags (free-form, ≤8 per device, ≤32 chars
   each — the server enforces the same). Enter/comma commits the typed tag;
   Backspace on an empty input pops the last chip; suggestions are the tags
   already in use across the caller's device list, filtered as you type. */
export function TagsInput({ value, onChange, suggestions, placeholder }: {
  value: string[]
  onChange: (tags: string[]) => void
  suggestions: string[]
  placeholder?: string
}) {
  const [draft, setDraft] = useState("")
  const [focused, setFocused] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const has = (t: string) => value.some((v) => v.toLowerCase() === t.toLowerCase())
  const add = (raw: string) => {
    const t = raw.trim().replace(/,/g, "")
    if (!t || t.length > 32 || value.length >= 8 || has(t)) return
    onChange([...value, t])
    setDraft("")
  }
  const remove = (t: string) => onChange(value.filter((v) => v !== t))

  const needle = draft.trim().toLowerCase()
  const pool = [...new Set(suggestions.filter(Boolean))].sort((a, b) => a.localeCompare(b))
  const offered = pool
    .filter((t) => !has(t) && (!needle || t.toLowerCase().includes(needle)))
    .slice(0, 8)

  return (
    <div className="flex flex-col gap-1">
      <div
        className={cn(
          "flex min-h-9 flex-wrap items-center gap-1 rounded-md border bg-transparent px-2 py-1",
          focused && "border-ring ring-ring/50 ring-[3px]",
        )}
        onClick={() => inputRef.current?.focus()}
      >
        {value.map((t) => (
          <span key={t}
            className="flex items-center gap-1 rounded-full border bg-muted px-2 py-0.5 text-2xs font-medium">
            {t}
            <button type="button" aria-label={`Remove tag ${t}`}
              className="text-muted-foreground hover:text-foreground"
              onClick={(e) => { e.stopPropagation(); remove(t) }}>
              <X className="size-3" />
            </button>
          </span>
        ))}
        <Input
          ref={inputRef}
          value={draft}
          placeholder={value.length === 0 ? (placeholder ?? "add tags…") : ""}
          className="h-6 min-w-24 flex-1 border-0 bg-transparent px-1 shadow-none focus-visible:ring-0 dark:bg-transparent"
          onChange={(e) => {
            // a pasted/typed comma commits everything before it
            if (e.target.value.includes(",")) add(e.target.value)
            else setDraft(e.target.value)
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); add(draft) }
            else if (e.key === "Backspace" && !draft && value.length) remove(value[value.length - 1])
          }}
          onFocus={() => setFocused(true)}
          onBlur={() => { setFocused(false); add(draft) }}
        />
      </div>
      {focused && offered.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          {offered.map((t) => (
            <button key={t} type="button"
              className="rounded-full border px-2 py-0.5 text-2xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              // mousedown beats the input's blur, which would commit the draft first
              onMouseDown={(e) => { e.preventDefault(); add(t) }}>
              {t}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
