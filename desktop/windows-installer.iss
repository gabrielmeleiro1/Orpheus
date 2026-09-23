; Compile with Inno Setup 7 (extended-length paths are required by PyTorch).
; First build the CUDA app in PowerShell:
; $env:ORPHEUS_RUNTIME_PROFILE = "cuda"
; npm run desktop:build -- --no-bundle
; Use /O with a local temporary directory when building from OneDrive.
; /EXTRACTONLY=1 /DIR=<temporary directory> verifies the payload without
; registering an installation, creating shortcuts, or launching the app.

[Setup]
AppId=com.orpheus.desktop
AppName=Orpheus
AppVersion=1.0.0
AppPublisher=Orpheus
DefaultDirName={localappdata}\Orpheus
DefaultGroupName=Orpheus
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=release
OutputBaseFilename=Orpheus_1.0.0_x64-setup
SetupIconFile=src-tauri\icons\icon.ico
UninstallDisplayIcon={app}\Orpheus.ico
Uninstallable=not ExtractOnly
CreateUninstallRegKey=not ExtractOnly
; Maximum compression keeps the CUDA installer under GitHub's 2 GiB release-asset limit.
Compression=lzma2/ultra64
LZMANumBlockThreads=8
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "src-tauri\target\release\orpheus_desktop.exe"; DestDir: "{app}"; DestName: "Orpheus.exe"; Flags: ignoreversion
Source: "src-tauri\icons\icon.ico"; DestDir: "{app}"; DestName: "Orpheus.ico"; Flags: ignoreversion
Source: "src-tauri\binaries\*"; DestDir: "{app}\binaries"; Excludes: ".gitkeep"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{app}\binaries\orpheus-desktop-backend"; Check: not ExtractOnly

[Icons]
Name: "{autoprograms}\Orpheus"; Filename: "{app}\Orpheus.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Orpheus.ico"; AppUserModelID: "com.orpheus.desktop"; Check: not ExtractOnly
Name: "{autodesktop}\Orpheus"; Filename: "{app}\Orpheus.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Orpheus.ico"; AppUserModelID: "com.orpheus.desktop"; Tasks: desktopicon; Check: not ExtractOnly

[Run]
Filename: "{app}\Orpheus.exe"; Description: "Launch Orpheus"; Flags: nowait postinstall skipifsilent; Check: not ExtractOnly

[Code]
procedure InitializeWizard;
begin
  { Native themed glyphs grow with DPI. Reserve enough inset and row height
    for the full checkbox, including at 200% and higher scaling. }
  WizardForm.TasksList.Offset := ScaleX(16);
  WizardForm.TasksList.MinItemHeight := ScaleY(28);
  WizardForm.RunList.Offset := ScaleX(16);
  WizardForm.RunList.MinItemHeight := ScaleY(28);
end;

function ExtractOnly: Boolean;
begin
  Result := ExpandConstant('{param:EXTRACTONLY|0}') = '1';
end;
