import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Plus, X } from "lucide-react"
import { regionsApi } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select, SelectContent, SelectItem, SelectSeparator, SelectTrigger, SelectValue,
} from "@/components/ui/select"

const NONE = "__none__"
const NEW = "__new__"

/* Region picker fed by /api/regions (union of declared + in-use names, so legacy
   free-text regions appear without backfill). "New region…" swaps to a text input;
   saving the parent form is what brings the name into circulation. */
export function RegionSelect({ org, value, onChange, className, inputClassName }: {
  org: string
  value: string
  onChange: (v: string) => void
  className?: string
  inputClassName?: string
}) {
  const [custom, setCustom] = useState(false)
  const { data } = useQuery({
    queryKey: ["regions", org],
    queryFn: () => regionsApi.list(org),
    enabled: !!org,
  })

  if (custom) {
    return (
      <div className="flex items-center gap-1">
        <Input autoFocus placeholder="new region" value={value}
          className={inputClassName ?? className}
          onChange={(e) => onChange(e.target.value)} />
        <Button variant="ghost" size="icon" className="size-7 shrink-0 text-muted-foreground"
          aria-label="Back to region list"
          onClick={() => { setCustom(false); onChange("") }}>
          <X className="size-3.5" />
        </Button>
      </div>
    )
  }

  const names = (data?.regions ?? []).map((r) => r.name)
  // a legacy value not (yet) in the list must still render as selected
  const options = value && !names.includes(value) ? [...names, value] : names

  return (
    <Select value={value || NONE} onValueChange={(v) => {
      if (v === NEW) { onChange(""); setCustom(true) }
      else onChange(v === NONE ? "" : v)
    }}>
      <SelectTrigger className={className}><SelectValue /></SelectTrigger>
      <SelectContent>
        <SelectItem value={NONE}>None</SelectItem>
        {options.map((n) => <SelectItem key={n} value={n}>{n}</SelectItem>)}
        <SelectSeparator />
        <SelectItem value={NEW}>
          <Plus className="size-3.5" /> New region…
        </SelectItem>
      </SelectContent>
    </Select>
  )
}
