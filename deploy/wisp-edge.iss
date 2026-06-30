; deploy/wisp-edge.iss — Windows FLEET installer for the WISP edge (Phase 10 Part D).
;
; This is the Windows analogue of deploy/install-edge.sh (the Linux curl|sh fleet path): it drops
; the frozen agent + supervisor, writes identity/config OUTSIDE the binaries (so an update swaps
; only the .exe files), and registers the supervisor as a SYSTEM Scheduled Task that auto-starts
; at boot and restarts on crash. It is distinct from deploy/install.ps1, which is the single-box
; *venv* path; this one ships the frozen binaries CI builds.
;
; Build it in CI on windows-latest, AFTER PyInstaller has produced the two .exe files and they +
; the two helper scripts sit next to this .iss (the release workflow does exactly that):
;     iscc /DAppVersion=%VER% deploy\wisp-edge.iss        ; -> dist\wisp-edge-setup-%VER%.exe
;
; Silent fleet install (what you put behind the download link), passing enrollment in:
;     wisp-edge-setup-x.y.z.exe /VERYSILENT /SUPPRESSMSGBOXES ^
;        /central=https://central.example.net /token=<TOKEN> /tenant=ispA /node=edge-a1
;
; Code signing (Authenticode) is wired in CI via a SignTool definition + the cert in Actions
; secrets (see deploy/ci-cd.md); an unsigned installer trips SmartScreen on a fleet rollout.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
; A stable AppId so upgrades replace in place (generated once; never change it).
AppId={{B6F3B2B1-6C1E-4E2A-9E2C-7D5A1F0E9A21}
AppName=WISP Edge
AppVersion={#AppVersion}
AppPublisher=WISP
DefaultDirName={autopf}\Wisp
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=..\dist
OutputBaseFilename=wisp-edge-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; SignTool=signtool $f   ; <- defined on the iscc command line in CI; see deploy/ci-cd.md

[Files]
; The frozen binaries PyInstaller produced (staged next to this .iss before compiling).
Source: "wisp-edge.exe";          DestDir: "{app}"; Flags: ignoreversion
Source: "wisp-supervisor.exe";    DestDir: "{app}"; Flags: ignoreversion
Source: "wisp-edge-launcher.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "register-wisp-edge-task.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; Config + identity + DB live OUTSIDE {app} so a binary update never touches them.
Name: "{commonappdata}\Wisp"; Permissions: admins-full

[Run]
; Register + start the SYSTEM boot task via the PowerShell helper (clean task settings:
; restart-on-failure, run indefinitely — see register-wisp-edge-task.ps1).
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\register-wisp-edge-task.ps1"" -Launcher ""{app}\wisp-edge-launcher.cmd"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "Registering WISP-Edge service task..."

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\register-wisp-edge-task.ps1"" -Unregister"; \
  Flags: runhidden waituntilterminated; RunOnceId: "RemoveWispEdgeTask"

[Code]
{ Write %ProgramData%\Wisp\edge.env from the install-time /central= /token= /tenant= /node=
  parameters — but NEVER overwrite an existing file, so an upgrade preserves the node's
  identity/config (the same rule as install-edge.sh and the systemd EnvironmentFile). }
procedure WriteEdgeEnvIfAbsent();
var
  CfgDir, CfgPath, Node: string;
  Lines: TArrayOfString;
begin
  CfgDir := ExpandConstant('{commonappdata}\Wisp');
  CfgPath := CfgDir + '\edge.env';
  if FileExists(CfgPath) then
    exit;  { upgrade path: leave identity/config alone }

  Node := ExpandConstant('{param:node|}');
  if Node = '' then
    Node := GetComputerNameString();

  ForceDirectories(CfgDir);
  SetArrayLength(Lines, 5);
  Lines[0] := 'WISP_CENTRAL_URL=' + ExpandConstant('{param:central|}');
  Lines[1] := 'WISP_CENTRAL_TOKEN=' + ExpandConstant('{param:token|}');
  Lines[2] := 'WISP_TENANT_ID=' + ExpandConstant('{param:tenant|default}');
  Lines[3] := 'WISP_NODE_ID=' + Node;
  Lines[4] := 'WISP_DB=' + CfgDir + '\wisp.db';
  SaveStringsToFile(CfgPath, Lines, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { Write config before [Run] registers the task, so the first launch already has identity. }
  if CurStep = ssInstall then
    WriteEdgeEnvIfAbsent();
end;
