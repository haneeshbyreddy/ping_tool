<#
.SYNOPSIS
    WISP — one-shot installer for a fresh Windows edge box. The PowerShell sibling of
    deploy/install.sh.

.DESCRIPTION
    Takes you from "code is on the box" to "the edge probe auto-starts on boot": venv,
    the probe's deps, and a Scheduled Task (the Windows stand-in for the systemd unit)
    that runs as SYSTEM and restarts on crash. There is no local dashboard/DB on the
    edge anymore (see plan.md) — this box only probes and reports to a central
    server; you'll edit the generated launcher with your central URL/token/tenant
    before starting it. Idempotent — safe to re-run to upgrade after a `git pull`.

    Why SYSTEM / why a Scheduled Task (not NSSM): Windows has no unprivileged ICMP
    (no `ping_group_range`, no SOCK_DGRAM ICMP). icmplib therefore *forces raw
    sockets* on Windows, and raw sockets need Administrator. SYSTEM has that right,
    runs before any user logs in, and ships with every Windows — so we don't have
    to download NSSM onto a locked-down LAN box. The task restarts on failure, so
    you get the same "runs forever" behaviour as the systemd unit.

    It does NOT install Python (download it from python.org first, ticking "Add to
    PATH") and does NOT set your central connection details — edit the generated
    .cmd launcher (deploy\wisp-monitor-run.cmd) after installing.

.PARAMETER Uninstall
    Remove the Scheduled Task (leaves the venv and code in place).

.EXAMPLE
    # From an *elevated* PowerShell, in the repo:
    powershell -ExecutionPolicy Bypass -File deploy\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Uninstall
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [switch]$Uninstall
)

# Fail fast, like `set -euo pipefail`.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Log  { param([string]$m) Write-Host "> $m" -ForegroundColor Cyan }
function Ok   { param([string]$m) Write-Host "OK $m" -ForegroundColor Green }
function Die  { param([string]$m) Write-Host "X  $m" -ForegroundColor Red; exit 1 }

# --- resolve the repo root (this script lives in <repo>\deploy) --------------
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Venv     = Join-Path $RepoRoot '.venv'
$VenvPy   = Join-Path $Venv 'Scripts\python.exe'

$TaskMonitor = 'WISP-Monitor'

# --- uninstall path ----------------------------------------------------------
if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskMonitor -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskMonitor -Confirm:$false
        Log "removed scheduled task: $TaskMonitor"
    }
    Ok 'WISP scheduled task removed (venv and code left in place).'
    exit 0
}

Log "installing into: $RepoRoot"

# --- 1. Python prerequisite --------------------------------------------------
# Can't `apt-get` on Windows; Python must already be present. Prefer the `py`
# launcher (handles versions cleanly), fall back to `python` on PATH.
$BasePythonExe  = $null
$BasePythonArgs = @()
foreach ($cand in @('py', 'python')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $tryArgs = if ($cand -eq 'py') { @('-3') } else { @() }
    try {
        & $cmd.Source @tryArgs --version *> $null
        if ($LASTEXITCODE -eq 0) { $BasePythonExe = $cmd.Source; $BasePythonArgs = $tryArgs; break }
    } catch {}
}
if (-not $BasePythonExe) {
    Die "Python 3 not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), reopen this elevated PowerShell, and re-run."
}
Log ("using base Python: $BasePythonExe " + ($BasePythonArgs -join ' '))

# --- 2. venv + the probe's deps -----------------------------------------------
if (-not (Test-Path $Venv)) {
    Log "creating venv at $Venv ..."
    & $BasePythonExe @BasePythonArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Die 'venv creation failed.' }
}
if (-not (Test-Path $VenvPy)) { Die "venv python missing at $VenvPy (corrupt venv? delete .venv and re-run)." }

Log 'installing Python deps (icmplib, httpx)...'
& $VenvPy -m pip install -q --upgrade pip
if ($LASTEXITCODE -ne 0) { Die 'pip self-upgrade failed.' }
& $VenvPy -m pip install -q -r (Join-Path $RepoRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Die 'dependency install failed (offline box? proxy needed?).' }

# Fail loud HERE if the venv is broken — a probe that can't import icmplib/httpx
# must not reach "running" only to die on the first poll.
& $VenvPy -c 'import icmplib, httpx'
if ($LASTEXITCODE -ne 0) { Die 'venv deps failed to import (icmplib/httpx) — check the pip output above.' }

# --- 3. unprivileged ICMP: N/A on Windows ------------------------------------
# No `ping_group_range` equivalent. The Scheduled Task runs as SYSTEM, which has
# the raw-socket privilege icmplib needs. Nothing to configure here.
Log 'ICMP: task runs as SYSTEM (raw sockets) — no sysctl equivalent needed on Windows.'

# --- 4. generate the launcher -------------------------------------------------
# The systemd unit carries the env on `Environment=` lines; the Scheduled Task
# action can't, so we bake it into a tiny .cmd launcher (the Windows analogue of
# the sed-rewritten unit). Regenerated every run; git-ignored. EDIT THE CENTRAL_*
# VALUES BELOW before starting the task.
$MonitorCmd = Join-Path $RepoRoot 'deploy\wisp-monitor-run.cmd'

@"
@echo off
rem GENERATED by deploy\install.ps1 — do not edit structure; re-run the installer
rem to regenerate. DO edit the WISP_CENTRAL_* values to match your central server.
set "PYTHONPATH=$RepoRoot\src"
set "PYTHONUNBUFFERED=1"
set "WISP_CENTRAL_BRAIN=1"
set "WISP_CENTRAL_URL=https://central.example.net"
set "WISP_CENTRAL_TOKEN=changeme"
set "WISP_TENANT_ID=changeme"
set "WISP_POLL_INTERVAL_S=20"
"$VenvPy" "$RepoRoot\apps\daemon\main.py"
"@ | Set-Content -Path $MonitorCmd -Encoding ASCII

Log "wrote $MonitorCmd — edit its WISP_CENTRAL_* values before starting the task."

# --- 5. Scheduled Task (the systemd-unit stand-in) --------------------------
Log "registering scheduled task (runs as SYSTEM, starts at boot, restarts on crash)..."
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # run indefinitely

# Stop any running instance FIRST. With MultipleInstances=IgnoreNew, Start is a
# no-op while the old process is alive — so on a re-run (upgrade) the old code
# would keep running even though -Force rewrote the definition on disk.
if (Get-ScheduledTask -TaskName $TaskMonitor -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskMonitor -ErrorAction SilentlyContinue
}
$action = New-ScheduledTaskAction -Execute $MonitorCmd -WorkingDirectory $RepoRoot
Register-ScheduledTask -TaskName $TaskMonitor -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description 'WISP edge probe' -Force | Out-Null

# --- done --------------------------------------------------------------------
Write-Host ''
Ok 'WISP edge probe is installed (Scheduled Task registered, not yet started).'
Write-Host ''
Write-Host "  1. Edit $MonitorCmd with your central server's URL/token/tenant."
Write-Host "  2. Start it:   Start-ScheduledTask -TaskName $TaskMonitor"
Write-Host '  3. Check it:   Get-ScheduledTask WISP-Monitor | Get-ScheduledTaskInfo'
Write-Host '                 Event Viewer, or run the .cmd by hand to see console output'
Write-Host ''
Write-Host '  All device topology, team, alert routing, and outage history now live on'
Write-Host '  your central server''s dashboard — this box has no UI of its own.'
Write-Host ''
Write-Host '  Manage:   Restart-ScheduledTask -TaskName WISP-Monitor'
Write-Host '            deploy\install.ps1 -Uninstall   # remove the task'
Write-Host ''
Write-Host "  Re-run this script any time after a 'git pull' to upgrade."
