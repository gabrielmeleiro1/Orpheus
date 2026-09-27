; Compile with Inno Setup 7 (extended-length paths are required by PyTorch).
; First build the CUDA app in PowerShell:
; $env:ORPHEUS_RUNTIME_PROFILE = "cuda"
; npm run desktop:build -- --no-bundle
; Use /O with a local temporary directory when building from OneDrive.
; /EXTRACTONLY=1 /DIR=<temporary directory> verifies the payload without
; registering an installation, creating shortcuts, or launching the app.

#define RepoRoot SourcePath + ".."
#define BuildPython RepoRoot + "\.venv\Scripts\python.exe"
#if Exec(BuildPython, '"' + RepoRoot + '\scripts\build_desktop_backend.py" --validate-existing --target-triple x86_64-pc-windows-msvc --runtime-profile cuda', RepoRoot, 1, SW_HIDE) != 0
  #error CUDA sidecar validation failed. Rebuild with --runtime-profile cuda before compiling this installer.
#endif

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
Source: "src-tauri\binaries\orpheus-desktop-backend\*"; DestDir: "{app}\binaries\orpheus-desktop-backend.new"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
Type: filesandordirs; Name: "{app}\binaries\orpheus-desktop-backend"

[Icons]
Name: "{autoprograms}\Orpheus"; Filename: "{app}\Orpheus.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Orpheus.ico"; AppUserModelID: "com.orpheus.desktop"; Check: not ExtractOnly
Name: "{autodesktop}\Orpheus"; Filename: "{app}\Orpheus.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Orpheus.ico"; AppUserModelID: "com.orpheus.desktop"; Tasks: desktopicon; Check: not ExtractOnly

[Run]
Filename: "{app}\Orpheus.exe"; Description: "Launch Orpheus"; Flags: nowait postinstall skipifsilent; Check: not ExtractOnly

[Code]
var
  BackendPath, StagedBackendPath, BackupBackendPath: String;
  BackendBackedUp, BackendActivated, InstallComplete: Boolean;

function ExtractOnly: Boolean;
begin
  Result := ExpandConstant('{param:EXTRACTONLY|0}') = '1';
end;

function WebView2VersionInstalled(RootKey: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey,
    'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'pv', Version) and (Version <> '') and (Version <> '0.0.0.0');
end;

function WebView2Installed: Boolean;
begin
  Result := WebView2VersionInstalled(HKLM32) or WebView2VersionInstalled(HKCU);
end;

function RecoverBackend: String;
begin
  Result := '';
  if not DirExists(BackendPath) and DirExists(BackupBackendPath) then begin
    if not RenameFile(BackupBackendPath, BackendPath) then begin
      Result := 'Could not restore the previous backend from ' + BackupBackendPath + '. Close Orpheus and retry.';
      exit;
    end;
  end;
  if DirExists(StagedBackendPath) and not DelTree(StagedBackendPath, True, True, True) then begin
    Result := 'Could not remove the interrupted backend staging directory ' + StagedBackendPath + '. Close applications using it and retry.';
    exit;
  end;
  if DirExists(BackendPath) and DirExists(BackupBackendPath) then begin
    if not DelTree(BackupBackendPath, True, True, True) then
      Result := 'Could not remove the previous backend backup ' + BackupBackendPath + '. Close applications using it and retry.';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  BackendPath := ExpandConstant('{app}\binaries\orpheus-desktop-backend');
  StagedBackendPath := BackendPath + '.new';
  BackupBackendPath := BackendPath + '.old';
  Result := RecoverBackend;
  if Result <> '' then
    exit;
  if ExtractOnly or WebView2Installed then
    exit;
  try
    DownloadTemporaryFile('https://go.microsoft.com/fwlink/p/?LinkId=2124703',
      'MicrosoftEdgeWebview2Setup.exe', '', nil);
    if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
      '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException('The Microsoft WebView2 installer could not be started.');
    if ResultCode <> 0 then
      RaiseException(Format('The Microsoft WebView2 installer returned exit code %d.', [ResultCode]));
    if not WebView2Installed then
      RaiseException('Microsoft WebView2 was not detected after installation.');
  except
    Result := 'Orpheus requires Microsoft Edge WebView2 Runtime. Setup could not install it: ' +
      GetExceptionMessage + ' Check your internet connection or install the Evergreen WebView2 Runtime, then retry.';
  end;
end;

procedure RestoreBackend;
begin
  if BackendActivated then begin
    if not RenameFile(BackendPath, StagedBackendPath) then
      RaiseException('Could not remove the new backend. The previous backend remains at ' + BackupBackendPath);
    BackendActivated := False;
  end;
  if BackendBackedUp then begin
    if not RenameFile(BackupBackendPath, BackendPath) then
      RaiseException('Could not restore the previous backend. Restore it from ' + BackupBackendPath);
    BackendBackedUp := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    { Extraction succeeded. Keep the old payload until the installation finishes. }
    if DirExists(BackendPath) then begin
      if not RenameFile(BackendPath, BackupBackendPath) then
        RaiseException('Could not preserve the existing backend. Close Orpheus and retry.');
      BackendBackedUp := True;
    end;
    if not RenameFile(StagedBackendPath, BackendPath) then begin
      RestoreBackend;
      RaiseException('Could not activate the new backend. The previous backend was preserved.');
    end;
    BackendActivated := True;
  end;
  if CurStep = ssDone then begin
    InstallComplete := True;
    if BackendBackedUp then
      DelTree(BackupBackendPath, True, True, True);
  end;
end;

procedure DeinitializeSetup;
begin
  if not InstallComplete then
    RestoreBackend;
end;

procedure InitializeWizard;
begin
  { Native themed glyphs grow with DPI. Reserve enough inset and row height
    for the full checkbox, including at 200% and higher scaling. }
  WizardForm.TasksList.Offset := ScaleX(16);
  WizardForm.TasksList.MinItemHeight := ScaleY(28);
  WizardForm.RunList.Offset := ScaleX(16);
  WizardForm.RunList.MinItemHeight := ScaleY(28);
end;
