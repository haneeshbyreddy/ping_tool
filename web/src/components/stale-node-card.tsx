import { Link } from "react-router-dom"
import { WifiOff } from "lucide-react"
import type { NodeToken } from "@/lib/types"
import { durationSince } from "@/lib/format"
import { useNow } from "@/hooks/use-now"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"

export function StaleNodeCard({ node }: { node: NodeToken }) {
  useNow()
  return (
    <Card className="border-l-2 border-l-destructive py-4">
      <CardContent className="flex flex-col gap-3 px-5">
        <div className="flex items-start justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <WifiOff className="size-4 shrink-0 text-destructive" />
            <div className="min-w-0">
              <p className="truncate font-mono text-sm font-semibold">{node.node_id}</p>
              <p className="text-xs text-muted-foreground">
                Probe offline — central can't see any device behind it
              </p>
            </div>
          </div>
          <span className="shrink-0 rounded-full border border-destructive/30 bg-destructive-soft px-2 py-0.5 text-[0.75rem] font-semibold whitespace-nowrap text-destructive">
            Offline
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="font-mono text-xs font-semibold text-destructive">
            dark for {durationSince(node.last_seen)}
          </span>
          <Button asChild size="sm" variant="outline">
            {/* probeId state pre-filters the device tree to what this probe covers */}
            <Link to="/topology" state={{ probeId: node.node_id }}>Check probe</Link>
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
