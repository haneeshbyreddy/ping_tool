import { useEffect, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { MessageCircle, UserRound } from "lucide-react"
import { useAuth } from "@/hooks/use-auth"
import { usersApi, ApiError } from "@/lib/api"
import { ChangePasswordCard } from "@/components/change-password-card"
import { TwoFactorCard } from "@/components/two-factor-card"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/** The account holder's identity, read-only. Names WHO you're signed in as, so
 *  the personal actions below have an unambiguous owner. */
function ProfileCard() {
  const { user } = useAuth()
  if (!user) return null
  const role = user.is_superadmin ? "Superadmin" : user.role
  const org = user.is_superadmin ? "Platform (all orgs)" : user.org_name || user.org_id || "—"
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <UserRound className="size-4 text-muted-foreground" /> Profile
        </CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
        <span className="text-muted-foreground">Username</span>
        <span className="font-medium">{user.username}</span>
        <span className="text-muted-foreground">Role</span>
        <span className="font-medium capitalize">{role}</span>
        <span className="text-muted-foreground">Organization</span>
        <span className="font-medium">{org}</span>
      </CardContent>
    </Card>
  )
}

/** Your OWN WhatsApp page number (no id = self — self-service, so a worker can
 *  set it too). Re-reads from ["me"] on save so the value survives a refresh. */
function MyWhatsappCard() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const current = user?.whatsapp_number || ""
  const [value, setValue] = useState(current)
  useEffect(() => { setValue(current) }, [current])

  const save = useMutation({
    mutationFn: (num: string) => usersApi.setWhatsapp(num),
    onSuccess: (r) => {
      setValue(r.whatsapp_number || "")
      queryClient.invalidateQueries({ queryKey: ["me"] })
      toast.success(r.whatsapp_number ? "WhatsApp number saved" : "WhatsApp number cleared")
    },
    onError: (e) => {
      setValue(current)
      toast.error(e instanceof ApiError ? e.message : "Failed to save number")
    },
  })

  const dirty = value.trim() !== current
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <MessageCircle className="size-4 text-muted-foreground" /> WhatsApp alerts
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5">
        <p className="text-xs text-muted-foreground">
          The number your role's alerts are also sent to on WhatsApp, when the platform has the
          WhatsApp channel enabled. Leave blank to opt out — ntfy still reaches you either way.
        </p>
        <div className="flex flex-col gap-1.5">
          <Label>Your WhatsApp number</Label>
          <div className="flex items-center gap-2">
            <Input
              className="max-w-sm font-mono text-xs"
              placeholder="e.g. +919000000000"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && dirty && !save.isPending) save.mutate(value.trim()) }}
            />
            <Button size="sm" className="w-fit" disabled={!dirty || save.isPending}
              onClick={() => save.mutate(value.trim())}>
              {save.isPending ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

/** "You" — the personal third of the settings split (you / your org / the
 *  platform). Reachable by EVERY role, including a worker, from the account
 *  menu: it's the only password/2FA/WhatsApp surface a worker has, since
 *  workers never open the org Settings page. */
export function AccountPage() {
  const { user } = useAuth()
  // TOTP is owner/superadmin only (a worker's account can't enrol), so the card
  // is hidden rather than shown-then-403 for the field team.
  const canTwoFactor = !!user && (user.is_superadmin || user.role === "owner")

  return (
    <div className="wisp-page wisp-page--narrow flex flex-col gap-4 p-4 md:px-8 md:py-6">
      <h1 className="text-lg font-semibold tracking-tight">Your account</h1>
      <div className="flex flex-col gap-4">
        <ProfileCard />
        <ChangePasswordCard />
        <MyWhatsappCard />
        {canTwoFactor && <TwoFactorCard />}
      </div>
    </div>
  )
}
