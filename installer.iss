; ============================================================================
; SteamScout Installer — Inno Setup Script
;
; Prerequisites:
;   1. Build the EXE first:  python build.py
;   2. Install Inno Setup:   https://jrsoftware.org/isinfo.php
;   3. Open this file in Inno Setup Compiler and click Build.
; ============================================================================

#define MyAppName      "SteamScout"
#define MyAppVersion   "1.0.0"
#define MyAppPublisher "SteamScout"
#define MyAppExeName   "SteamScout.exe"

[Setup]
AppId={{7E8A3F95-B2C4-41D8-9FA6-3C5D7E1A8B4F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=installer_output
OutputBaseFilename=SteamScoutSetup
SetupIconFile=steamscout.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart";   Description: "Start SteamScout when Windows starts"; GroupDescription: "Startup:"

[Files]
Source: "dist\{#MyAppExeName}";  DestDir: "{app}"; Flags: ignoreversion
Source: "SteamScoutIcon.png";    DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}";  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";      Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\SteamScout"
