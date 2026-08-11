import { useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { toast } from "sonner"
import { KeyRound } from "lucide-react"
import { usersApi, ApiError } from "@/lib/api"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

export function ChangePasswordCard() {
  const [current, setCurrent] = useState("")
  const [next, setNext] = useState("")
  const [confirm, setConfirm] = useState("")
  const [error, setError] = useState("")

  const change = useMutation({
    mutationFn: () => usersApi.changePassword({ current_password: current, new_password: next }),
    onSuccess: () => {
      toast.success("Password changed")
      setCurrent(""); setNext(""); setConfirm(""); setError("")
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Failed to change password"),
  })

  const mismatch = confirm.length > 0 && next !== confirm
  const canSubmit = current.length > 0 && next.length >= 8 && next === confirm && !change.isPending

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <KeyRound className="size-4 text-muted-foreground" /> Your password
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5">
        <div className="flex flex-col gap-1.5">
          <Label>Current password</Label>
          <Input type="password" autoComplete="current-password" className="max-w-sm"
            value={current} onChange={(e) => setCurrent(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>New password</Label>
          <Input type="password" autoComplete="new-password" placeholder="min 8 characters" className="max-w-sm"
            value={next} onChange={(e) => setNext(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Confirm new password</Label>
          <Input type="password" autoComplete="new-password" className="max-w-sm"
            value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        {mismatch && <p className="text-xs text-destructive">Passwords don't match.</p>}
        {error && <p className="text-xs text-destructive">{error}</p>}
        <Button size="sm" className="w-fit" disabled={!canSubmit} onClick={() => change.mutate()}>
          {change.isPending ? "Changing…" : "Change password"}
        </Button>
      </CardContent>
    </Card>
  )
}
