import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { inventoryApi } from "@/lib/api"
import { ponLabels } from "@/map/plant"

/** The PON labels one OLT's roster actually reports.
 *
 *  ONE definition, because three surfaces now ask the question — the map's
 *  create sheet, the Network page's device form and the field survey — and a
 *  splitter bound to `EPON0/4` from one screen and `0/4` from another is a
 *  splitter whose customer picker is empty on both. The whole reason the PON
 *  stopped being a text field is that its value has to match what the walk
 *  stores, exactly, and only the walk knows how it spells it.
 *
 *  Keyed on the OLT, so CHANGING THE FEEDER REFRESHES THE LIST for free: a PON
 *  label belongs to one box's roster, and carrying a selection across would
 *  attach a splitter to a port the new OLT has never reported. Callers reset
 *  their own selection on the same change.
 *
 *  Shares the Optical tab's cache key, so opening this over an OLT somebody was
 *  just looking at costs nothing. It is a heavier reply than the question needs
 *  (the full roster, to read one column off it) and that is the deliberate
 *  trade: no new endpoint, no central restart, and it is already cached on every
 *  surface that matters. */
export function usePonOptions(oltId: number | null | undefined, enabled = true) {
  const q = useQuery({
    queryKey: ["optics", oltId],
    queryFn: () => inventoryApi.optics(oltId!),
    enabled: !!oltId && enabled,
    staleTime: 60_000,
  })
  const pons = useMemo(
    () => ponLabels((q.data?.onus ?? []).map((o) => o.pon_port)),
    [q.data])
  return { pons, loading: q.isLoading && !!oltId && enabled }
}
