@echo off
rem WISP edge launcher (Windows fleet path) — what the "WISP-Edge" Scheduled Task runs as SYSTEM.
rem
rem A Scheduled Task action can't carry env vars the way a systemd unit's Environment= lines do
rem (the same reason install.ps1 generates per-service .cmd launchers). So this loads the node's
rem identity/config from %ProgramData%\Wisp\edge.env, points the supervisor at the agent binary,
rem and starts the SUPERVISOR (which launches + self-updates the agent). Installed verbatim by
rem deploy\wisp-edge.iss next to the two binaries; an update swaps the binaries, never edge.env.
setlocal EnableExtensions
set "WISP_CFG=%ProgramData%\Wisp\edge.env"
if exist "%WISP_CFG%" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%WISP_CFG%") do set "%%A=%%B"
)
rem The frozen agent the supervisor manages lives next to this launcher (and this .exe too).
set "WISP_AGENT_BIN=%~dp0wisp-edge.exe"
set "PYTHONUNBUFFERED=1"
"%~dp0wisp-supervisor.exe"
