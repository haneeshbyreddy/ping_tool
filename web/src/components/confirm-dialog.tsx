import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

export function ConfirmDialog({
  open, onOpenChange, title, description, confirmLabel = "Delete", requireText, onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel?: string
  requireText?: string
  onConfirm: () => void
}) {
  const [typed, setTyped] = useState("")
  useEffect(() => { if (open) setTyped("") }, [open])
  const armed = !requireText || typed.trim() === requireText

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {requireText && (
          <div className="flex flex-col gap-1.5">
            <label className="text-xs text-muted-foreground">
              Type <span className="font-mono font-semibold text-foreground">{requireText}</span> to confirm
            </label>
            <Input autoFocus className="font-mono text-xs" value={typed}
              placeholder={requireText}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && armed) { onOpenChange(false); onConfirm() }
              }} />
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="destructive" size="sm" disabled={!armed}
            onClick={() => { onOpenChange(false); onConfirm() }}>
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function useConfirm() {
  const [open, setOpen] = useState(false)
  return {
    ask: () => setOpen(true),
    props: { open, onOpenChange: setOpen },
  }
}
