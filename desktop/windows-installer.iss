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
; Staging and rollback paths are shared by every installer version. Serialize
; setup instances across sessions before recovery can touch the app or backend.
SetupMutex=OrpheusSetupMutex,Global\OrpheusSetupMutex

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "src-tauri\target\release\orpheus_desktop.exe"; DestDir: "{app}"; DestName: "Orpheus.exe.new"; Flags: ignoreversion
Source: "src-tauri\icons\icon.ico"; DestDir: "{app}"; DestName: "Orpheus.ico"; Flags: ignoreversion
Source: "src-tauri\binaries\orpheus-desktop-backend\*"; DestDir: "{app}\binaries\orpheus-desktop-backend.new"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
Type: files; Name: "{app}\Orpheus.exe"
Type: files; Name: "{app}\Orpheus.exe.new"
Type: files; Name: "{app}\Orpheus.exe.old"
Type: files; Name: "{app}\.orpheus-install-pending"
Type: filesandordirs; Name: "{app}\binaries\orpheus-desktop-backend"
Type: filesandordirs; Name: "{app}\binaries\orpheus-desktop-backend.new"
Type: filesandordirs; Name: "{app}\binaries\orpheus-desktop-backend.old"

[Icons]
Name: "{autoprograms}\Orpheus"; Filename: "{app}\Orpheus.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Orpheus.ico"; AppUserModelID: "com.orpheus.desktop"; Check: not ExtractOnly
Name: "{autodesktop}\Orpheus"; Filename: "{app}\Orpheus.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Orpheus.ico"; AppUserModelID: "com.orpheus.desktop"; Tasks: desktopicon; Check: not ExtractOnly

[Run]
Filename: "{app}\Orpheus.exe"; Description: "Launch Orpheus"; Flags: nowait postinstall skipifsilent; Check: not ExtractOnly and InstallationReady

[Code]
const
  LegacyUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\Orpheus';

var
  BackendPath, StagedBackendPath, BackupBackendPath: String;
  BackendBackedUp, BackendActivated, InstallComplete: Boolean;
  ShellPath, StagedShellPath, BackupShellPath: String;
  ShellBackedUp, ShellActivated: Boolean;
  TransactionPath: String;
  TransactionOwned: Boolean;
  BackendInstallFailed: Boolean;
  BackendInstallError: String;

function ExtractOnly: Boolean;
begin
  Result := ExpandConstant('{param:EXTRACTONLY|0}') = '1';
end;

function InstallationReady: Boolean;
begin
  Result := BackendActivated and ShellActivated and not BackendInstallFailed;
end;

procedure RegisterExtraCloseApplicationsResources;
begin
  if not ExtractOnly then begin
    RegisterExtraCloseApplicationsResource(ExpandConstant('{app}\Orpheus.exe'));
    RegisterExtraCloseApplicationsResource(ExpandConstant('{app}\binaries\orpheus-desktop-backend\orpheus-desktop-backend.exe'));
  end;
end;

function NormalizedAbsoluteInstallPath(Value: String): String;
begin
  Result := '';
  Value := RemoveQuotes(Trim(Value));
  if (Value = '') or ((Pos(':\', Value) <> 2) and (Copy(Value, 1, 2) <> '\\')) then
    exit;
  try
    Result := RemoveBackslashUnlessRoot(ExpandFileName(Value));
  except
    { Ambiguous or malformed registrations must be preserved. }
    Result := '';
  end;
end;

function IsOrphanLegacyRegistration(
  const AppDir, LegacyLocation, LegacyCommand, DisplayName, Publisher: String): Boolean;
var
  AppPath, LegacyPath, ExpectedUninstaller, Command: String;
begin
  Result := False;
  if (CompareText(Trim(DisplayName), 'Orpheus') <> 0) or
     (CompareText(Trim(Publisher), 'Orpheus') <> 0) then
    exit;
  AppPath := NormalizedAbsoluteInstallPath(AppDir);
  LegacyPath := NormalizedAbsoluteInstallPath(LegacyLocation);
  if (AppPath = '') or (LegacyPath = '') or (CompareText(AppPath, LegacyPath) <> 0) then
    exit;
  ExpectedUninstaller := AddBackslash(AppPath) + 'uninstall.exe';
  Command := Trim(LegacyCommand);
  if (CompareText(Command, ExpectedUninstaller) <> 0) and
     (CompareText(Command, '"' + ExpectedUninstaller + '"') <> 0) then
    exit;
  Result := not FileExists(ExpectedUninstaller) and not DirExists(ExpectedUninstaller);
end;

procedure RemoveOrphanLegacyRegistration;
var
  Location, Command, DisplayName, Publisher: String;
begin
  if ExtractOnly or not InstallComplete then
    exit;
  if not FileExists(ExpandConstant('{uninstallexe}')) then
    exit;
  if not RegQueryStringValue(HKCU, LegacyUninstallKey, 'InstallLocation', Location) then
    exit;
  if not RegQueryStringValue(HKCU, LegacyUninstallKey, 'UninstallString', Command) then
    exit;
  if not RegQueryStringValue(HKCU, LegacyUninstallKey, 'DisplayName', DisplayName) then
    exit;
  if not RegQueryStringValue(HKCU, LegacyUninstallKey, 'Publisher', Publisher) then
    exit;
  if not IsOrphanLegacyRegistration(ExpandConstant('{app}'), Location, Command, DisplayName, Publisher) then
    exit;
  { Remove only obsolete Apps metadata. Never run an old uninstaller or delete files. }
  if RegDeleteKeyIncludingSubkeys(HKCU, LegacyUninstallKey) then
    Log('Removed the same-directory orphan legacy Orpheus uninstall registration.')
  else
    Log('Could not remove the orphan legacy Orpheus uninstall registration; it was preserved.');
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

function RecoverShell: String;
begin
  Result := '';
  if not FileExists(ShellPath) and FileExists(BackupShellPath) then begin
    if not RenameFile(BackupShellPath, ShellPath) then begin
      Result := 'Could not restore the previous app from ' + BackupShellPath + '. Close Orpheus and retry.';
      exit;
    end;
  end;
  if FileExists(StagedShellPath) and not DeleteFile(StagedShellPath) then begin
    Result := 'Could not remove the interrupted app staging file ' + StagedShellPath + '. Close applications using it and retry.';
    exit;
  end;
  if FileExists(ShellPath) and FileExists(BackupShellPath) then begin
    if not DeleteFile(BackupShellPath) then
      Result := 'Could not remove the previous app backup ' + BackupShellPath + '. Close applications using it and retry.';
  end;
end;

function PayloadExists(const Path: String; Directory: Boolean): Boolean;
begin
  if Directory then
    Result := DirExists(Path)
  else
    Result := FileExists(Path);
end;

function RemovePayload(const Path: String; Directory: Boolean): Boolean;
begin
  if Directory then
    Result := DelTree(Path, True, True, True)
  else
    Result := DeleteFile(Path);
end;

function RecoverPendingComponent(const ActivePath, StagedPath, BackupPath: String;
  HadPrevious, Directory: Boolean): String;
begin
  Result := '';
  if (HadPrevious and PayloadExists(BackupPath, Directory)) or not HadPrevious then begin
    if PayloadExists(ActivePath, Directory) then begin
      if PayloadExists(StagedPath, Directory) and not RemovePayload(StagedPath, Directory) then begin
        Result := 'Could not clear interrupted staging at ' + StagedPath;
        exit;
      end;
      if not RenameFile(ActivePath, StagedPath) then begin
        Result := 'Could not move the interrupted installation from ' + ActivePath;
        exit;
      end;
    end;
    if HadPrevious and not RenameFile(BackupPath, ActivePath) then
      Result := 'Could not restore the previous installation from ' + BackupPath;
  end else if not PayloadExists(ActivePath, Directory) then
    Result := 'The previous installation is missing from ' + ActivePath;
end;

function RecoverPendingInstallation: String;
var
  State: AnsiString;
  ShellError, BackendError: String;
begin
  Result := '';
  if not FileExists(TransactionPath) then
    exit;
  if not LoadStringFromFile(TransactionPath, State) or
    ((State <> 'OrpheusInstallV1;0;0') and (State <> 'OrpheusInstallV1;0;1') and
     (State <> 'OrpheusInstallV1;1;0') and (State <> 'OrpheusInstallV1;1;1')) then begin
    Result := 'Could not read the interrupted installation record at ' + TransactionPath;
    exit;
  end;
  { A process kill can occur between the two publications. Restore both old
    versions before any retry is allowed to discard backups or extract files. }
  ShellError := RecoverPendingComponent(ShellPath, StagedShellPath, BackupShellPath,
    (State = 'OrpheusInstallV1;1;0') or (State = 'OrpheusInstallV1;1;1'), False);
  BackendError := RecoverPendingComponent(BackendPath, StagedBackendPath, BackupBackendPath,
    (State = 'OrpheusInstallV1;0;1') or (State = 'OrpheusInstallV1;1;1'), True);
  if (ShellError <> '') or (BackendError <> '') then begin
    Result := ShellError + #13#10 + BackendError + #13#10 + 'Close Orpheus and retry setup.';
    exit;
  end;
  if not DeleteFile(TransactionPath) then
    Result := 'Could not remove the recovered installation record at ' + TransactionPath;
end;

procedure BeginInstallationTransaction;
var
  State: String;
begin
  TransactionOwned := True;
  State := 'OrpheusInstallV1;';
  if FileExists(ShellPath) then State := State + '1;' else State := State + '0;';
  if DirExists(BackendPath) then State := State + '1' else State := State + '0';
  if not SaveStringToFile(TransactionPath, State, False) then
    RaiseException('Could not record the installation transaction at ' + TransactionPath);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  BackendPath := ExpandConstant('{app}\binaries\orpheus-desktop-backend');
  StagedBackendPath := BackendPath + '.new';
  BackupBackendPath := BackendPath + '.old';
  ShellPath := ExpandConstant('{app}\Orpheus.exe');
  StagedShellPath := ShellPath + '.new';
  BackupShellPath := ShellPath + '.old';
  TransactionPath := ExpandConstant('{app}\.orpheus-install-pending');
  Result := RecoverPendingInstallation;
  if Result <> '' then
    exit;
  Result := RecoverShell;
  if Result <> '' then
    exit;
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

procedure RestoreShell;
begin
  if ShellActivated then begin
    if not RenameFile(ShellPath, StagedShellPath) then
      RaiseException('Could not remove the new app. The previous app remains at ' + BackupShellPath);
    ShellActivated := False;
  end;
  if ShellBackedUp then begin
    if not RenameFile(BackupShellPath, ShellPath) then
      RaiseException('Could not restore the previous app. Restore it from ' + BackupShellPath);
    ShellBackedUp := False;
  end;
end;

procedure RestoreInstallation;
var
  RestoreError: String;
begin
  RestoreError := '';
  try
    RestoreShell;
  except
    RestoreError := GetExceptionMessage;
  end;
  try
    RestoreBackend;
  except
    RestoreError := RestoreError + #13#10 + GetExceptionMessage;
  end;
  if (RestoreError = '') and TransactionOwned then begin
    if FileExists(TransactionPath) and not DeleteFile(TransactionPath) then
      RestoreError := 'Could not remove the rolled-back installation record at ' + TransactionPath
    else
      TransactionOwned := False;
  end;
  if RestoreError <> '' then
    RaiseException(RestoreError);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    try
      BeginInstallationTransaction;
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
      if FileExists(ShellPath) then begin
        if not RenameFile(ShellPath, BackupShellPath) then
          RaiseException('Could not preserve the existing app. Close Orpheus and retry.');
        ShellBackedUp := True;
      end;
      if not RenameFile(StagedShellPath, ShellPath) then
        RaiseException('Could not activate the new app. The previous app and backend will be restored.');
      ShellActivated := True;
      { Removing the record commits the pair. Backups can then be cleaned at
        ssDone or by a later setup after an interruption during cleanup. }
      if not DeleteFile(TransactionPath) then
        RaiseException('Could not commit the installation transaction at ' + TransactionPath);
      TransactionOwned := False;
    except
      { Inno continues after exceptions from ssPostInstall. Track failure
        explicitly so ssDone, the launch checkbox, and the exit code agree. }
      BackendInstallFailed := True;
      BackendInstallError := GetExceptionMessage;
      try
        RestoreInstallation;
      except
        BackendInstallError := BackendInstallError + #13#10 + GetExceptionMessage;
      end;
      Log('Application installation failed: ' + BackendInstallError);
      SuppressibleMsgBox(BackendInstallError, mbCriticalError, MB_OK, IDOK);
    end;
  end;
  if (CurStep = ssDone) and InstallationReady then begin
    InstallComplete := True;
    if ShellBackedUp then
      DeleteFile(BackupShellPath);
    if BackendBackedUp then
      DelTree(BackupBackendPath, True, True, True);
    RemoveOrphanLegacyRegistration;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  if BackendInstallFailed then
    Result := 1
  else
    Result := 0;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and BackendInstallFailed then begin
    WizardForm.FinishedHeadingLabel.Caption := 'Orpheus could not be installed';
    WizardForm.FinishedLabel.Caption := BackendInstallError + #13#10#13#10 +
      'Close applications using Orpheus and run setup again.';
  end;
end;

procedure DeinitializeSetup;
begin
  if not InstallComplete then begin
    { Setup is closing; a restore failure here has nowhere to surface, and
      the pending transaction record lets the next setup finish the rollback. }
    try
      RestoreInstallation;
    except
      Log('Could not restore the previous installation: ' + GetExceptionMessage);
    end;
  end;
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
