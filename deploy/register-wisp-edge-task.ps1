<#
.SYNOPSIS
    Register (or remove) the "WISP-Edge" Scheduled Task — the Windows stand-in for the
    wisp-edge.service systemd unit, used by the frozen-binary FLEET path (deploy\wisp-edge.iss).

.DESCRIPTION
    Runs the supervisor launcher as SYSTEM, at boot, restarting on crash — the same proven
    pattern as deploy\install.ps1's tasks. SYSTEM because Windows has no unprivileged ICMP
    (icmplib forces raw sockets, which need Administrator); SYSTEM has that right and starts
    before any user logs in. The Inno installer calls this so the task settings (restart-on-
    failure, run-indefinitely) are defined in PowerShell, not brittle schtasks /TR quoting.

.PARAMETER Launcher
    Full path to wisp-edge-launcher.cmd (defaults to this script's own folder).

.PARAMETER Unregister
    Remove the task instead of creating it (used by the uninstaller).
#>
[CmdletBinding()]
param(
    [string]$Launcher = (Join-Path $PSScriptRoot 'wisp-edge-launcher.cmd'),
    [switch]$Unregister
)
$ErrorActionPreference = 'Stop'
$TaskName = 'WISP-Edge'

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask     -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed scheduled task: $TaskName"
    }
    exit 0
}

if (-not (Test-Path $Launcher)) { throw "launcher not found: $Launcher" }

$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # run indefinitely (it's a forever loop)

# Re-running (an upgrade) must replace cleanly: stop the old instance first, since
# MultipleInstances=IgnoreNew makes Start a no-op while the old process is alive.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
$action = New-ScheduledTaskAction -Execute $Launcher -WorkingDirectory (Split-Path -Parent $Launcher)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'WISP Edge supervisor (manages + self-updates the polling agent)' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "registered + started scheduled task: $TaskName"
