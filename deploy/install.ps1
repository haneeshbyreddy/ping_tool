<#
.SYNOPSIS
    WISP — one-shot installer for a fresh Windows box. The PowerShell sibling of
    deploy/install.sh.

.DESCRIPTION
    Takes you from "code is on the box" to "both runtimes auto-start on boot":
    venv, the two daemon deps, DB migrate, and two Scheduled Tasks (the Windows
    stand-in for the systemd units) that run as SYSTEM and restart on crash.
    Idempotent — safe to re-run to upgrade after a `git pull`.

    Why SYSTEM / why Scheduled Tasks (not NSSM): Windows has no unprivileged ICMP
    (no `ping_group_range`, no SOCK_DGRAM ICMP). icmplib therefore *forces raw
    sockets* on Windows, and raw sockets need Administrator. SYSTEM has that right,
    runs before any user logs in, and ships with every Windows — so we don't have
    to download NSSM onto a locked-down LAN box. The tasks restart on failure, so
    you get the same "runs forever" behaviour as the systemd units.

    It does NOT touch Windows Firewall (that needs your LAN subnet — it prints the
    netsh command) and does NOT install Python (download it from python.org first,
    ticking "Add to PATH").

.PARAMETER Port
    Dashboard TCP port. Default 8000 (matches the systemd unit).

.PARAMETER Uninstall
    Remove the two Scheduled Tasks (leaves the venv, DB and code in place).

.EXAMPLE
    # From an *elevated* PowerShell, in the repo:
    powershell -ExecutionPolicy Bypass -File deploy\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Port 9000

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Uninstall
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [int]$Port = 8000,
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

# Role alert channels — MUST match deploy\wisp-*.service and run.sh exactly, or
# the dashboard's "Send test" button pages a different topic than the daemon.
$TopicOwner    = 'hansa-owner-35f027e3a8'
$TopicOperator = 'hansa-ops-428fe896b9'
$TopicTech     = 'hansa-tech-87e2965d5e'
$PollIntervalS = '20'   # 20s x 3 strikes = DOWN within ~1 min (keeps flap suppression)

$TaskMonitor   = 'WISP-Monitor'
$TaskDashboard = 'WISP-Dashboard'

# --- uninstall path ----------------------------------------------------------
if ($Uninstall) {
    foreach ($t in @($TaskMonitor, $TaskDashboard)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Log "removed scheduled task: $t"
        }
    }
    Ok 'WISP scheduled tasks removed (venv, DB and code left in place).'
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

# --- 2. venv + the two daemon deps -------------------------------------------
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

# Fail loud HERE if the venv is broken — a monitor that can't import its prober/
# notifier must not reach "running" only to die on the first poll.
& $VenvPy -c 'import icmplib, httpx'
if ($LASTEXITCODE -ne 0) { Die 'venv deps failed to import (icmplib/httpx) — check the pip output above.' }

# --- 3. unprivileged ICMP: N/A on Windows ------------------------------------
# No `ping_group_range` equivalent. The Scheduled Tasks run as SYSTEM, which has
# the raw-socket privilege icmplib needs. Nothing to configure here.
Log 'ICMP: tasks run as SYSTEM (raw sockets) — no sysctl equivalent needed on Windows.'

# --- 4. database (idempotent migrations) -------------------------------------
Log 'creating / migrating database...'
$env:PYTHONPATH = (Join-Path $RepoRoot 'src')   # `-m wisp...` needs src on the path
& $VenvPy -m wisp.database.client | Out-Null
if ($LASTEXITCODE -ne 0) { Die 'database migration failed.' }

# --- 5. generate per-service launchers ---------------------------------------
# The systemd units carry the env on `Environment=` lines; the Scheduled Task
# action can't, so we bake it into a tiny .cmd launcher per service (the Windows
# analogue of the sed-rewritten unit). Regenerated every run; git-ignored.
$MonitorCmd   = Join-Path $RepoRoot 'deploy\wisp-monitor-run.cmd'
$DashboardCmd = Join-Path $RepoRoot 'deploy\wisp-dashboard-run.cmd'

$envBlock = @"
@echo off
rem GENERATED by deploy\install.ps1 — do not edit; re-run the installer instead.
set "PYTHONPATH=$RepoRoot\src"
set "PYTHONUNBUFFERED=1"
set "WISP_NTFY_TOPIC_OWNER=$TopicOwner"
set "WISP_NTFY_TOPIC_OPERATOR=$TopicOperator"
set "WISP_NTFY_TOPIC_TECH=$TopicTech"
"@

@"
$envBlock
set "WISP_POLL_INTERVAL_S=$PollIntervalS"
"$VenvPy" "$RepoRoot\apps\daemon\main.py"
"@ | Set-Content -Path $MonitorCmd -Encoding ASCII

@"
$envBlock
"$VenvPy" "$RepoRoot\apps\dashboard\main.py" --host 0.0.0.0 --port $Port
"@ | Set-Content -Path $DashboardCmd -Encoding ASCII

# --- 6. Scheduled Tasks (the systemd-unit stand-in) --------------------------
Log "registering scheduled tasks (run as SYSTEM, start at boot, restart on crash)..."
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # run indefinitely

function Register-WispTask {
    param([string]$Name, [string]$Cmd, [string]$Desc)
    # Stop any running instance FIRST. With MultipleInstances=IgnoreNew, Start is a
    # no-op while the old process is alive — so on a re-run (upgrade) the old code
    # would keep running even though -Force rewrote the definition on disk.
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    }
    $action = New-ScheduledTaskAction -Execute $Cmd -WorkingDirectory $RepoRoot
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description $Desc -Force | Out-Null
    Start-ScheduledTask -TaskName $Name
}

Register-WispTask -Name $TaskMonitor   -Cmd $MonitorCmd   -Desc 'WISP polling daemon'
Register-WispTask -Name $TaskDashboard -Cmd $DashboardCmd -Desc 'WISP dashboard web UI'

# --- done --------------------------------------------------------------------
Start-Sleep -Seconds 2
$lanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
          Select-Object -First 1 -ExpandProperty IPAddress)
if (-not $lanIp) { $lanIp = '<box-lan-ip>' }

Write-Host ''
Ok 'WISP is installed and running (two Scheduled Tasks).'
Write-Host ''
Get-ScheduledTask -TaskName $TaskMonitor, $TaskDashboard |
    Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "  Dashboard:  http://${lanIp}:${Port}   (set the PIN on first visit)"
Write-Host "  Logs:       Event Viewer, or run the .cmd in deploy\ by hand to see console output"
Write-Host ''
Write-Host '  Next:'
Write-Host '   1. Lock the dashboard to your LAN (replace the subnet):'
Write-Host "        netsh advfirewall firewall add rule name=""WISP Dashboard"" dir=in action=allow protocol=TCP localport=$Port remoteip=192.168.1.0/24"
Write-Host '      Keep it OFF the public internet — use a VPN for remote access.'
Write-Host '   2. Open the dashboard, set the PIN, add devices (Nodes) + team (Team).'
Write-Host '   3. Settings > Send test alert — confirm a push lands before you trust it.'
Write-Host ''
Write-Host '  Manage:   Get-ScheduledTask WISP-* | Get-ScheduledTaskInfo'
Write-Host '            Restart-ScheduledTask -TaskName WISP-Monitor'
Write-Host '            deploy\install.ps1 -Uninstall   # remove the tasks'
Write-Host ''
Write-Host "  Re-run this script any time after a 'git pull' to upgrade."
