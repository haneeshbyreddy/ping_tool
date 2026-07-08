import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { CheckCheck } from "lucide-react"
import { outagesApi, ApiError } from "@/lib/api"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"

export function ClearPostmortems({ org, count }: { org: string | null; count: number }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)

  const clear = useMutation({
    mutationFn: () => outagesApi.clearPostmortems(org),
    onSuccess: ({ cleared }) => {
      queryClient.invalidateQueries({ queryKey: ["outages"] })
      queryClient.invalidateQueries({ queryKey: ["logs"] })
      setOpen(false)
      toast.success(`Cleared ${cleared} post-mortem${cleared === 1 ? "" : "s"}`)
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : "failed to clear"),
  })

  if (count === 0) return null

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5">
          <CheckCheck className="size-3.5" />
          Clear post-mortems ({count})
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Clear the post-mortem queue?</DialogTitle>
          <DialogDescription>
            This closes {count} resolved outage{count === 1 ? "" : "s"} still awaiting a
            post-mortem, stamping each with a generic "no post-mortem recorded" cause. Every
            one lands a post-mortem entry in the log. This can't be undone in bulk.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
          <Button onClick={() => clear.mutate()} disabled={clear.isPending}>
            {clear.isPending ? "Clearing…" : `Clear ${count}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
