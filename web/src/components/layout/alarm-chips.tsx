import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { TriangleAlert, ArrowDown } from "lucide-react"
import { summaryApi } from "@/lib/api"
import { useAuth } from "@/hooks/use-auth"
import { cn } from "@/lib/utils"

export function AlarmChips() {
  const { scopeTenant } = useAuth()
  const { data } = useQuery({
    queryKey: ["summary", scopeTenant],
    queryFn: () => summaryApi.get(scopeTenant),
    enabled: !!scopeTenant,
    refetchInterval: 30_000,
  })

  if (!data) return null
  const lowBw = data.low_bandwidth.length

  return (
    <div className="flex items-center gap-2">
      {data.uplink_down && (
        <Link
          to="/outages"
          className={cn(
            "flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-semibold",
            "border-destructive/30 bg-destructive-soft text-destructive",
          )}
        >
          <span className="relative flex size-1.5">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-destructive opacity-75" />
            <span className="relative inline-flex size-1.5 rounded-full bg-destructive" />
          </span>
          <TriangleAlert className="size-3.5" />
          <span className="hidden sm:inline">Uplink down</span>
        </Link>
      )}
      {lowBw > 0 && (
        <Link
          to="/topology"
          className={cn(
            "flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-semibold",
            "border-warning/30 bg-warning-soft text-warning",
          )}
        >
          <ArrowDown className="size-3.5" />
          <span>{lowBw} low BW</span>
        </Link>
      )}
    </div>
  )
}
