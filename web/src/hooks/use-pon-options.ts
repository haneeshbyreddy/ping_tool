import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { inventoryApi } from "@/lib/api"
import { ponLabels } from "@/map/plant"

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
