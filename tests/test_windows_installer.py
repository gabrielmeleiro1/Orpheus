"""Windows installer payload and prerequisite contracts."""

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import build_desktop_backend

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "desktop" / "windows-installer.iss").read_text(encoding="utf-8")


def _code(name):
    return re.search(
        rf"(?:function|procedure) {name}\b.*?(?=\n(?:function|procedure) |\Z)",
        INSTALLER, re.S,
    ).group()


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


@pytest.mark.parametrize("problem", ["base", "missing_resource", "valid"])
def test_installer_compile_gate_validates_cuda_marker_and_resources(tmp_path, monkeypatch, problem):
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Darwin")
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


@pytest.mark.parametrize("host_system", ["Darwin", "Linux", "Windows"])
def test_windows_sidecar_checks_packaged_binaries_only_on_windows(tmp_path, monkeypatch, host_system):
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: host_system)
    for resource in build_desktop_backend.required_sidecar_resources(
        "x86_64-pc-windows-msvc", runtime_profile="cuda",
    ):
        path = tmp_path / resource
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"resource")
    (tmp_path / "runtime-profile.json").write_text(json.dumps({"profile": "cuda"}))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "configuration: --enable-static", ""))
    monkeypatch.setattr(build_desktop_backend.subprocess, "run", run)

    build_desktop_backend._validate_sidecar_resources(
        tmp_path, target_triple="x86_64-pc-windows-msvc", runtime_profile="cuda",
    )

    assert run.call_count == (2 if host_system == "Windows" else 0)
    if host_system == "Windows":
        assert [call.args[0] for call in run.call_args_list] == [
            [str(tmp_path / "bin" / f"{name}.exe"), "-version"] for name in ("ffmpeg", "ffprobe")
        ]
