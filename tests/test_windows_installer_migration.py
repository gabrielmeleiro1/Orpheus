"""Strict legacy-registration migration, including native Pascal predicate checks."""

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from modules.subprocess_utils import background_process_kwargs


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "desktop/windows-installer.iss").read_text(encoding="utf-8")
_ORIGINAL_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", ""))


def _code(name):
    return re.search(
        rf"(?:function|procedure) {name}\b.*?(?=\n(?:function|procedure) |\Z)",
        INSTALLER, re.S,
    ).group()


def test_legacy_registration_cleanup_is_success_only_and_metadata_only():
    cleanup = _code("RemoveOrphanLegacyRegistration")
    assert "if ExtractOnly or not InstallComplete then\n    exit;" in cleanup
    assert "if not FileExists(ExpandConstant('{uninstallexe}')) then\n    exit;" in cleanup
    assert "LegacyUninstallKey = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Orpheus';" in INSTALLER
    assert cleanup.count("RegDeleteKeyIncludingSubkeys(HKCU, LegacyUninstallKey)") == 1
    assert re.findall(r"RegQueryStringValue\(HKCU, LegacyUninstallKey, '([^']+)'", cleanup) == [
        "InstallLocation", "UninstallString", "DisplayName", "Publisher",
    ]
    assert cleanup.index("IsOrphanLegacyRegistration(") < cleanup.index("RegDeleteKeyIncludingSubkeys(")
    assert not re.search(r"\b(?:Exec|ShellExec|DeleteFile|DelTree|RenameFile)\s*\(", cleanup)
    steps = _code("CurStepChanged")
    assert steps.index("CurStep = ssDone") < steps.index("InstallComplete := True") < steps.index("RemoveOrphanLegacyRegistration;")
    assert INSTALLER.count("RemoveOrphanLegacyRegistration;") == 2  # Declaration and successful-install call.


def _inno_major_version(compiler):
    try:
        banner = subprocess.run(
            [str(compiler)], capture_output=True, text=True, timeout=30,
            **background_process_kwargs(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"Inno Setup (\d+)", banner)
    return int(match.group(1)) if match else None


def _inno_compiler():
    # The installer script needs Inno Setup 7; an older ISCC on PATH (CI
    # images ship 6) rejects its syntax.
    candidates = [
        _ORIGINAL_LOCAL_APP_DATA / "Programs/Inno Setup 7/ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 7/ISCC.exe",
        Path(shutil.which("ISCC.exe") or "__missing_iscc__"),
    ]
    return next(
        (path for path in candidates if path.is_file() and (_inno_major_version(path) or 0) >= 7),
        None,
    )


def _pascal_string(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.skipif(os.name != "nt", reason="Native Inno Setup predicate execution needs Windows")
def test_legacy_migration_predicate_in_native_inno_setup(tmp_path):
    compiler = _inno_compiler()
    if compiler is None:
        pytest.skip("Inno Setup 7 is not installed")

    app = tmp_path / "Orpheus install é"
    app.mkdir()
    legacy_exe = app / "uninstall.exe"
    existing = tmp_path / "existing legacy app"
    existing.mkdir()
    (existing / "uninstall.exe").write_bytes(b"preserve existing uninstaller")
    directory = tmp_path / "uninstaller is a directory"
    (directory / "uninstall.exe").mkdir(parents=True)
    result_file = tmp_path / "predicate-results.txt"
    defaults = (str(app), str(app), f'"{legacy_exe}"', "Orpheus", "orpheus")
    cased_app = str(app).replace("Orpheus install", "ORPHEUS INSTALL")

    cases = [
        ("observed quoted registration", (str(app), f'"{app}"', f'"{legacy_exe}"', "Orpheus", "orpheus"), True),
        ("unquoted command", (str(app), str(app), str(legacy_exe), "Orpheus", "Orpheus"), True),
        ("case and trailing separator", (str(app), cased_app + "\\", f'"{cased_app}\\UNINSTALL.EXE"', "ORPHEUS", "Orpheus"), True),
        ("different install path", (str(app), str(tmp_path / "another install"), *defaults[2:]), False),
        ("unexpected command", (*defaults[:2], str(app / "unins000.exe"), *defaults[3:]), False),
        ("command arguments", (*defaults[:2], f'"{legacy_exe}" /S', *defaults[3:]), False),
        ("unexpected display name", (*defaults[:3], "Different app", defaults[4]), False),
        ("unexpected publisher", (*defaults[:4], "Different publisher"), False),
        ("empty location", (defaults[0], "", *defaults[2:]), False),
        ("relative location", (defaults[0], app.name, *defaults[2:]), False),
        ("existing legacy uninstaller", (str(existing), str(existing), str(existing / "uninstall.exe"), *defaults[3:]), False),
        ("directory instead of uninstaller", (str(directory), str(directory), str(directory / "uninstall.exe"), *defaults[3:]), False),
    ]
    checks = "\n".join(
        "  CheckCase(" + _pascal_string(label) + ", IsOrphanLegacyRegistration("
        + ", ".join(_pascal_string(value) for value in values)
        + "), " + str(expected) + ");"
        for label, values, expected in cases
    )
    harness = tmp_path / "legacy-migration-predicate.iss"
    harness.write_text(
        "[Setup]\nAppId=OrpheusPredicateHarness\nAppName=Orpheus predicate harness\n"
        "AppVersion=1\nCreateAppDir=no\nUninstallable=no\nCreateUninstallRegKey=no\n"
        "PrivilegesRequired=lowest\nOutputBaseFilename=predicate-harness\nCompression=none\n"
        "[Code]\n"
        + INSTALLER.split("[Code]", 1)[1] + "\n"
        + "procedure CheckCase(const Name: String; Actual, Expected: Boolean);\n"
        "begin\n  if Actual <> Expected then RaiseException(Name + ' failed');\nend;\n"
        "function InitializeSetup: Boolean;\nbegin\n"
        + checks + "\n"
        + "  SaveStringToFile(" + _pascal_string(result_file) + ", 'all passed', False);\n"
        "  Result := False;\nend;\n",
        encoding="utf-8-sig",
    )
    compiled = subprocess.run(
        [str(compiler), "/Q", f"/O{tmp_path}", str(harness)],
        capture_output=True, text=True, timeout=60,
        **background_process_kwargs(),
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    log = tmp_path / "native-predicate.log"
    executed = subprocess.run(
        [str(tmp_path / "predicate-harness.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", f"/LOG={log}"],
        capture_output=True, text=True, timeout=60,
        **background_process_kwargs(),
    )
    # InitializeSetup deliberately returns False after assertions: no files,
    # shortcuts, or registry records are installed by this native harness.
    assert executed.returncode in (1, 2), executed.stdout + executed.stderr
    assert result_file.is_file(), log.read_text(encoding="utf-8", errors="replace")
    assert result_file.read_text(encoding="utf-8") == "all passed"
    assert (existing / "uninstall.exe").read_bytes() == b"preserve existing uninstaller"
