import { useEffect, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import QRCode from "qrcode"
import { Copy, Loader2, ShieldAlert, ShieldCheck } from "lucide-react"
import { ApiError, usersApi } from "@/lib/api"
import { useAuth } from "@/hooks/use-auth"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"

function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        Save these somewhere safe. Each works once, they are the only way back in
        if you lose your phone, and they won't be shown again.
      </p>
      <div className="grid grid-cols-2 gap-x-6 gap-y-1 rounded-lg border bg-muted p-3 font-mono text-sm">
        {codes.map((c) => <span key={c}>{c}</span>)}
      </div>
      <DialogFooter>
        <Button size="sm" variant="outline" onClick={() => {
          navigator.clipboard?.writeText(codes.join("\n"))
          toast.success("Recovery codes copied")
        }}>
          <Copy className="size-4" /> Copy
        </Button>
        <Button size="sm" onClick={onDone}>I've saved them</Button>
      </DialogFooter>
    </div>
  )
}

function EnableDialog() {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [qr, setQr] = useState("")
  const [password, setPassword] = useState("")
  const [code, setCode] = useState("")
  const [codes, setCodes] = useState<string[] | null>(null)
  const [error, setError] = useState("")

  const start = useMutation({
    mutationFn: usersApi.totpStart,
    onError: (e) => setError(e instanceof ApiError ? e.message : "Couldn't start setup"),
  })
  const confirm = useMutation({
    mutationFn: () => usersApi.totpConfirm({ password, code: code.trim() }),
    onSuccess: (r) => { setCodes(r.recovery_codes); setError("") },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Couldn't enable"),
  })

  useEffect(() => {
    const uri = start.data?.otpauth_uri
    if (!uri) { setQr(""); return }
    QRCode.toDataURL(uri, { width: 200, margin: 1 }).then(setQr).catch(() => setQr(""))
  }, [start.data?.otpauth_uri])

  const reset = () => {
    setQr(""); setPassword(""); setCode(""); setCodes(null); setError("")
    start.reset(); confirm.reset()
  }
  const finish = () => { setOpen(false); reset(); qc.invalidateQueries({ queryKey: ["me"] }) }

  const secret = start.data?.secret

  return (
    <Dialog open={open} onOpenChange={(o) => {
      setOpen(o)
      if (o) start.mutate()
      else reset()
    }}>
      <DialogTrigger asChild><Button size="sm">Enable</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Set up two-factor authentication</DialogTitle></DialogHeader>
        {codes ? (
          <RecoveryCodes codes={codes} onDone={finish} />
        ) : (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-muted-foreground">
              Scan this with Google Authenticator (or any authenticator app), then
              enter the 6-digit code it shows to confirm.
            </p>
            <div className="flex flex-col items-center gap-2">
              {qr
                ? <img src={qr} alt="Authenticator setup QR code" width={200} height={200}
                    className="rounded-lg bg-white p-2" />
                : <div className="flex size-[216px] items-center justify-center rounded-lg border">
                    <Loader2 className="size-5 animate-spin text-muted-foreground" />
                  </div>}
              {secret && (
                <button type="button" title="Copy setup key for manual entry"
                  className="font-mono text-xs tracking-wider text-muted-foreground hover:text-foreground"
                  onClick={() => { navigator.clipboard?.writeText(secret); toast.success("Setup key copied") }}>
                  {secret}
                </button>
              )}
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>Current password</Label>
              <Input type="password" autoComplete="current-password" value={password}
                onChange={(e) => setPassword(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>6-digit code</Label>
              <Input inputMode="numeric" autoComplete="one-time-code" placeholder="123 456"
                value={code} onChange={(e) => setCode(e.target.value)} />
            </div>
            {error && <p className="text-xs text-destructive">{error}</p>}
            <DialogFooter>
              <Button disabled={!password || code.trim().length < 6 || confirm.isPending}
                onClick={() => confirm.mutate()}>
                {confirm.isPending ? "Verifying…" : "Turn on"}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function DisableDialog() {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const disable = useMutation({
    mutationFn: () => usersApi.totpDisable(password),
    onSuccess: () => {
      toast.success("Two-factor turned off")
      setOpen(false); setPassword("")
      qc.invalidateQueries({ queryKey: ["me"] })
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Couldn't disable"),
  })
  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) { setPassword(""); setError("") } }}>
      <DialogTrigger asChild><Button size="sm" variant="outline">Turn off</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Turn off two-factor authentication</DialogTitle></DialogHeader>
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted-foreground">
            Confirm your password to remove the second factor from your account.
          </p>
          <div className="flex flex-col gap-1.5">
            <Label>Current password</Label>
            <Input type="password" autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)} />
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
          <DialogFooter>
            <Button variant="destructive" disabled={!password || disable.isPending}
              onClick={() => disable.mutate()}>
              {disable.isPending ? "Turning off…" : "Turn off"}
            </Button>
          </DialogFooter>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function RegenerateDialog() {
  const [open, setOpen] = useState(false)
  const [password, setPassword] = useState("")
  const [code, setCode] = useState("")
  const [codes, setCodes] = useState<string[] | null>(null)
  const [error, setError] = useState("")
  const regen = useMutation({
    mutationFn: () => usersApi.totpRegenerate({ password, code: code.trim() }),
    onSuccess: (r) => { setCodes(r.recovery_codes); setError("") },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Couldn't regenerate"),
  })
  const reset = () => { setPassword(""); setCode(""); setCodes(null); setError(""); regen.reset() }
  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) reset() }}>
      <DialogTrigger asChild><Button size="sm" variant="outline">Recovery codes</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>New recovery codes</DialogTitle></DialogHeader>
        {codes ? (
          <RecoveryCodes codes={codes} onDone={() => { setOpen(false); reset() }} />
        ) : (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-muted-foreground">
              This replaces your old codes. Any you haven't used stop working.
              Confirm your password and a current authenticator code.
            </p>
            <div className="flex flex-col gap-1.5">
              <Label>Current password</Label>
              <Input type="password" autoComplete="current-password" value={password}
                onChange={(e) => setPassword(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>6-digit code</Label>
              <Input inputMode="numeric" autoComplete="one-time-code" placeholder="123 456"
                value={code} onChange={(e) => setCode(e.target.value)} />
            </div>
            {error && <p className="text-xs text-destructive">{error}</p>}
            <DialogFooter>
              <Button disabled={!password || code.trim().length < 6 || regen.isPending}
                onClick={() => regen.mutate()}>
                {regen.isPending ? "Generating…" : "Generate new codes"}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

export function TwoFactorCard() {
  const { user } = useAuth()
  const enabled = !!user?.totp_enabled
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          {enabled
            ? <ShieldCheck className="size-4 text-success" />
            : <ShieldAlert className="size-4 text-muted-foreground" />}
          Two-factor authentication
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground">
          {enabled
            ? "On. A code from your authenticator app is required each time you sign in."
            : "Off. Add a second step at sign-in with an authenticator app. Strongly recommended for the account that configures your network."}
        </p>
        {enabled
          ? <div className="flex gap-2"><RegenerateDialog /><DisableDialog /></div>
          : <EnableDialog />}
      </CardContent>
    </Card>
  )
}
