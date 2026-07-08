import { useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

/* One confirmation pattern for every destructive action — replaces both the
   native confirm() and the no-confirm one-click deletes. */
export function ConfirmDialog({
  open, onOpenChange, title, description, confirmLabel = "Delete", onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel?: string
  onConfirm: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="destructive" size="sm"
            onClick={() => { onOpenChange(false); onConfirm() }}>
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* Local open/close state folded in — callers keep a one-liner call site:
   const del = useConfirm(); del.ask() to open, <ConfirmDialog {...del.props}/> */
export function useConfirm() {
  const [open, setOpen] = useState(false)
  return {
    ask: () => setOpen(true),
    props: { open, onOpenChange: setOpen },
  }
}
