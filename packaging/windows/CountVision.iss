; CountVision Local - Windows setup (Inno Setup 6).
;
; Saved as UTF-8 with BOM (the German texts). Built by packaging\windows\build.ps1, which fills the "stage" folder first:
;   CountVision.exe        the program behind the icons (starts python\pythonw.exe -m countvision_desktop)
;   python\                embeddable Python 3.12 + all packages (API, counting engine, desktop app)
;   node\node.exe          Node.js for the app's web server
;   web\                   the app (Next.js standalone build)
;   pgsql\                 PostgreSQL 16 (bin, lib, share)
;   models\                YOLOX-Tiny (Apache-2.0)
;   tools\                 the scripts below (data folder rights, stop before update/uninstall)
;   version.txt, LICENSE.txt, THIRD-PARTY-NOTICES.txt, CountVision.ico
;
; Data (database, settings, logs, backups) lives in C:\ProgramData\CountVision, NOT in the program
; folder: an update replaces the program and keeps the data; the uninstaller asks.
;
;   iscc /DAppVersion=0.2.0 /DStageDir=...\stage /DRedistDir=...\redist /DOutputDir=...\dist CountVision.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\..\build\windows\stage"
#endif
#ifndef RedistDir
  #define RedistDir "..\..\build\windows\redist"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

#define AppName "CountVision"
#define AppExe "CountVision.exe"
#define AppUserModelID "CountVision.Local"
#define FirewallRule "CountVision app (shop network)"

[Setup]
; AppId must never change: Windows recognises updates and the uninstaller by it.
AppId={{8F3C6A52-4D1B-4C8E-9F27-6B2E1A7D5C31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Sami Hotak
AppPublisherURL=https://github.com/SamiHotak/countvision
AppSupportURL=https://github.com/SamiHotak/countvision
AppComments=Turns your cameras into visitor numbers. Runs on this PC, works without internet.
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} setup
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809 or newer (WebView2, Media Foundation H.264, PostgreSQL 16)
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=CountVision-Local-Setup-{#AppVersion}
SetupIconFile={#StageDir}\CountVision.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
LicenseFile={#StageDir}\LICENSE.txt
WizardStyle=modern
; English or German, chosen from Windows' language (asks only when it is neither)
ShowLanguageDialog=auto
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
; We stop CountVision ourselves (cleanly, see PrepareToInstall) instead of the Restart Manager.
CloseApplications=no
RestartApplications=no
; The autostart shortcut belongs to the person who installs (the one who will use CountVision).
UsedUserAreasWarning=no
SetupLogging=yes
ChangesEnvironment=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[CustomMessages]
english.DesktopIcon=Create a desktop icon
german.DesktopIcon=Symbol auf dem Desktop erstellen
english.Autostart=Start CountVision when I log in to Windows (counting starts by itself)
german.Autostart=CountVision bei der Windows-Anmeldung starten (zählt dann von selbst)
english.OpenNow=Open CountVision now
german.OpenNow=CountVision jetzt öffnen
english.StatusRuntime=Installing the Microsoft Visual C++ runtime ...
german.StatusRuntime=Microsoft Visual C++ Laufzeit wird installiert ...
english.StatusWebView=Installing Microsoft Edge WebView2 (the window engine) ...
german.StatusWebView=Microsoft Edge WebView2 (Fenster-Technik) wird installiert ...
english.StatusRights=Protecting the CountVision data folder ...
german.StatusRights=Der CountVision-Datenordner wird geschützt ...
english.DeleteData=Also delete ALL CountVision data on this PC?%n%nThis is the database with all counts, the settings, the logs AND the backups in %1.%n%nChoose "No" to keep them (for example to install CountVision again later).
german.DeleteData=Auch ALLE CountVision-Daten auf diesem PC löschen?%n%nDas sind die Datenbank mit allen Zählungen, die Einstellungen, die Protokolle UND die Sicherungen in %1.%n%nWählen Sie "Nein", um sie zu behalten (z. B. für eine spätere Neuinstallation).

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"
Name: "autostart"; Description: "{cm:Autostart}"

[InstallDelete]
; An update replaces the program parts completely (no old Python files mixed with new ones).
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\web"
Type: filesandordirs; Name: "{app}\node"
Type: filesandordirs; Name: "{app}\pgsql"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\tools"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\tools\stop-countvision.ps1"; Flags: dontcopy
Source: "{#RedistDir}\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: VCRuntimeNeeded
Source: "{#RedistDir}\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: WebView2Needed

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; AppUserModelID: "{#AppUserModelID}"; Comment: "Visitor and traffic counting"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--hidden"; WorkingDir: "{app}"; Tasks: autostart

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "{cm:StatusRuntime}"; Flags: waituntilterminated; Check: VCRuntimeNeeded
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "{cm:StatusWebView}"; Flags: waituntilterminated; Check: WebView2Needed
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\set-data-acl.ps1"" -DataDir ""{commonappdata}\{#AppName}"" -User ""{username}"""; StatusMsg: "{cm:StatusRights}"; Flags: runhidden waituntilterminated
; Phones and other PCs may only reach the app when the person switches "Allow phones and other PCs"
; on in the tray menu (until then it listens on this PC only). This rule spares them an
; administrator question at that moment: private networks only, the app's port only.
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden waituntilterminated
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\node\node.exe"" protocol=TCP localport=3000 profile=private enable=yes"; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "{cm:OpenNow}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\stop-countvision.ps1"" -AppDir ""{app}"" -DataDir ""{commonappdata}\{#AppName}"""; Flags: runhidden waituntilterminated; RunOnceId: "StopCountVision"
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "DeleteFirewallRule"

[UninstallDelete]
; __pycache__ folders and other files made after the install
Type: filesandordirs; Name: "{app}"

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function DataDir(): String;
begin
  Result := ExpandConstant('{commonappdata}\{#AppName}');
end;

{ Microsoft Edge WebView2 Runtime: part of Windows 11 and of current Windows 10. Without it the
  bootstrapper downloads it (needs internet); if that is not possible, CountVision opens in
  Microsoft Edge's app mode instead, which also works offline. }
function WebView2Needed(): Boolean;
var
  Version: String;
begin
  Result := True;
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key, 'pv', Version) and
     (Version <> '') and (Version <> '0.0.0.0') then
    Result := False
  else if RegQueryStringValue(HKCU, 'Software\' + WebView2Key, 'pv', Version) and
     (Version <> '') and (Version <> '0.0.0.0') then
    Result := False;
end;

{ Microsoft Visual C++ 2015-2022 runtime (x64), version 14.40 or newer: needed by PostgreSQL,
  ONNX Runtime and OpenCV. }
function VCRuntimeNeeded(): Boolean;
var
  Installed, Major, Minor: Cardinal;
begin
  Result := True;
  if RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and
     (Installed = 1) and
     RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Major', Major) and
     RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Minor', Minor) and
     ((Major > 14) or ((Major = 14) and (Minor >= 40))) then
    Result := False;
end;

procedure StopCountVision(const AppDir, Script: String);
var
  Code: Integer;
begin
  Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" -AppDir "' + AppDir +
       '" -DataDir "' + DataDir() + '"', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

{ An update: stop the running CountVision cleanly first (the files are in use otherwise). The new
  version makes a backup of the database at its first start, before it updates the database. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  AppDir: String;
begin
  Result := '';
  AppDir := ExpandConstant('{app}');
  if FileExists(AppDir + '\{#AppExe}') then
  begin
    ExtractTemporaryFile('stop-countvision.ps1');
    StopCountVision(AppDir, ExpandConstant('{tmp}\stop-countvision.ps1'));
  end;
end;

{ Uninstall: keep the data unless the person says otherwise. Silent uninstall keeps it, unless
  /DELETEDATA=1 is given. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Delete: Boolean;
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(DataDir()) then
  begin
    if UninstallSilent() then
      Delete := ExpandConstant('{param:DELETEDATA|0}') = '1'
    else
      Delete := MsgBox(FmtMessage(CustomMessage('DeleteData'), [DataDir()]), mbConfirmation,
                       MB_YESNO or MB_DEFBUTTON2) = IDYES;
    if Delete then
      DelTree(DataDir(), True, True, True);
  end;
end;
