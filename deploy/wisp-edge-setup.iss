#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BinDir
  #define BinDir "out"
#endif
#ifndef OutDir
  #define OutDir "out"
#endif

[Setup]
AppId={{7C1E9B3A-4B9E-4C64-9C36-2D8A41E6F0B2}
AppName=WISP Edge
AppVersion={#AppVersion}
AppPublisher=WISP
DefaultDirName={autopf}\WISP
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=wisp-edge-setup-win-amd64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\bin\wisp-edge.exe

[Files]
Source: "{#BinDir}\wisp-edge-win-amd64.exe"; DestDir: "{app}\bin"; DestName: "wisp-edge.exe"; Flags: ignoreversion
Source: "{#BinDir}\wisp-supervisor-win-amd64.exe"; DestDir: "{app}\bin"; DestName: "wisp-supervisor.exe"; Flags: ignoreversion
Source: "{#BinDir}\wisp-tray-win-amd64.exe"; DestDir: "{app}\bin"; DestName: "wisp-tray.exe"; Flags: ignoreversion
Source: "windows-task.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "WISP Tray"; ValueData: """{app}\bin\wisp-tray.exe"""; \
  Flags: uninsdeletevalue

[Run]
Filename: "{app}\bin\wisp-tray.exe"; Flags: nowait runasoriginaluser skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/F /IM wisp-tray.exe"; \
  Flags: runhidden waituntilterminated; RunOnceId: "KillWispTray"
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\windows-task.ps1"" -Unregister"; \
  Flags: runhidden waituntilterminated; RunOnceId: "UnregisterWispTask"

[Code]
var
  ConfigPage: TInputQueryWizardPage;

procedure KillImage(const Image: string);
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ' + Image, '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  // A running fleet delete-locks its own images (supervisor/agent under the
  // SYSTEM task, tray per-user), which made every reinstall fail with
  // "old files exist" until the user manually uninstalled and deleted the
  // folder. Stop them all BEFORE file copy — re-running this installer must
  // always upgrade in place. CurStepChanged re-registers and restarts the
  // task afterwards, and [Run] relaunches the tray.
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN WISP-Edge', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  KillImage('wisp-tray.exe');
  KillImage('wisp-supervisor.exe');
  KillImage('wisp-edge.exe');
  Sleep(1500);
end;

function ParamOr(const Name, Default: string): string;
begin
  Result := ExpandConstant('{param:' + Name + '|' + Default + '}');
end;

function DefaultNodeName(): string;
begin
  Result := GetEnv('COMPUTERNAME');
  if Result = '' then
    Result := 'edge-node';
end;

function EnvFileValue(const Contents, Name, Default: string): string;
var
  Marker: string;
  P, Q: Integer;
begin
  Result := Default;
  Marker := '$env:' + Name + ' = ''';
  P := Pos(Marker, Contents);
  if P = 0 then exit;
  P := P + Length(Marker);
  Q := P;
  while (Q <= Length(Contents)) and (Contents[Q] <> '''') do
    Q := Q + 1;
  Result := Copy(Contents, P, Q - P);
end;

procedure InitializeWizard;
var
  EnvText: AnsiString;
  Existing: string;
begin
  Existing := '';
  if LoadStringFromFile(ExpandConstant('{commonappdata}\WISP\edge.env.ps1'), EnvText) then
    Existing := string(EnvText);
  ConfigPage := CreateInputQueryPage(wpSelectDir,
    'Connect to WISP Central', 'Where should this probe report?',
    'From the dashboard''s Network page, register this probe under Probes and paste ' +
    'the enrollment token below. Leave the token empty on a trusted network or when ' +
    'using mTLS. All of this can be edited later in ' +
    'C:\ProgramData\WISP\edge.env.ps1.');
  ConfigPage.Add('Central URL (https://central.example.net):', False);
  ConfigPage.Add('Enrollment token:', False);
  ConfigPage.Add('Organization id:', False);
  ConfigPage.Add('Node id:', False);
  ConfigPage.Values[0] := ParamOr('Central', EnvFileValue(Existing, 'WISP_CENTRAL_URL', ''));
  ConfigPage.Values[1] := ParamOr('Token', EnvFileValue(Existing, 'WISP_CENTRAL_TOKEN', ''));
  ConfigPage.Values[2] := ParamOr('Org', EnvFileValue(Existing, 'WISP_ORG_ID', 'default'));
  ConfigPage.Values[3] := ParamOr('Node', EnvFileValue(Existing, 'WISP_NODE_ID', DefaultNodeName()));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (ConfigPage <> nil) and (CurPageID = ConfigPage.ID) then begin
    if Trim(ConfigPage.Values[0]) = '' then begin
      MsgBox('Central URL is required - without it the probe has nowhere to report.',
             mbError, MB_OK);
      Result := False;
      exit;
    end;
    ConfigPage.Values[0] := Trim(ConfigPage.Values[0]);
    if Pos('://', ConfigPage.Values[0]) = 0 then
      ConfigPage.Values[0] := 'https://' + ConfigPage.Values[0];
  end;
end;

function GetCentral(Param: string): string; begin Result := ConfigPage.Values[0]; end;
function GetToken(Param: string): string;   begin Result := ConfigPage.Values[1]; end;
function GetOrg(Param: string): string;     begin Result := ConfigPage.Values[2]; end;
function GetNode(Param: string): string;    begin Result := ConfigPage.Values[3]; end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Params: string;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then exit;
  Params := '-NoProfile -ExecutionPolicy Bypass -File "'
    + ExpandConstant('{app}\windows-task.ps1') + '" -Register'
    + ' -Prefix "' + ExpandConstant('{app}\bin') + '"'
    + ' -Central "' + GetCentral('') + '"'
    + ' -Token "' + GetToken('') + '"'
    + ' -Org "' + GetOrg('') + '"'
    + ' -Node "' + GetNode('') + '"';
  if not Exec('powershell.exe', Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    SuppressibleMsgBox('Could not run PowerShell to register the WISP Edge service. '
      + 'Register manually: "' + ExpandConstant('{app}\windows-task.ps1') + '" -Register',
      mbError, MB_OK, IDOK);
    exit;
  end;
  if ResultCode = 10 then
    SuppressibleMsgBox('WISP Edge installed, but the probe did not confirm it is '
      + 'reporting yet.' + #13#10#13#10 + 'Check C:\ProgramData\WISP\install.log and '
      + 'C:\ProgramData\WISP\logs\edge.log - the tray icon (near the clock, under '
      + '"Show hidden icons") shows live status.', mbInformation, MB_OK, IDOK)
  else if ResultCode <> 0 then
    SuppressibleMsgBox('WISP Edge service registration FAILED (exit code '
      + IntToStr(ResultCode) + ').' + #13#10#13#10
      + 'See C:\ProgramData\WISP\install.log for details.', mbError, MB_OK, IDOK);
end;
