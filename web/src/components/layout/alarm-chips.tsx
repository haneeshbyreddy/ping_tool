import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { ArrowDown } from "lucide-react"
import { summaryApi } from "@/lib/api"
import { useAuth } from "@/hooks/use-auth"
import { cn } from "@/lib/utils"

export function AlarmChips() {
  const { scopeOrg } = useAuth()
  const { data } = useQuery({
    queryKey: ["summary", scopeOrg],
    queryFn: () => summaryApi.get(scopeOrg),
    enabled: !!scopeOrg,
    refetchInterval: 30_000,
  })

  if (!data) return null
  const lowBw = data.low_bandwidth.length

  return (
    <div className="flex items-center gap-2">
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
