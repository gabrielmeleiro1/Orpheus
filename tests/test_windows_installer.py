"""Windows installer payload and prerequisite contracts."""

import json
import re
from pathlib import Path

import pytest

from scripts import build_desktop_backend

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "desktop" / "windows-installer.iss").read_text(encoding="utf-8")


def _code(name):
    return re.search(
        rf"(?:function|procedure) {name}\b.*?(?=\n(?:function|procedure) |\Z)",
        INSTALLER, re.S,
    ).group()


def test_upgrade_stages_backend_and_preserves_previous_payload_until_success():
    assert "[InstallDelete]" not in INSTALLER
    sidecar = next(line for line in INSTALLER.splitlines() if line.startswith('Source: "src-tauri\\binaries'))
    assert 'DestDir: "{app}\\binaries\\orpheus-desktop-backend.new"' in sidecar
    steps = _code("CurStepChanged")
    assert steps.index("CurStep = ssPostInstall") < steps.index("RenameFile(BackendPath, BackupBackendPath)")
    assert steps.index("RenameFile(BackendPath, BackupBackendPath)") < steps.index("RenameFile(StagedBackendPath, BackendPath)")
    assert steps.index("CurStep = ssDone") < steps.index("DelTree(BackupBackendPath")
    assert re.search(r"if not RenameFile\(StagedBackendPath, BackendPath\) then begin\s+RestoreBackend;", steps)
    assert "if not InstallComplete then\n    RestoreBackend;" in _code("DeinitializeSetup")
    restore = _code("RestoreBackend")
    assert "RenameFile(BackupBackendPath, BackendPath)" in restore
    assert "DelTree" not in restore
    assert '[UninstallDelete]\nType: filesandordirs; Name: "{app}\\binaries\\orpheus-desktop-backend"' in INSTALLER


def test_webview2_is_detected_and_bootstrapped_before_installation():
    version_check = _code("WebView2VersionInstalled")
    assert r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in version_check
    assert "'pv', Version" in version_check
    assert "(Version <> '') and (Version <> '0.0.0.0')" in version_check
    assert "WebView2VersionInstalled(HKLM32) or WebView2VersionInstalled(HKCU)" in _code("WebView2Installed")
    prepare = _code("PrepareToInstall")
    assert prepare.index("if ExtractOnly or WebView2Installed then") < prepare.index("DownloadTemporaryFile(")
    assert "DownloadTemporaryFile('https://go.microsoft.com/fwlink/p/?LinkId=2124703'" in prepare
    assert "'{tmp}\\MicrosoftEdgeWebview2Setup.exe'" in prepare
    assert "'/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode" in prepare
    assert "if ResultCode <> 0 then" in prepare
    assert "if not WebView2Installed then" in prepare
    assert "except\n    Result := 'Orpheus requires Microsoft Edge WebView2 Runtime." in prepare


def test_upgrade_recovers_interrupted_payload_before_preparing_installation():
    prepare = _code("PrepareToInstall")
    assert "Result := RecoverBackend;\n  if Result <> '' then\n    exit;" in prepare
    assert prepare.index("BackupBackendPath :=") < prepare.index("Result := RecoverBackend;")
    assert prepare.index("Result := RecoverBackend;") < prepare.index("if ExtractOnly or WebView2Installed")
    assert "remove the .new staging directory before retrying" not in INSTALLER

    recovery = _code("RecoverBackend")
    assert re.search(
        r"if not DirExists\(BackendPath\) and DirExists\(BackupBackendPath\) then begin\s+"
        r"if not RenameFile\(BackupBackendPath, BackendPath\) then begin\s+"
        r"Result := .*?;\s+exit;", recovery,
    )
    assert recovery.index("RenameFile(BackupBackendPath, BackendPath)") < recovery.index("DelTree(StagedBackendPath")
    assert "if DirExists(StagedBackendPath) and not DelTree(StagedBackendPath, True, True, True) then begin" in recovery
    assert re.search(
        r"if DirExists\(BackendPath\) and DirExists\(BackupBackendPath\) then begin\s+"
        r"if not DelTree\(BackupBackendPath, True, True, True\) then\s+Result :=", recovery,
    )
    assert "DelTree(BackendPath," not in recovery
    assert "BackendBackedUp :=" not in recovery
    assert "BackendActivated :=" not in recovery


@pytest.mark.parametrize("problem", ["base", "missing_resource", "valid"])
def test_installer_compile_gate_validates_cuda_marker_and_resources(tmp_path, monkeypatch, problem):
    gate = re.search(r"#if Exec\(.*?\n\s+#error .*?\n#endif", INSTALLER).group()
    assert gate.index("--validate-existing") < gate.index("!= 0")
    assert "--runtime-profile cuda" in gate
    assert "--target-triple x86_64-pc-windows-msvc" in gate
    assert "build_desktop_backend.py" in gate
    assert ", RepoRoot, 1, SW_HIDE)" in gate
    monkeypatch.setattr(build_desktop_backend, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    executable = tmp_path / "orpheus-desktop-backend.exe"
    executable.write_bytes(b"test fixture")
    resources = build_desktop_backend.required_sidecar_resources(
        "x86_64-pc-windows-msvc", runtime_profile="cuda",
    )
    for resource in resources:
        path = tmp_path / resource
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"resource")
    (tmp_path / "runtime-profile.json").write_text(json.dumps({"profile": "base" if problem == "base" else "cuda"}))
    if problem == "missing_resource":
        next(tmp_path / path for path in resources if "espeak" in str(path)).unlink()
    if problem == "valid":
        assert build_desktop_backend.validate_existing_sidecar(
            target_triple="x86_64-pc-windows-msvc", runtime_profile="cuda",
        ) == executable
    else:
        with pytest.raises(RuntimeError, match="runtime profile mismatch|missing required runtime resources"):
            build_desktop_backend.validate_existing_sidecar(
                target_triple="x86_64-pc-windows-msvc", runtime_profile="cuda",
            )
