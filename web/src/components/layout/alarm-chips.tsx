import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { ArrowDown, ArrowUp } from "lucide-react"
import { summaryApi } from "@/lib/api"
import { useAuth } from "@/hooks/use-auth"
import { Chip } from "@/components/status-badge"

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
  const highBw = data.high_bandwidth.length
  if (!lowBw && !highBw) return null

  return (
    <div className="hidden items-center gap-1.5 sm:flex">
      {lowBw > 0 && (
        <Link to="/topology" title={`${lowBw} port(s) under the bandwidth floor`}>
          <Chip tone="warning">
            <ArrowDown className="size-3" />
            {lowBw} low BW
          </Chip>
        </Link>
      )}
      {highBw > 0 && (
        <Link to="/topology" title={`${highBw} port(s) over the bandwidth ceiling`}>
          <Chip tone="warning">
            <ArrowUp className="size-3" />
            {highBw} high BW
          </Chip>
        </Link>
      )}
    </div>
  )
}
