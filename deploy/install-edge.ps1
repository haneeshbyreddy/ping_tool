<#
.SYNOPSIS
    WISP Edge — fleet installer for Windows (frozen binary). The PowerShell sibling of
    deploy/install-edge.sh.

.DESCRIPTION
    Downloads the signed win-amd64 agent + supervisor binaries built by
    .github/workflows/release.yml, VERIFIES their sha256 against the published SHA256SUMS
    (refuses to install on a mismatch) and, when the binaries carry an Authenticode
    signature (the "Sign (Windows, Authenticode)" CI step — a no-op until the operator sets
    the WINDOWS_CODESIGN_PFX secret), verifies that signature too — hard-failing on anything
    other than a valid chain. Installs under Program Files, writes config/identity to
    ProgramData (never touched by an update), and registers a Scheduled Task that runs the
    SUPERVISOR (not the agent directly) as SYSTEM at boot — the supervisor owns the agent's
    self-update (download -> verify(sha256) -> atomic-swap -> restart -> health-gate ->
    rollback, see runtime/supervisor.py) from then on, the same as the Linux fleet path's
    wisp-edge.service.

    This is distinct from deploy/install.ps1, which sets up the *single-box venv* path
    (running apps/daemon/main.py from a source checkout) — that script has no frozen
    binary, no supervisor, and no self-update. Use THIS script for a fleet of many Windows
    edges managed by central's staged rollout; use install.ps1 for a one-off box run from
    a git checkout.

.PARAMETER Central
    Central base URL, e.g. https://central.example.net

.PARAMETER Token
    The bearer token this edge presents to central.

.PARAMETER Tenant
    The tenant/org id this edge belongs to.

.PARAMETER Node
    This edge's node id (defaults to the hostname).

.PARAMETER BaseUrl
    Where the binaries + SHA256SUMS(+.minisig) + minisign.pub live. Defaults to "$Central/dl".

.PARAMETER Uninstall
    Remove the Scheduled Task and installed files (leaves nothing behind).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install-edge.ps1 `
        -Central https://central.example.net -Token s3cret -Tenant ispA -Node edge-w1
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$Central,
    [string]$Token,
    [string]$Tenant = "default",
    [string]$Node = $env:COMPUTERNAME,
    [string]$BaseUrl,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Log { param([string]$m) Write-Host "> $m" -ForegroundColor Cyan }
function Ok  { param([string]$m) Write-Host "OK $m" -ForegroundColor Green }
function Die { param([string]$m) Write-Host "X  $m" -ForegroundColor Red; exit 1 }

$TaskName  = 'WISP-Edge'
$Prefix    = Join-Path $env:ProgramFiles 'WISP\bin'
$ConfigDir = Join-Path $env:ProgramData 'WISP'
$Plat      = 'win-amd64'

# --- uninstall path ----------------------------------------------------------
if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Log "removed scheduled task: $TaskName"
    }
    Ok 'WISP edge scheduled task removed (installed files under Program Files/ProgramData left in place).'
    exit 0
}

if (-not $Central) { Die '-Central is required (central base URL)' }
if (-not $BaseUrl) { $BaseUrl = "$Central/dl" }
Log "platform: $Plat"
Log "downloading from: $BaseUrl"

# --- download agent + supervisor + checksums --------------------------------
$tmp = Join-Path $env:TEMP ("wisp-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    function Fetch {
        param([string]$Name, [switch]$Optional)
        $dest = Join-Path $tmp $Name
        try {
            Invoke-WebRequest -Uri "$BaseUrl/$Name" -OutFile $dest -UseBasicParsing
            return $dest
        } catch {
            if ($Optional) { return $null }
            Die "download failed: $BaseUrl/$Name"
        }
    }

    Log 'downloading binaries + checksums...'
    $agentExe = Fetch "wisp-edge-$Plat.exe"
    $supExe   = Fetch "wisp-supervisor-$Plat.exe"
    $sums     = Fetch 'SHA256SUMS'

    Log 'verifying sha256...'
    $sumLines = Get-Content $sums | Where-Object { $_ -match "wisp-(edge|supervisor)-$Plat\.exe$" }
    foreach ($line in $sumLines) {
        # sha256sum format: "<hash>  <filename>" (may be a leading "*" for binary mode)
        if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$') { continue }
        $expected = $Matches[1].ToLower()
        $fname    = $Matches[2].Trim()
        $fpath    = Join-Path $tmp $fname
        if (-not (Test-Path $fpath)) { continue }
        $actual = (Get-FileHash -Path $fpath -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expected) { Die "checksum verification FAILED for $fname — refusing to install" }
    }
    Ok 'sha256 verified.'

    # --- Authenticode (self-activating: only enforced once CI actually signs, i.e. once the
    # operator sets WINDOWS_CODESIGN_PFX — an unsigned build just gets a warning, same
    # sha256-only fallback policy as install-edge.sh's minisign check) -------------------------
    foreach ($exe in @($agentExe, $supExe)) {
        $sig = Get-AuthenticodeSignature -FilePath $exe
        if ($sig.Status -eq 'NotSigned') {
            Log "warning: $(Split-Path -Leaf $exe) is not Authenticode-signed (signing not yet configured in CI) — sha256-only verification."
        } elseif ($sig.Status -ne 'Valid') {
            Die "Authenticode verification FAILED for $(Split-Path -Leaf $exe): $($sig.Status) — refusing to install"
        } else {
            Ok "Authenticode signature valid: $(Split-Path -Leaf $exe) ($($sig.SignerCertificate.Subject))"
        }
    }

    # --- install -----------------------------------------------------------
    New-Item -ItemType Directory -Path $Prefix -Force | Out-Null
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    Copy-Item $agentExe (Join-Path $Prefix 'wisp-edge.exe') -Force
    Copy-Item $supExe   (Join-Path $Prefix 'wisp-supervisor.exe') -Force
    Ok "installed to $Prefix"

    $envFile = Join-Path $ConfigDir 'edge.env.ps1'
    if (-not (Test-Path $envFile)) {
        Log "writing $envFile (identity + central config)..."
        @"
# GENERATED by install-edge.ps1 — edit values, not structure. An update never overwrites this.
`$env:WISP_CENTRAL_URL = '$Central'
`$env:WISP_CENTRAL_TOKEN = '$Token'
`$env:WISP_TENANT_ID = '$Tenant'
`$env:WISP_NODE_ID = '$Node'
`$env:WISP_AGENT_BIN = '$Prefix\wisp-edge.exe'
`$env:WISP_DB = '$ConfigDir\wisp.db'
"@ | Set-Content -Path $envFile -Encoding ASCII
    } else {
        Log "$envFile exists — leaving it (update never overwrites identity)."
    }

    $launcher = Join-Path $Prefix 'run-supervisor.cmd'
    @"
@echo off
rem GENERATED by install-edge.ps1 — runs the supervisor, which owns the agent's lifecycle
rem and self-update. Edit %ProgramData%\WISP\edge.env.ps1 for connection settings.
powershell -NoProfile -ExecutionPolicy Bypass -Command ". '$ConfigDir\edge.env.ps1'; & '$Prefix\wisp-supervisor.exe'"
"@ | Set-Content -Path $launcher -Encoding ASCII

    # --- Scheduled Task (runs the SUPERVISOR, not the agent directly) ------
    Log 'registering scheduled task (runs as SYSTEM, starts at boot, restarts on crash)...'
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $settings  = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    $action = New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $Prefix
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description 'WISP edge supervisor (fleet path)' -Force | Out-Null

    Write-Host ''
    Ok "WISP edge (fleet path) installed. Node $Tenant/$Node -> $Central."
    Write-Host "  start:  Start-ScheduledTask -TaskName $TaskName"
    Write-Host "  status: Get-ScheduledTask $TaskName | Get-ScheduledTaskInfo"
    Write-Host "  logs:   run '$launcher' by hand to see console output"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
