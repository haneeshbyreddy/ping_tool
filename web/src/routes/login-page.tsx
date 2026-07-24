import { useRef, useState, type FormEvent, type KeyboardEvent } from "react"
import { useLocation, useNavigate } from "react-router-dom"
import { Eye, EyeOff, Loader2, Radio } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { ApiError } from "@/lib/api"
import { SESSION_EXPIRED_KEY } from "@/lib/session"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

export function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [showPassword, setShowPassword] = useState(false)
  const [capsLock, setCapsLock] = useState(false)
  const [error, setError] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [totpRequired, setTotpRequired] = useState(false)
  const [code, setCode] = useState("")
  const [useRecovery, setUseRecovery] = useState(false)
  const passwordRef = useRef<HTMLInputElement>(null)
  const codeRef = useRef<HTMLInputElement>(null)
  // Read-and-clear: the flag survives the redirect here, not a manual refresh.
  const [expired] = useState(() => {
    const was = sessionStorage.getItem(SESSION_EXPIRED_KEY) === "1"
    sessionStorage.removeItem(SESSION_EXPIRED_KEY)
    return was
  })

  const from = (location.state as { from?: string } | null)?.from

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError("")
    setShowPassword(false)
    setSubmitting(true)
    try {
      const second = totpRequired
        ? (useRecovery ? { recovery: code.trim() } : { totp: code.trim() })
        : undefined
      // No "remember this device": owners/superadmins are refused a long-lived
      // session server-side regardless, and we can't tell the role pre-login.
      await login(username, password, false, second)
      navigate(from || "/", { replace: true })
    } catch (err) {
      // A correct password on a 2FA account comes back as a 401 carrying
      // `totp_required` — that's the prompt for the second factor, not a failure.
      if (err instanceof ApiError && err.body?.totp_required) {
        const wasAsking = totpRequired
        setTotpRequired(true)
        setError(wasAsking ? "That code didn't match. Try again." : "")
        setCode("")
        setTimeout(() => codeRef.current?.focus(), 0)
        return
      }
      // server messages arrive lowercase ("invalid credentials") — display-cased here
      const msg = err instanceof ApiError ? err.message : "Sign-in failed. Try again."
      setError(msg.charAt(0).toUpperCase() + msg.slice(1))
      passwordRef.current?.focus()
      passwordRef.current?.select()
    } finally {
      setSubmitting(false)
    }
  }

  const onPasswordKey = (e: KeyboardEvent<HTMLInputElement>) => {
    setCapsLock(e.getModifierState("CapsLock"))
  }

  return (
    <div className="relative flex min-h-svh flex-col items-center justify-center overflow-hidden bg-background px-4">
      {/* one quiet glow so the card reads as lit, not floating in a void */}
      <div aria-hidden className="pointer-events-none absolute top-1/2 left-1/2 size-[36rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary/5 blur-3xl" />
      <Card className="relative w-full max-w-sm">
        <CardHeader>
          <div className="flex items-center gap-2">
            <Radio className="size-6 text-primary" />
            <h1 className="text-xl font-semibold tracking-tight">
              WISP Central
            </h1>
          </div>
          <p className="text-sm text-muted-foreground">Sign in to your network console.</p>
        </CardHeader>
        <CardContent>
          <form className="flex flex-col gap-4" onSubmit={onSubmit}>
            {expired && !error && (
              <p role="status" className="rounded-lg border bg-muted px-3 py-2 text-xs text-muted-foreground">
                Your session expired. Sign in again.
              </p>
            )}
            {!totpRequired ? (
              <>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="username">Username</Label>
                  <Input
                    id="username"
                    autoComplete="username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    disabled={submitting}
                    autoFocus
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="password">Password</Label>
                  <div className="relative">
                    <Input
                      id="password"
                      ref={passwordRef}
                      type={showPassword ? "text" : "password"}
                      autoComplete="current-password"
                      className="pr-9"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      onKeyDown={onPasswordKey}
                      onKeyUp={onPasswordKey}
                      disabled={submitting}
                    />
                    <button
                      type="button"
                      aria-label={showPassword ? "Hide password" : "Show password"}
                      className="absolute top-1/2 right-2.5 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground"
                      onClick={() => setShowPassword(!showPassword)}
                      disabled={submitting}
                    >
                      {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    </button>
                  </div>
                  {capsLock && (
                    <p className="text-xs text-warning">Caps Lock is on.</p>
                  )}
                </div>
              </>
            ) : (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="code">{useRecovery ? "Recovery code" : "Authenticator code"}</Label>
                <Input
                  id="code"
                  ref={codeRef}
                  inputMode={useRecovery ? "text" : "numeric"}
                  autoComplete="one-time-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder={useRecovery ? "xxxxx-xxxxx" : "123 456"}
                  disabled={submitting}
                  autoFocus
                />
                <p className="text-xs text-muted-foreground">
                  {useRecovery
                    ? "Enter one of the recovery codes you saved."
                    : "Enter the 6-digit code from your authenticator app."}
                </p>
                <button
                  type="button"
                  className="w-fit text-xs text-primary hover:underline"
                  onClick={() => { setUseRecovery(!useRecovery); setCode(""); setError("") }}
                  disabled={submitting}
                >
                  {useRecovery ? "Use your authenticator app instead" : "Use a recovery code instead"}
                </button>
              </div>
            )}
            {error && (
              <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive-soft px-3 py-2 text-xs text-destructive">
                {error}
              </p>
            )}
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting && <Loader2 className="size-4 animate-spin" />}
              {submitting ? "Signing in…" : totpRequired ? "Verify" : "Sign in"}
            </Button>
            {!totpRequired && (
              <p className="text-center text-xs text-muted-foreground">
                Forgot your password? Ask your org owner to reset it.
              </p>
            )}
          </form>
        </CardContent>
      </Card>
      <p className="relative mt-6 text-xs text-faint-foreground">
        WISP Central: uptime monitoring for ISPs
      </p>
    </div>
  )
}
