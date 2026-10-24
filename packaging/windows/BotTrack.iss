; BotTrack for Windows: installs for this Windows user only (no admin prompt), with a Start menu and desktop
; shortcut. Built by packaging/windows/build_windows.ps1. Uninstalling removes the program; your data in
; %LOCALAPPDATA%\BotTrack stays unless you delete that folder.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\win\BotTrack"
#endif
#ifndef OutDir
  #define OutDir "..\..\dist"
#endif

[Setup]
AppId={{7B3F2C1E-5D8A-4E6B-9C2F-B07A0C11E5D4}
AppName=BotTrack
AppVersion={#AppVersion}
AppPublisher=BotTrack
DefaultDirName={localappdata}\Programs\BotTrack
DefaultGroupName=BotTrack
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=BotTrack-Setup
SetupIconFile={#SourceDir}\BotTrack.ico
UninstallDisplayIcon={app}\BotTrack.ico
Compression=lzma2/fast
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=yes

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Old code and packages from a previous install, so nothing stale is left behind.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"

[Icons]
Name: "{group}\BotTrack"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\BotTrack.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\BotTrack.ico"
Name: "{userdesktop}\BotTrack"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\BotTrack.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\BotTrack.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Put a BotTrack shortcut on the desktop"

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\BotTrack.pyw"""; WorkingDir: "{app}"; Description: "Open BotTrack"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /IM ollama.exe /F & taskkill /IM cloudflared.exe /F"; Flags: runhidden; RunOnceId: "StopEngines"
