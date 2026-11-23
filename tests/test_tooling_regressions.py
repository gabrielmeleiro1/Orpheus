"""Regression tests for build, setup, qualification and CI tooling."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_desktop_backend
from scripts import release_qualification as cli
from scripts import setup_runtime
from tools.release_qualification import artifact, checklist, preflight
from tools.release_qualification.commands import CommandSpec, run_command
from tools.release_qualification.model import PhaseState

REPO_ROOT = Path(__file__).resolve().parents[1]
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")


def _record_popen(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen]:
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(setup_runtime.subprocess, "Popen", recording_popen)
    return processes


def _reap(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


# --- setup_runtime stage supervision ---------------------------------------


@posix_only
def test_supervised_stage_timeout_kills_child_without_signalling_shared_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(setup_runtime.TAURI_SUPERVISED_ENV, "1")
    processes = _record_popen(monkeypatch)
    group_signals: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def recording_killpg(pgid: int, sig: int) -> None:
        group_signals.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(setup_runtime.os, "killpg", recording_killpg)
    try:
        with pytest.raises(setup_runtime.SetupExecutionError, match="timed out"):
            setup_runtime._supervise_command(SLEEPER, stage="stage", timeout_seconds=0.2)
        assert processes
        assert processes[0].poll() is not None, "timed-out supervised stage child is still running"
        assert group_signals == []
    finally:
        _reap(processes)


@posix_only
def test_standalone_stage_timeout_still_signals_its_own_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(setup_runtime.TAURI_SUPERVISED_ENV, raising=False)
    processes = _record_popen(monkeypatch)
    group_signals: list[int] = []
    real_killpg = os.killpg

    def recording_killpg(pgid: int, sig: int) -> None:
        group_signals.append(pgid)
        real_killpg(pgid, sig)

    monkeypatch.setattr(setup_runtime.os, "killpg", recording_killpg)
    try:
        with pytest.raises(setup_runtime.SetupExecutionError, match="timed out"):
            setup_runtime._supervise_command(SLEEPER, stage="stage", timeout_seconds=0.2)
        assert processes[0].poll() is not None
        assert group_signals == [processes[0].pid]
    finally:
        _reap(processes)


def test_interrupted_stage_wait_terminates_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(setup_runtime.TAURI_SUPERVISED_ENV, raising=False)
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen] = []

    def interrupting_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        real_wait = process.wait
        calls = {"count": 0}

        def wait(timeout=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise KeyboardInterrupt
            return real_wait(timeout=timeout)

        process.wait = wait  # type: ignore[method-assign]
        return process

    monkeypatch.setattr(setup_runtime.subprocess, "Popen", interrupting_popen)
    try:
        with pytest.raises(KeyboardInterrupt):
            setup_runtime._supervise_command(SLEEPER, stage="stage", timeout_seconds=30)
        assert processes and processes[0].poll() is not None
    finally:
        _reap(processes)


def test_completed_stage_child_is_not_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(setup_runtime.TAURI_SUPERVISED_ENV, raising=False)
    monkeypatch.setattr(
        setup_runtime.os,
        "killpg",
        lambda *_args: pytest.fail("a finished stage must not be signalled"),
        raising=False,  # absent on Windows
    )
    setup_runtime._supervise_command(
        [sys.executable, "-c", "pass"], stage="stage", timeout_seconds=30
    )


# --- qualification command runner -------------------------------------------


@posix_only
def test_command_timeout_applies_to_a_command_that_never_stops_writing(tmp_path: Path) -> None:
    spec = CommandSpec(
        label="chatty",
        argv=(
            sys.executable,
            "-c",
            # Output is always pending; the child gives up on its own after 20 s.
            "import sys, time\nend = time.monotonic() + 20\n"
            "while time.monotonic() < end:\n    sys.stdout.write('x' * 4096 + '\\n'); sys.stdout.flush()",
        ),
        cwd=str(tmp_path),
        timeout_seconds=1.0,
    )
    started = time.monotonic()
    result = run_command(spec, log_dir=tmp_path / "logs", env_allowlist=("PATH",))

    assert result.timed_out is True
    assert result.exit_code != 0
    assert time.monotonic() - started < 15
    assert "xxxx" in (tmp_path / "logs" / "chatty.log").read_text(encoding="utf-8")


@posix_only
def test_command_output_written_before_exit_is_still_drained(tmp_path: Path) -> None:
    spec = CommandSpec(
        label="burst",
        argv=(
            sys.executable,
            "-c",
            "import sys\nsys.stdout.write('y' * 300000 + '\\nEND\\n')",
        ),
        cwd=str(tmp_path),
        timeout_seconds=30.0,
    )
    result = run_command(spec, log_dir=tmp_path / "logs", env_allowlist=("PATH",))

    assert (result.timed_out, result.exit_code) == (False, 0)
    assert (tmp_path / "logs" / "burst.log").read_text(encoding="utf-8").rstrip().endswith("END")


# --- desktop Python launcher --------------------------------------------------


NODE = shutil.which("node")
LAUNCHER = REPO_ROOT / "desktop" / "scripts" / "run-python.mjs"
needs_node = pytest.mark.skipif(NODE is None or os.name != "posix", reason="needs node on POSIX")


def _launcher_checkout(tmp_path: Path) -> Path:
    scripts_dir = tmp_path / "desktop" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(LAUNCHER, scripts_dir / LAUNCHER.name)
    return scripts_dir / LAUNCHER.name


def _fake_python(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{label} $*"\nexit 7\n', encoding="utf-8")
    path.chmod(0o755)


@needs_node
def test_launcher_prefers_the_checkout_venv_and_forwards_args_and_exit_code(tmp_path: Path) -> None:
    launcher = _launcher_checkout(tmp_path)
    _fake_python(tmp_path / ".venv" / "bin" / "python", "venv")
    path_dir = tmp_path / "path-bin"
    _fake_python(path_dir / "python3", "path")

    completed = subprocess.run(
        [NODE, str(launcher), "../scripts/build.py", "--flag"],
        capture_output=True,
        text=True,
        env={"PATH": f"{path_dir}{os.pathsep}/usr/bin:/bin"},
        timeout=30,
    )

    assert completed.returncode == 7
    assert completed.stdout.strip() == "venv ../scripts/build.py --flag"


@needs_node
def test_launcher_falls_back_to_python3_on_path_without_a_venv(tmp_path: Path) -> None:
    launcher = _launcher_checkout(tmp_path)
    path_dir = tmp_path / "path-bin"
    _fake_python(path_dir / "python3", "python3")
    _fake_python(path_dir / "python", "python")

    completed = subprocess.run(
        [NODE, str(launcher), "script.py"],
        capture_output=True,
        text=True,
        env={"PATH": str(path_dir)},
        timeout=30,
    )

    assert completed.returncode == 7
    assert completed.stdout.strip() == "python3 script.py"


@needs_node
def test_launcher_falls_back_to_python_when_python3_is_missing(tmp_path: Path) -> None:
    launcher = _launcher_checkout(tmp_path)
    path_dir = tmp_path / "path-bin"
    _fake_python(path_dir / "python", "python")

    completed = subprocess.run(
        [NODE, str(launcher), "script.py"],
        capture_output=True,
        text=True,
        env={"PATH": str(path_dir)},
        timeout=30,
    )

    assert completed.returncode == 7
    assert completed.stdout.strip() == "python script.py"


def test_build_and_install_commands_never_call_a_bare_python() -> None:
    tauri = json.loads(
        (REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    scripts = json.loads((REPO_ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))[
        "scripts"
    ]
    commands = [tauri["build"]["beforeBuildCommand"], *scripts.values()]
    for command in commands:
        for step in command.split("&&"):
            assert step.split()[0] not in {"python", "python3"}, command
    launcher = "node scripts/run-python.mjs"
    assert tauri["build"]["beforeBuildCommand"].startswith(
        f"{launcher} ../scripts/build_desktop_backend.py"
    )
    assert scripts["desktop:install"].startswith(f"{launcher} ../scripts/install_desktop_app.py")
    assert f"{launcher} ../scripts/install_desktop_app.py" in scripts["desktop:release"]


# --- report symlink checks ---------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256sums_report(tmp_path: Path) -> tuple[Path, Path, Path]:
    report = tmp_path / "report"
    report.mkdir()
    (report / "prepare.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    (report / "artifacts.json").write_text(
        json.dumps({"dmg": "build/Orpheus_1.0.0.dmg"}), encoding="utf-8"
    )
    dmg = report / "Orpheus_1.0.0.dmg"
    dmg.write_bytes(b"dmg")
    real_md = report / "notes.md"
    real_md.write_text("report", encoding="utf-8")
    return report, dmg, real_md


def test_sha256sums_accepts_regular_files(tmp_path: Path) -> None:
    report, dmg, real_md = _sha256sums_report(tmp_path)
    md = report / "release-qualification-1.0.0.md"
    shutil.copy2(real_md, md)
    sums = report / "SHA256SUMS"
    sums.write_text(f"{_sha(dmg)}  {dmg.name}\n{_sha(md)}  {md.name}\n", encoding="utf-8")

    cli.verify_sha256sums(sums, report_dir=report)


@posix_only
def test_sha256sums_rejects_an_intra_report_symlink(tmp_path: Path) -> None:
    report, dmg, real_md = _sha256sums_report(tmp_path)
    md = report / "release-qualification-1.0.0.md"
    md.symlink_to(real_md.name)
    sums = report / "SHA256SUMS"
    sums.write_text(f"{_sha(dmg)}  {dmg.name}\n{_sha(real_md)}  {md.name}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA256SUMS target missing"):
        cli.verify_sha256sums(sums, report_dir=report)


@posix_only
@pytest.mark.parametrize("kind", ["evidence", "metrics"])
def test_checklist_revalidation_rejects_an_intra_report_symlink(tmp_path: Path, kind: str) -> None:
    report = tmp_path / "report"
    (report / "files").mkdir(parents=True)
    real = report / "files" / "real.txt"
    real.write_text("proof", encoding="utf-8")
    link = report / "files" / "linked.txt"
    link.symlink_to(real.name)
    item = {f"{kind}_path": "files/linked.txt", f"{kind}_sha256": _sha(real)}

    with pytest.raises(ValueError, match="not a regular file"):
        checklist._revalidate_passed_item(report, "check", item, frozenset({kind}))

    item[f"{kind}_path"] = "files/real.txt"
    checklist._revalidate_passed_item(report, "check", item, frozenset({kind}))


# --- preflight Node floor ----------------------------------------------------


def test_preflight_node_floor_matches_the_desktop_engine_and_pin() -> None:
    package = json.loads((REPO_ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    engine = package["engines"]["node"]
    pinned = (REPO_ROOT / ".node-version").read_text(encoding="utf-8").strip()

    assert engine == f">={preflight.MINIMUM_NODE_MAJOR}"
    assert int(pinned.split(".")[0]) == preflight.MINIMUM_NODE_MAJOR


# --- Mach-O rpath parsing ----------------------------------------------------

OTOOL_LOAD_COMMANDS = """\
Load command 12
          cmd LC_LOAD_DYLIB
      cmdsize 56
         name /usr/lib/libSystem.B.dylib (offset 24)
Load command 13
          cmd LC_RPATH
      cmdsize 48
         path /Applications/My App.app/Contents/Frameworks (offset 12)
Load command 14
          cmd LC_DYLD_ENVIRONMENT
      cmdsize 32
         path DYLD_FALLBACK_LIBRARY_PATH=/opt/x (offset 12)
Load command 15
          cmd LC_RPATH
      cmdsize 32
         path @loader_path/../lib (offset 12)
"""


def test_otool_rpath_parsing_keeps_spaces_and_only_reads_lc_rpath() -> None:
    assert build_desktop_backend._parse_otool_rpaths(OTOOL_LOAD_COMMANDS) == [
        "/Applications/My App.app/Contents/Frameworks",
        "@loader_path/../lib",
    ]


def test_macho_rpaths_reads_otool_load_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout=OTOOL_LOAD_COMMANDS, stderr="")

    monkeypatch.setattr(build_desktop_backend.subprocess, "run", fake_run)

    assert build_desktop_backend._macho_rpaths(Path("/bin/x"))[0].endswith("My App.app/Contents/Frameworks")
    assert calls == [["otool", "-l", str(Path("/bin/x"))]]


# --- Mach-O relocation batching ---------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="Mach-O relocation is macOS-only")
def test_relocation_runs_one_install_name_tool_per_binary(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    roots = [bin_dir / "ffmpeg", bin_dir / "ffprobe"]
    for root in roots:
        root.write_bytes(b"exe")
    cellar = tmp_path / "cellar"
    cellar.mkdir()
    libs = [cellar / f"libav{index}.dylib" for index in range(4)]
    for lib in libs:
        lib.write_bytes(lib.name.encode())
    graph = {root.resolve(): tuple(str(lib) for lib in libs) for root in roots}
    for index, lib in enumerate(libs):
        graph[lib.resolve()] = tuple(str(other) for other in libs[index + 1 :])
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        assert kwargs["check"] is True
        commands.append([str(part) for part in command])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    lib_dir = bin_dir / "lib"
    build_desktop_backend.package_macho_dependencies_relocatable(
        roots,
        lib_dir=lib_dir,
        host_system="Darwin",
        read_dependencies=lambda path: graph[Path(path).resolve()],
        run_tool=fake_run,
    )

    rewrites = [command for command in commands if command[0] == "install_name_tool"]
    targets = [command[-1] for command in rewrites]
    copies = [str(lib_dir / lib.name) for lib in libs]
    assert sorted(targets) == sorted([*map(str, roots), *copies])
    for command in rewrites:
        changes = {
            command[index + 1]: command[index + 2]
            for index in range(1, len(command) - 1)
            if command[index] == "-change"
        }
        prefix = "@loader_path/lib/" if command[-1] in map(str, roots) else "@loader_path/"
        assert changes == {str(lib): f"{prefix}{lib.name}" for lib in libs}
        if command[-1] in copies:
            assert command[1:3] == ["-id", f"@loader_path/{Path(command[-1]).name}"]
        else:
            assert "-id" not in command
    # Every binary is re-signed after its rewrite.
    for target in targets:
        rewrite_at = commands.index(next(c for c in rewrites if c[-1] == target))
        assert ["codesign", "--force", "--sign", "-", target] in commands[rewrite_at + 1 :]


# --- required-module probe ---------------------------------------------------


def test_module_probe_uses_one_interpreter_when_everything_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_desktop_backend.subprocess, "run", fake_run)

    assert build_desktop_backend._missing_required_modules("py", ["json", "sqlite3", "csv"]) == []
    assert len(calls) == 1
    assert calls[0][-3:] == ["json", "sqlite3", "csv"]


def test_module_probe_names_each_missing_module_with_its_error() -> None:
    missing = build_desktop_backend._missing_required_modules(
        sys.executable, ["json", "orpheus_missing_mod_a", "csv", "orpheus_missing_mod_b"]
    )

    assert [entry.split(":", 1)[0] for entry in missing] == [
        "orpheus_missing_mod_a",
        "orpheus_missing_mod_b",
    ]
    assert "No module named 'orpheus_missing_mod_a'" in missing[0]
    assert "No module named 'orpheus_missing_mod_b'" in missing[1]


# --- Mach-O classification prefilter ---------------------------------------


@pytest.mark.parametrize(
    "magic",
    ["feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca"],
)
def test_macho_magic_headers_are_still_classified_by_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, magic: str
) -> None:
    candidate = tmp_path / "binary"
    candidate.write_bytes(bytes.fromhex(magic) + b"\0" * 28)
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="Mach-O 64-bit executable arm64", stderr="")

    monkeypatch.setattr(artifact.subprocess, "run", fake_run)

    assert artifact.default_classify_macho(candidate) is True
    assert calls == [["file", "-b", str(candidate)]]


@pytest.mark.parametrize("payload", [b"", b"#!", b"print('x')\n", b"\x61\x0d\x0d\x0a" + b"\0" * 12])
def test_non_macho_files_skip_the_file_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    candidate = tmp_path / "data"
    candidate.write_bytes(payload)
    monkeypatch.setattr(
        artifact.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("file must not run for a non-Mach-O header"),
    )

    assert artifact.default_classify_macho(candidate) is False


@pytest.mark.skipif(shutil.which("file") is None, reason="needs file(1)")
def test_macho_prefilter_agrees_with_file_on_real_binaries(tmp_path: Path) -> None:
    text = tmp_path / "script.py"
    text.write_text("print('x')\n", encoding="utf-8")
    java = tmp_path / "Example.class"
    java.write_bytes(bytes.fromhex("cafebabe") + b"\x00\x00\x00\x34" + b"\0" * 24)
    for path in (Path(sys.executable).resolve(), text, java):
        probe = subprocess.run(["file", "-b", str(path)], capture_output=True, text=True, check=True)
        assert artifact.default_classify_macho(path) is ("mach-o" in probe.stdout.lower())


# --- Windows installer -------------------------------------------------------


def test_installer_deinitialize_contains_rollback_failures() -> None:
    text = (REPO_ROOT / "desktop" / "windows-installer.iss").read_text(encoding="utf-8")
    body = text.split("procedure DeinitializeSetup;", 1)[1].split("\nprocedure ", 1)[0]
    restore_at = body.index("RestoreInstallation;")
    assert body.rindex("try", 0, restore_at) < restore_at < body.index("except", restore_at)
    assert "Log(" in body[body.index("except", restore_at) :]


# --- CI ----------------------------------------------------------------------


@pytest.mark.parametrize("workflow", ["source-validation.yml", "release-artifact-validation.yml"])
def test_workflows_cache_the_tauri_cargo_build(workflow: str) -> None:
    text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    cache_at = text.index("uses: Swatinem/rust-cache@v2")
    assert "workspaces: desktop/src-tauri" in text[cache_at : cache_at + 200]
    assert text.index("rustup default") < cache_at < text.index("cargo test")


# --- One shipped build per qualification run -------------------------------------


def _qualification_runner(report_dir: Path, app: Path, dmg: Path, calls: list[str]):
    from tools.release_qualification import runtime

    def run(spec, **_kwargs):
        calls.append(spec.label)
        log = report_dir / "commands" / f"{spec.label}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({
            "app": str(app), "dmg": str(dmg),
            "dmg_sha256": hashlib.sha256(dmg.read_bytes()).hexdigest(),
            "app_tree_sha256": runtime.app_tree_digest(app),
        }) + "\n")
        return SimpleNamespace(exit_code=0, label=spec.label, log_path=str(log))

    return run


@pytest.fixture
def qualification_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    report = tmp_path / "report"
    (report / "config").mkdir(parents=True)
    (report / "config/release_qualification.toml").write_bytes(cli.DEFAULT_CONFIG.read_bytes())
    (report / "report.json").write_text(json.dumps({"release_id": "1.0.0"}))
    app = tmp_path / "Orpheus.app"
    app.mkdir()
    (app / "executable").write_bytes(b"app")
    dmg = tmp_path / "Orpheus.dmg"
    dmg.write_bytes(b"image")
    monkeypatch.setattr(cli, "_locate_app_bundle", lambda: app)
    monkeypatch.setattr(cli, "_locate_dmg", lambda _version: dmg)
    monkeypatch.setattr(cli, "inspect_app_bundle", lambda *a, **k: None)
    monkeypatch.setattr(cli, "inspect_dmg", lambda *a, **k: None)
    return report, app, dmg


def test_qualification_validates_the_sidecar_built_by_the_shipped_tauri_build(
    qualification_workspace,
) -> None:
    report, app, dmg = qualification_workspace
    calls: list[str] = []
    handlers = cli.build_phase_handlers(
        report, run_command_fn=_qualification_runner(report, app, dmg, calls)
    )

    assert handlers["sidecar_validation"]("sidecar_validation", report).state is PhaseState.PASSED
    assert handlers["bundle_validation"]("bundle_validation", report).state is PhaseState.PASSED

    # Exactly one build, and every validation of the sidecar and bundle follows it.
    assert calls == [
        "tauri-desktop-build", "validate-sidecar", "sidecar-runtime-smoke", "bundled-runtime-smoke",
    ]
    assert json.loads((report / "artifacts.json").read_text())["dmg_sha256"]


def test_resumed_bundle_validation_rebuilds_before_qualifying(qualification_workspace) -> None:
    """A run resumed at bundle_validation must not certify an app it did not build."""
    report, app, dmg = qualification_workspace
    calls: list[str] = []
    handlers = cli.build_phase_handlers(
        report, run_command_fn=_qualification_runner(report, app, dmg, calls)
    )

    assert handlers["bundle_validation"]("bundle_validation", report).state is PhaseState.PASSED
    assert calls == [
        "tauri-desktop-build", "validate-sidecar", "sidecar-runtime-smoke", "bundled-runtime-smoke",
    ]


def test_failed_sidecar_build_blocks_the_bundle_phase_from_reusing_it(
    qualification_workspace,
) -> None:
    report, app, dmg = qualification_workspace
    calls: list[str] = []
    runner = _qualification_runner(report, app, dmg, calls)

    def failing_smoke(spec, **kwargs):
        result = runner(spec, **kwargs)
        if spec.label == "sidecar-runtime-smoke" and calls.count(spec.label) == 1:
            return SimpleNamespace(exit_code=1, label=spec.label, log_path=result.log_path)
        return result

    handlers = cli.build_phase_handlers(report, run_command_fn=failing_smoke)
    side = handlers["sidecar_validation"]("sidecar_validation", report)
    assert side.state is PhaseState.FAILED
    assert handlers["bundle_validation"]("bundle_validation", report).state is PhaseState.PASSED
    assert calls.count("tauri-desktop-build") == 2


def test_shipped_build_is_pinned_to_the_qualified_runtime_profile() -> None:
    build, validate, smoke = artifact.build_sidecar_commands(repo_root=REPO_ROOT)
    assert build.argv == (
        "/usr/bin/env", "ORPHEUS_RUNTIME_PROFILE=mlx", "npm", "run", "desktop:build",
    )
    assert build.cwd == str(REPO_ROOT / "desktop")
    assert validate.argv[1:] == (
        "scripts/build_desktop_backend.py", "--validate-existing", "--runtime-profile", "mlx",
    )
    assert smoke.label == "sidecar-runtime-smoke"
    assert [spec.label for spec in artifact.build_bundle_commands(repo_root=REPO_ROOT)] == [
        "bundled-runtime-smoke",
    ]
    # The env prefix must survive the runner's environment allowlist.
    tauri_conf = json.loads((REPO_ROOT / "desktop/src-tauri/tauri.conf.json").read_text())
    assert "build_desktop_backend.py" in tauri_conf["build"]["beforeBuildCommand"]
    assert "--runtime-profile" not in tauri_conf["build"]["beforeBuildCommand"]


# --- One tree walk per qualification checkpoint ----------------------------------

_SIDECAR = Path("Contents/Resources/binaries/orpheus-desktop-backend")


def _make_bundle(root: Path, *, backend: bytes = b"backend") -> Path:
    app = root / "Orpheus.app"
    sidecar = app / _SIDECAR
    (sidecar / "bin").mkdir(parents=True)
    (sidecar / "orpheus-desktop-backend").write_bytes(backend)
    (sidecar / "orpheus-desktop-backend").chmod(0o755)
    (sidecar / "bin" / "ffmpeg").write_bytes(b"ffmpeg")
    (sidecar / "_internal").mkdir()
    (sidecar / "_internal" / "lib.dylib").write_bytes(b"lib")
    (sidecar / "link").symlink_to("bin/ffmpeg")
    (sidecar / "linked-dir").symlink_to("_internal", target_is_directory=True)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "Orpheus").write_bytes(b"shell")
    (app / "Contents" / "Info.plist").write_bytes(b"plist")
    return app


@pytest.mark.parametrize(
    "variant",
    ["plain", "empty-sidecar", "no-sidecar", "symlinked-sidecar", "executable-symlink",
     "escape-within-app", "escape-outside-app"],
)
def test_combined_walk_matches_the_separate_digests(tmp_path: Path, variant: str) -> None:
    from tools.release_qualification import runtime

    app = _make_bundle(tmp_path)
    sidecar = app / _SIDECAR
    if variant == "empty-sidecar":
        shutil.rmtree(sidecar)
        sidecar.mkdir()
    elif variant == "no-sidecar":
        shutil.rmtree(sidecar)
    elif variant == "symlinked-sidecar":
        real = app / "Contents" / "Resources" / "real-backend"
        sidecar.rename(real)
        sidecar.symlink_to(real, target_is_directory=True)
    elif variant == "executable-symlink":
        (sidecar / "orpheus-desktop-backend").unlink()
        (sidecar / "orpheus-desktop-backend").symlink_to("bin/ffmpeg")
    elif variant == "escape-within-app":
        # Inside the app but outside the sidecar: only the sidecar check rejects it.
        (sidecar / "escape").symlink_to(app / "Contents" / "Info.plist")
    elif variant == "escape-outside-app":
        outside = tmp_path / "outside"
        outside.write_bytes(b"x")
        (app / "Contents" / "escape").symlink_to(outside)
        (sidecar / "escape").symlink_to(outside)

    def separate():
        app_digest = runtime.app_tree_digest(app)
        sidecar_digest = runtime.sidecar_tree_digest(sidecar)
        return app_digest, sidecar_digest

    try:
        expected = separate()
    except runtime.ArtifactInspectionError as exc:
        with pytest.raises(runtime.ArtifactInspectionError) as raised:
            runtime.app_and_sidecar_digests(app)
        assert str(raised.value) == str(exc)
        return
    app_digest, sidecar_digest, executable_digest = runtime.app_and_sidecar_digests(app)
    assert (app_digest, sidecar_digest) == expected
    executable = sidecar / "orpheus-desktop-backend"
    if executable_digest is not None:
        assert executable_digest == artifact.sha256_file(executable)
    else:
        assert variant in {"no-sidecar", "empty-sidecar", "symlinked-sidecar", "executable-symlink"}


def _run_qualify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, smoke=None):
    from tools.release_qualification import runtime

    app = _make_bundle(tmp_path / "built")
    mounted = tmp_path / "mounted"
    _make_bundle(mounted)
    dmg = tmp_path / "Orpheus_1.0.0_aarch64.dmg"
    dmg.write_bytes(b"image")
    walks: list[Path] = []
    real_rglob = Path.rglob

    def counting_rglob(self, pattern, *args, **kwargs):
        walks.append(self)
        return real_rglob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", counting_rglob)
    dmg_hashes: list[Path] = []
    real_sha = runtime.sha256_file

    def counting_sha(path, *args, **kwargs):
        if Path(path) == dmg:
            dmg_hashes.append(Path(path))
        return real_sha(path, *args, **kwargs)

    monkeypatch.setattr(runtime, "sha256_file", counting_sha)
    smoke_walks: list[int] = []

    def run(argv, **_kwargs):
        smoke_walks.append(len(walks))
        if smoke is not None:
            smoke(app, mounted / "Orpheus.app", dmg)

    result = runtime.qualify_bundled_runtime(
        app=app, dmg=dmg, repo_root=tmp_path, run=run,
        mount_dmg=lambda *_: mounted, detach_dmg=lambda _mount: None,
    )
    return result, walks, smoke_walks, dmg_hashes, app, mounted / "Orpheus.app"


def test_each_qualification_checkpoint_walks_each_tree_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, walks, smoke_walks, dmg_hashes, app, shipped = _run_qualify(tmp_path, monkeypatch)

    # Before the smoke: the built app and the mounted app, one walk each.
    assert walks[: smoke_walks[0]] == [app, shipped]
    # After the smoke: both again, still one walk each.
    assert walks[smoke_walks[0]:] == [shipped, app]
    assert len(dmg_hashes) == 2
    assert result["sidecar_sha256"] == hashlib.sha256(b"backend").hexdigest()


@pytest.mark.parametrize(
    "target",
    ["shipped-sidecar", "shipped-outside-sidecar", "built-sidecar", "built-outside-sidecar", "dmg"],
)
def test_changes_between_checkpoints_are_still_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    from tools.release_qualification import runtime

    if target == "built-sidecar" and os.name != "posix":
        pytest.skip("this case changes POSIX permission bits")

    def tamper(app: Path, shipped: Path, dmg: Path) -> None:
        if target == "shipped-sidecar":
            (shipped / _SIDECAR / "_internal" / "lib.dylib").write_bytes(b"changed")
        elif target == "shipped-outside-sidecar":
            (shipped / "Contents" / "MacOS" / "Orpheus").write_bytes(b"changed")
        elif target == "built-sidecar":
            (app / _SIDECAR / "bin" / "ffmpeg").chmod(0o700)
        elif target == "built-outside-sidecar":
            (app / "Contents" / "Info.plist").write_bytes(b"changed")
        else:
            dmg.write_bytes(b"changed")

    with pytest.raises(runtime.ArtifactInspectionError, match="changed during qualification"):
        _run_qualify(tmp_path, monkeypatch, smoke=tamper)


# --- Smoke suite: one shared pytest process, failures re-run on their own --------


def _smoke_modules(monkeypatch: pytest.MonkeyPatch, modules: list[str]) -> None:
    import run_tests

    monkeypatch.setattr(run_tests, "SMOKE_TEST_MODULES", modules)


def test_smoke_suite_reports_each_module_from_one_shared_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import run_tests

    modules = ["tests/test_run_tests.py", "tests/test_markdown_parser.py"]
    _smoke_modules(monkeypatch, modules)
    batches: list[list[str]] = []
    monkeypatch.setattr(
        run_tests, "run_smoke_batch",
        lambda paths: (batches.append(list(paths)), {p: (True, 1.25) for p in paths})[1],
    )
    monkeypatch.setattr(run_tests, "run_test", lambda path: pytest.fail(f"re-ran {path}"))

    assert run_tests.run_smoke_suite() == 0
    out = capsys.readouterr().out
    assert batches == [modules]
    for module in modules:
        assert f"> {module}... PASS (1.2s)" in out
        assert f"PASS {Path(module).stem}: 1.2s" in out
    assert "SMOKE SUITE PASSED" in out


def test_smoke_module_failing_in_the_shared_run_is_rerun_alone_and_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import run_tests

    modules = ["tests/test_run_tests.py", "tests/test_markdown_parser.py"]
    _smoke_modules(monkeypatch, modules)
    monkeypatch.setattr(
        run_tests, "run_smoke_batch",
        lambda paths: {modules[0]: (True, 0.5), modules[1]: (False, 0.5)},
    )
    reruns: list[str] = []
    monkeypatch.setattr(
        run_tests, "run_test", lambda path: (reruns.append(path), (False, 2.0))[1]
    )

    assert run_tests.run_smoke_suite() == 1
    out = capsys.readouterr().out
    assert reruns == [modules[1]]
    assert "FAIL test_markdown_parser: 2.0s" in out
    assert "PASS test_run_tests: 0.5s" in out
    assert "SMOKE SUITE FAILED" in out


def test_shared_run_failure_no_module_reproduces_falls_back_to_isolated_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure pinned on the wrong module must not hide the module that fails alone."""
    import run_tests

    modules = ["tests/test_run_tests.py", "tests/test_markdown_parser.py"]
    _smoke_modules(monkeypatch, modules)
    monkeypatch.setattr(
        run_tests, "run_smoke_batch",
        lambda paths: {modules[0]: (True, 0.5), modules[1]: (False, 0.5)},
    )
    reruns: list[str] = []

    def run_alone(path: str) -> tuple[bool, float]:
        reruns.append(path)
        return (path != modules[0], 1.0)

    monkeypatch.setattr(run_tests, "run_test", run_alone)

    assert run_tests.run_smoke_suite() == 1
    assert reruns == [modules[1], modules[0], modules[1]]


@pytest.mark.parametrize("batch", [None, {}])
def test_unattributable_shared_run_runs_every_module_alone(
    monkeypatch: pytest.MonkeyPatch, batch,
) -> None:
    import run_tests

    modules = ["tests/test_run_tests.py", "tests/test_markdown_parser.py"]
    _smoke_modules(monkeypatch, modules)
    monkeypatch.setattr(run_tests, "run_smoke_batch", lambda paths: batch)
    reruns: list[str] = []
    monkeypatch.setattr(run_tests, "run_test", lambda p: (reruns.append(p), (True, 0.1))[1])

    assert run_tests.run_smoke_suite() == 0
    assert reruns == modules


def test_isolated_flag_keeps_one_process_per_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import run_tests

    modules = ["tests/test_run_tests.py", "tests/test_markdown_parser.py"]
    _smoke_modules(monkeypatch, modules)
    monkeypatch.setattr(
        run_tests, "run_smoke_batch", lambda paths: pytest.fail("batched an isolated run")
    )
    reruns: list[str] = []
    monkeypatch.setattr(run_tests, "run_test", lambda p: (reruns.append(p), (True, 0.1))[1])
    seen: list[bool] = []
    real_suite = run_tests.run_smoke_suite
    monkeypatch.setattr(
        run_tests, "run_smoke_suite",
        lambda *, isolated=False: (seen.append(isolated), real_suite(isolated=isolated))[1],
    )

    assert run_tests.main(["--isolated"]) == 0
    assert seen == [True]
    assert reruns == modules
    with pytest.raises(SystemExit):
        run_tests.parse_args(["--isolated", "--contract"])


def _report(nodeid: str, *, failed: bool = False, duration: float = 0.0):
    return SimpleNamespace(nodeid=nodeid, failed=failed, duration=duration)


def test_smoke_result_recorder_attributes_outcomes_per_module(tmp_path: Path) -> None:
    import run_tests

    path = tmp_path / "results.json"
    recorder = run_tests._SmokeResultRecorder(path)
    recorder.pytest_collectreport(_report("tests/test_b.py", failed=True))
    for phase_duration in (0.1, 0.2, 0.3):
        recorder.pytest_runtest_logreport(_report("tests/test_a.py::test_x", duration=phase_duration))
    recorder.pytest_runtest_logreport(_report("tests/test_c.py::T::test_y[1]", failed=True))
    recorder.pytest_sessionfinish(1)

    payload = json.loads(path.read_text())
    assert payload["exitstatus"] == 1
    assert payload["modules"]["tests/test_a.py"]["failed"] is False
    assert payload["modules"]["tests/test_a.py"]["duration"] == pytest.approx(0.6)
    assert payload["modules"]["tests/test_b.py"] == {"failed": True, "duration": 0.0, "reports": 0}
    assert payload["modules"]["tests/test_c.py"]["failed"] is True


def _batch_payload(monkeypatch, *, returncode: int, modules: dict) -> None:
    import run_tests

    def fake_run(command, **kwargs):
        results = Path(command[command.index(run_tests.SMOKE_RESULTS_OPTION) + 1])
        results.write_text(json.dumps({"exitstatus": returncode, "modules": modules}))
        assert kwargs["env"][run_tests._SMOKE_BATCH_ENV] == "1"
        return SimpleNamespace(returncode=returncode)

    monkeypatch.delenv(run_tests._SMOKE_BATCH_ENV, raising=False)
    monkeypatch.setattr(run_tests.subprocess, "run", fake_run)


def test_smoke_batch_leaves_out_modules_without_results(monkeypatch: pytest.MonkeyPatch) -> None:
    import run_tests

    _batch_payload(monkeypatch, returncode=1, modules={
        "tests/a.py": {"failed": False, "duration": 1.0, "reports": 3},
        "tests/b.py": {"failed": True, "duration": 0.0, "reports": 0},
    })
    assert run_tests.run_smoke_batch(["tests/a.py", "tests/b.py", "tests/c.py"]) == {
        "tests/a.py": (True, 1.0),
    }


@pytest.mark.parametrize(
    ("returncode", "modules"),
    [
        (1, {"tests/a.py": {"failed": False, "duration": 1.0, "reports": 3}}),
        (0, {"tests/a.py": {"failed": True, "duration": 1.0, "reports": 3}}),
        (2, {"tests/a.py": {"failed": False, "duration": 1.0, "reports": 3}}),
    ],
)
def test_smoke_batch_refuses_results_it_cannot_attribute(
    monkeypatch: pytest.MonkeyPatch, returncode: int, modules: dict,
) -> None:
    import run_tests

    _batch_payload(monkeypatch, returncode=returncode, modules=modules)
    assert run_tests.run_smoke_batch(["tests/a.py"]) is None


def test_smoke_batch_without_results_file_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interpreter abort (for example a native import crash) writes no results."""
    import run_tests

    monkeypatch.delenv(run_tests._SMOKE_BATCH_ENV, raising=False)
    monkeypatch.setattr(
        run_tests.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=-6)
    )
    assert run_tests.run_smoke_batch(["tests/a.py"]) is None


def test_smoke_batch_does_not_nest(monkeypatch: pytest.MonkeyPatch) -> None:
    import run_tests

    monkeypatch.setenv(run_tests._SMOKE_BATCH_ENV, "1")
    monkeypatch.setattr(
        run_tests.subprocess, "run", lambda *a, **k: pytest.fail("started a nested batch")
    )
    assert run_tests.run_smoke_batch(["tests/a.py"]) is None


def test_smoke_batch_runs_real_modules_through_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    import run_tests

    monkeypatch.delenv(run_tests._SMOKE_BATCH_ENV, raising=False)
    modules = ["tests/test_markdown_parser.py", "tests/test_text_parser.py"]
    results = run_tests.run_smoke_batch(modules)
    assert results is not None
    assert set(results) == set(modules)
    assert all(passed for passed, _ in results.values())
