"""Exercise the production Inno payload lifecycle in a disposable installation.

The harness uses a distinct app ID and tiny fixture files. It intentionally
does not validate the release payload or WebView2 bootstrap; those need their
own packaged/clean-machine checks.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

import pytest

from modules.subprocess_utils import background_process_kwargs
from tests.test_windows_installer_migration import _inno_compiler, _pascal_string


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "desktop/windows-installer.iss").read_text(encoding="utf-8")


def _run_installer(executable: Path, log: Path, *args: str) -> int:
    completed = subprocess.run(
        [str(executable), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
         f"/LOG={log}", *args],
        capture_output=True, text=True, timeout=90,
        **background_process_kwargs(),
    )
    # Inno's uninstaller can return from its original process while the
    # temporary child is finishing deletion. Wait for that child's log close
    # before a reinstall can race its cleanup.
    deadline = time.monotonic() + 10
    while log.is_file() and "Log closed." not in log.read_text(encoding="utf-8", errors="replace"):
        if time.monotonic() >= deadline:
            raise AssertionError(f"Installer did not finish writing {log}")
        time.sleep(0.05)
    return completed.returncode


@pytest.mark.skipif(os.name != "nt", reason="Native Inno lifecycle needs Windows")
def test_native_installer_upgrade_rollback_recovery_and_uninstall_preserve_data(tmp_path):
    import winreg

    compiler = _inno_compiler()
    if compiler is None:
        pytest.skip("Inno Setup 7 is not installed")

    app_id = "OrpheusLifecycleHarness" + uuid4().hex
    app_dir = tmp_path / "Orpheus install é"
    registry_key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_id}_is1"
    backend = app_dir / "binaries/orpheus-desktop-backend"
    shell = app_dir / "Orpheus.exe"
    staged_shell = app_dir / "Orpheus.exe.new"
    backup_shell = app_dir / "Orpheus.exe.old"
    transaction = app_dir / ".orpheus-install-pending"
    staged = backend.with_name(backend.name + ".new")
    backup = backend.with_name(backend.name + ".old")
    retained = [tmp_path / "profile/data/audiobooks.db", tmp_path / "models/model.bin"]
    for path in retained:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preserved user fixture")

    code = INSTALLER.split("[Code]", 1)[1]
    # Never write the production legacy registration or invoke an external
    # prerequisite installer. Everything else is the production Pascal code.
    code = code.replace(
        "      BackendActivated := True;",
        "      BackendActivated := True;\n"
        "      if ExpandConstant('{param:INTERRUPTAFTERBACKEND|0}') = '1' then begin\n"
        "        SaveStringToFile(ExpandConstant('{param:INTERRUPTSIGNAL}'), 'ready', False);\n"
        "        while True do Sleep(100);\n"
        "      end;",
    ).replace(
        "  if ExtractOnly or WebView2Installed then\n    exit;",
        "  if ExpandConstant('{param:FAILAFTERRECOVERY|0}') = '1' then begin\n"
        "    Result := 'Injected failure after interrupted-install recovery.';\n"
        "    exit;\n"
        "  end;\n"
        "  if True then\n    exit;",
    ).replace(
        "        ShellBackedUp := True;",
        "        ShellBackedUp := True;\n"
        "        if ExpandConstant('{param:FAILAFTERSHELLBACKUP|0}') = '1' then\n"
        "          RaiseException('Injected failure after preserving previous shell.');",
    ).replace(
        "      ShellActivated := True;",
        "      ShellActivated := True;\n"
        "      if ExpandConstant('{param:INTERRUPTAFTERSHELL|0}') = '1' then begin\n"
        "        SaveStringToFile(ExpandConstant('{param:INTERRUPTSIGNAL}'), 'ready', False);\n"
        "        while True do Sleep(100);\n"
        "      end;\n"
        "      if ExpandConstant('{param:FAILAFTERSHELLACTIVATE|0}') = '1' then\n"
        "        RaiseException('Injected failure after activating shell.');",
    ).replace(
        "        BackendBackedUp := True;",
        "        BackendBackedUp := True;\n"
        "        if ExpandConstant('{param:FAILAFTERBACKUP|0}') = '1' then\n"
        "          RaiseException('Injected failure after preserving previous backend.');",
    ).replace(
        "Uninstall\\Orpheus';", f"Uninstall\\{app_id}Legacy';",
    ).replace("if ExtractOnly or WebView2Installed then", "if True then")
    code = code.replace(
        "    BackendActivated := True;",
        "    BackendActivated := True;\n"
        "    if ExpandConstant('{param:FAILAFTERACTIVATE|0}') = '1' then\n"
        "      RaiseException('Injected failure after backend activation.');",
    )
    uninstall_delete = re.search(
        r"\[UninstallDelete\]\n(.*?)(?=\n\[)", INSTALLER, re.S,
    ).group(1)
    shell_destination = re.search(
        r'Source: "src-tauri\\target\\release\\orpheus_desktop.exe"; .*?DestName: "([^"]+)"',
        INSTALLER,
    ).group(1)
    installers = []
    for version in ("1", "2"):
        payload = tmp_path / f"payload-{version}"
        payload.mkdir()
        (payload / "backend.txt").write_text(version, encoding="utf-8")
        (payload / f"only-version-{version}.txt").write_text(version, encoding="utf-8")
        shell_source = tmp_path / f"shell-{version}.exe"
        shell_source.write_text(version, encoding="utf-8")
        script = tmp_path / f"lifecycle-{version}.iss"
        script.write_text(
            "[Setup]\n"
            f"AppId={app_id}\nAppName=Orpheus lifecycle harness\nAppVersion={version}\n"
            f"DefaultDirName={app_dir}\nPrivilegesRequired=lowest\n"
            "DisableProgramGroupPage=yes\nCompression=none\n"
            f"OutputBaseFilename=lifecycle-{version}\nSetupMutex={app_id}\n"
            "Uninstallable=yes\nCreateUninstallRegKey=yes\n"
            "[Files]\n"
            f'Source: "{shell_source}"; DestDir: "{{app}}"; DestName: "{shell_destination}"; '
            'Flags: ignoreversion\n'
            f'Source: "{payload}\\*"; DestDir: "{{app}}\\binaries\\orpheus-desktop-backend.new"; '
            'Flags: ignoreversion recursesubdirs createallsubdirs\n'
            f"[UninstallDelete]\n{uninstall_delete}\n[Code]\n{code}\n"
            # The production wizard expects Tasks/Run lists; no entries are
            # needed because this harness never creates shortcuts or runs apps.
            "function InitializeSetup: Boolean;\nbegin\n"
            f"  Result := Pos({_pascal_string(str(tmp_path))}, "
            "ExpandConstant('{param:DIR}')) = 1;\nend;\n",
            encoding="utf-8-sig",
        )
        compiled = subprocess.run(
            [str(compiler), "/Q", f"/O{tmp_path}", str(script)],
            capture_output=True, text=True, timeout=60,
            **background_process_kwargs(),
        )
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        installers.append(tmp_path / f"lifecycle-{version}.exe")

    def run_setup(version, label, *args):
        return _run_installer(
            installers[version - 1], tmp_path / f"{label}.log", f"/DIR={app_dir}", *args,
        )

    def check_payload(version):
        assert shell.read_text(encoding="utf-8") == str(version)
        assert (backend / "backend.txt").read_text(encoding="utf-8") == str(version)
        assert (backend / f"only-version-{version}.txt").is_file()
        assert not (backend / f"only-version-{3 - version}.txt").exists()
        assert not backup.exists()
        assert not backup_shell.exists()
        assert not transaction.exists()

    def installed_uninstaller():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_key) as key:
                path = Path(winreg.QueryValueEx(key, "UninstallString")[0].strip('"'))
        except FileNotFoundError:
            return None
        assert path.parent == app_dir
        return path

    def interrupt_upgrade(phase):
        signal_path = tmp_path / f"interrupted-{phase}-ready.txt"
        interrupted = subprocess.Popen(
            [str(installers[1]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
             f"/DIR={app_dir}", f"/LOG={tmp_path / f'hard-interruption-{phase}.log'}",
             f"/INTERRUPTAFTER{phase.upper()}=1", f"/INTERRUPTSIGNAL={signal_path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **background_process_kwargs(),
        )
        try:
            deadline = time.monotonic() + 30
            while not signal_path.is_file() and time.monotonic() < deadline:
                assert interrupted.poll() is None
                time.sleep(0.05)
            assert signal_path.is_file()
            assert shell.read_text(encoding="utf-8") == ("1" if phase == "backend" else "2")
            assert (backend / "backend.txt").read_text(encoding="utf-8") == "2"
            assert transaction.is_file()
        finally:
            if interrupted.poll() is None:
                killed = subprocess.run(
                    [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"),
                     "/PID", str(interrupted.pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=30,
                    **background_process_kwargs(),
                )
                assert killed.returncode == 0, killed.stdout + killed.stderr
            interrupted.wait(timeout=10)

    try:
        assert run_setup(1, "install") == 0
        check_payload(1)
        assert not staged.exists()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_key) as key:
            assert Path(winreg.QueryValueEx(key, "InstallLocation")[0]) == app_dir

        # Kill the native installer after publishing the new backend but before
        # replacing the old shell. The next attempt deliberately stops after
        # recovery; the original pair must already be usable at that point.
        interrupt_upgrade("backend")
        assert run_setup(2, "failed-retry-after-hard-interruption", "/FAILAFTERRECOVERY=1") != 0
        check_payload(1)
        assert not staged.exists()
        assert not staged_shell.exists()

        # A failed recovery must retain its durable journal even though this
        # new process has not begun a transaction of its own. A later retry can
        # finish recovery after the sharing violation is released.
        interrupt_upgrade("shell")
        with shell.open("rb"):
            assert run_setup(2, "locked-recovery", "/NOCLOSEAPPLICATIONS") != 0
            assert transaction.is_file()
            assert backup_shell.read_text(encoding="utf-8") == "1"
        assert run_setup(2, "retry-locked-recovery", "/FAILAFTERRECOVERY=1") != 0
        check_payload(1)

        transaction.write_bytes(b"unreadable transaction")
        assert run_setup(2, "invalid-recovery-record") != 0
        assert transaction.read_bytes() == b"unreadable transaction"
        transaction.unlink()
        check_payload(1)

        # A real Windows sharing violation during shell publication must roll
        # back the already-activated backend. Never let Restart Manager close
        # the test runner that deliberately holds this file open.
        with shell.open("rb"):
            assert run_setup(2, "locked-shell-upgrade", "/NOCLOSEAPPLICATIONS") != 0
        check_payload(1)

        for failure in ("FAILAFTERBACKUP", "FAILAFTERACTIVATE", "FAILAFTERSHELLBACKUP", "FAILAFTERSHELLACTIVATE"):
            assert run_setup(2, f"failed-upgrade-{failure}", f"/{failure}=1") != 0
            check_payload(1)

        assert run_setup(2, "upgrade") == 0
        check_payload(2)
        assert not staged.exists()
        assert not staged_shell.exists()

        backend.rename(backup)
        shell.rename(backup_shell)
        staged.mkdir()
        (staged / "interrupted.txt").write_text("partial extraction", encoding="utf-8")
        staged_shell.write_text("partial shell", encoding="utf-8")
        assert run_setup(2, "recover-interrupted") == 0
        check_payload(2)
        assert not staged.exists()

        # An unowned file inside the installation is also retained on uninstall.
        sentinel = app_dir / "unowned-user-file.txt"
        sentinel.write_bytes(b"preserved user fixture")
        retained.append(sentinel)
        assert _run_installer(installed_uninstaller(), tmp_path / "uninstall.log") == 0
        assert not backend.exists()
        assert not staged.exists()
        assert not backup.exists()
        assert not shell.exists()
        assert not staged_shell.exists()
        assert not backup_shell.exists()
        with pytest.raises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_key)

        assert run_setup(1, "failed-fresh-install", "/FAILAFTERSHELLACTIVATE=1") != 0
        assert not backend.exists()
        assert not shell.exists()
        assert run_setup(1, "reinstall") == 0
        check_payload(1)
        for path in retained:
            assert path.read_bytes() == b"preserved user fixture"
    finally:
        uninstaller = installed_uninstaller()
        if uninstaller is not None and uninstaller.is_file():
            assert _run_installer(uninstaller, tmp_path / "cleanup-uninstall.log") == 0
