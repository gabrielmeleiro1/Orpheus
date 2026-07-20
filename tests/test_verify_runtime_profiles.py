"""Tests for the isolated runtime-profile release verifier."""

from __future__ import annotations

import base64
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import venv

import pytest

from scripts import verify_runtime_profiles as verifier


def _ok(stdout: str = "") -> verifier.CommandResult:
    return verifier.CommandResult(0, stdout, "", 0.01, False)


class FakeRunner:
    def __init__(
        self,
        audit: object | None = None,
        *,
        fail_at: str | None = None,
        inventory: list[str] | None = None,
        site_paths: list[Path] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.audit = {"dependencies": [], "fixes": []} if audit is None else audit
        self.fail_at = fail_at
        self.inventory = [] if inventory is None else inventory
        self.site_paths = site_paths

    def __call__(self, command, *, cwd, timeout, env):
        cmd = list(command)
        self.commands.append(cmd)
        self.environments.append(dict(env))
        joined = " ".join(cmd)
        if self.fail_at and self.fail_at in joined:
            return verifier.CommandResult(1, "", "safe failure", 0.02, False)
        if "site.getsitepackages" in joined:
            paths = self.site_paths or [Path(cwd) / "fake-site-packages"]
            return _ok(json.dumps([str(path) for path in paths]))
        if "pip_audit" in joined:
            return _ok(json.dumps(self.audit))
        if "importlib.metadata" in joined:
            return _ok(json.dumps(self.inventory))
        return _ok()


def test_base_runs_isolated_install_check_import_prohibition_and_audit(tmp_path) -> None:
    runner = FakeRunner()
    report = verifier.verify_profiles(["base"], runner=runner, work_dir=tmp_path, keep_env=True)

    result = report.profiles[0]
    assert result.status == "passed"
    assert [stage.name for stage in result.stages] == [
        "create_environment",
        "bootstrap_tooling",
        "install",
        "pip_check",
        "import_smoke",
        "prohibited_packages",
        "artifact_integrity",
        "audit",
    ]
    assert all(isinstance(command, list) for command in runner.commands)
    assert not any("shell=True" in " ".join(command) for command in runner.commands)
    assert any(
        "requirements/base.txt" in " ".join(command).replace("\\", "/")
        for command in runner.commands
    )
    isolated_python_commands = [command for command in runner.commands if "-I" in command]
    assert isolated_python_commands
    assert all(command[1:3] == ["-B", "-I"] for command in isolated_python_commands)
    assert any("pip_audit" in command for command in runner.commands for command in command)
    audit_command = next(command for command in runner.commands if "pip_audit" in command)
    assert audit_command[1:4] == [
        "-m",
        "pip_audit",
        "--path",
    ]
    assert audit_command[4].endswith("audit-metadata")
    assert audit_command[5:] == [
        "--format",
        "json",
        "--strict",
        "--progress-spinner",
        "off",
    ]
    assert all("PYTHONPATH" not in env for env in runner.environments)


def test_command_spawn_error_is_structured_and_cuda_preflight_skips(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("missing tool")

    monkeypatch.setattr(verifier.subprocess, "Popen", missing)
    result = verifier.run_command(["missing-tool"], cwd=verifier.REPO_ROOT, timeout=1, env={})
    assert result.returncode == 127
    assert not result.timed_out

    compatible = SimpleNamespace(is_compatible=lambda: True, incompatibility_reason=lambda: None)
    monkeypatch.setattr(verifier, "get_runtime_profile", lambda profile: compatible)
    available, reason = verifier.hardware_preflight("cuda")
    assert available is False
    assert "NVIDIA" in reason


def test_non_preflight_spawn_error_fails_a_stage_without_traceback(monkeypatch, tmp_path) -> None:
    def denied(*args, **kwargs):
        raise PermissionError("password=not-for-output")

    monkeypatch.setattr(verifier.subprocess, "Popen", denied)
    report = verifier.verify_profiles(["base"], work_dir=tmp_path)

    assert report.profiles[0].status == "failed"
    assert report.profiles[0].stages[0].name == "create_environment"
    assert "not-for-output" not in report.profiles[0].reason
    assert "[REDACTED]" in report.profiles[0].reason


def test_command_failure_and_timeout_fail_the_profile(tmp_path) -> None:
    failed = verifier.verify_profiles(
        ["base"], runner=FakeRunner(fail_at="pip check"), work_dir=tmp_path
    )
    assert failed.profiles[0].status == "failed"

    def timeout_runner(command, *, cwd, timeout, env):
        return verifier.CommandResult(-1, "", "", timeout, True)

    timed_out = verifier.verify_profiles(
        ["base"], runner=timeout_runner, work_dir=tmp_path / "timeout"
    )
    assert timed_out.profiles[0].status == "failed"
    assert "timed out" in timed_out.profiles[0].stages[0].summary


def test_unavailable_profile_is_skipped_and_causes_exit_two(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(verifier, "hardware_preflight", lambda profile, **_: (False, "no GPU"))
    report = verifier.verify_profiles(["cuda"], runner=FakeRunner(), work_dir=tmp_path)

    assert report.profiles[0].status == "skipped"
    assert report.exit_code(allow_skipped=False) == 2
    assert report.exit_code(allow_skipped=True) == 0


def test_cuda_has_no_default_audit_exception_after_pytorch_upgrade(tmp_path) -> None:
    audit = [
        {
            "name": "torch",
            "version": "2.10.0",
            "vulns": [{"id": "PYSEC-2026-139", "aliases": ["CVE-2026-4538"], "fix_versions": []}],
        }
    ]
    exceptions = verifier.load_audit_exceptions(
        verifier.DEFAULT_EXCEPTIONS_FILE, today=date(2026, 7, 15)
    )
    result = verifier.evaluate_audit("cuda", audit, exceptions)

    assert result.status == "failed"
    assert "unexpected vulnerabilities" in result.summary.lower()


def test_exception_stops_accepting_an_advisory_when_a_fix_exists() -> None:
    exception = verifier.AuditException(
        "CVE-2025-3000",
        (),
        "torch",
        "cuda",
        "maintainers",
        "temporary",
        date(2026, 7, 14),
        date(2026, 10, 14),
    )
    audit = [
        {
            "name": "torch",
            "version": "2.10.0",
            "vulns": [{"id": "CVE-2025-3000", "aliases": [], "fix_versions": ["2.11.0"]}],
        }
    ]

    result = verifier.evaluate_audit("cuda", audit, (exception,))

    assert result.status == "failed"
    assert "fixed in 2.11.0" in result.summary


def test_audit_fails_when_pip_audit_skips_or_cannot_collect_a_distribution() -> None:
    skipped = {
        "dependencies": [
            {
                "name": "local-package",
                "version": "1.0",
                "vulns": [],
                "skip_reason": "Dependency is not on PyPI and could not be audited",
            }
        ],
        "fixes": [],
    }
    assert verifier.evaluate_audit("base", skipped, ()).status == "failed"

    collection_error = {
        "dependencies": [],
        "fixes": [],
        "errors": [{"message": "collection failed"}],
    }
    assert verifier.evaluate_audit("base", collection_error, ()).status == "failed"

    direct_url = {
        "dependencies": [
            {
                "name": "example-package",
                "version": "1.0",
                "vulns": [],
                "direct_url": "https://example.invalid/archive.whl",
            }
        ],
        "fixes": [],
    }
    assert verifier.evaluate_audit("base", direct_url, ()).status == "passed"


def test_expired_unexpected_and_cross_profile_exceptions_fail(tmp_path) -> None:
    raw = {
        "schema_version": 1,
        "exceptions": [
            {
                "advisory_id": "PYSEC-2026-139",
                "aliases": ["CVE-2026-4538"],
                "package": "torch",
                "profile": "cuda",
                "owner": "maintainers",
                "reason": "temporary",
                "created": "2026-01-01",
                "expires": "2026-01-02",
            }
        ],
    }
    path = tmp_path / "expired.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(verifier.ExceptionFileError, match="expired"):
        verifier.load_audit_exceptions(path, today=date(2026, 7, 15))

    vulnerability = [{"name": "torch", "version": "1", "vulns": [{"id": "CVE-1"}]}]
    assert verifier.evaluate_audit("cuda", vulnerability, ()).status == "failed"
    exception = verifier.AuditException(
        "CVE-1",
        (),
        "torch",
        "cuda",
        "maintainers",
        "temporary",
        date(2026, 1, 1),
        date(2026, 10, 1),
    )
    assert verifier.evaluate_audit("base", vulnerability, (exception,)).status == "failed"


def test_prohibited_package_sets_are_profile_specific() -> None:
    assert verifier.PROHIBITED_PACKAGES["base"] == {
        "accelerate",
        "bitsandbytes",
        "espeakng-loader",
        "kokoro",
        "language-tags",
        "misaki",
        "mlx",
        "mlx-audio",
        "mlx-lm",
        "num2words",
        "phonemizer-fork",
        "spacy",
        "thinc",
        "torch",
        "transformers",
    }
    assert verifier.PROHIBITED_PACKAGES["mlx"] == {
        "accelerate",
        "bitsandbytes",
        "kokoro",
        "torch",
    }
    assert verifier.PROHIBITED_PACKAGES["cuda"] == {"mlx", "mlx-lm", "mlx-audio"}


def test_each_profile_uses_a_distinct_clean_environment(monkeypatch, tmp_path) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(verifier, "hardware_preflight", lambda profile, **_: (True, "available"))
    monkeypatch.setattr(
        verifier,
        "verify_data_artifact",
        lambda profile, paths: verifier.ArtifactVerification(
            verifier.StageResult("artifact_integrity", "passed", "verified"),
            None,
            None,
        ),
    )

    report = verifier.verify_profiles(
        ["base", "mlx"], runner=runner, work_dir=tmp_path, keep_env=True
    )

    assert [profile.status for profile in report.profiles] == ["passed", "passed"]
    create_commands = [command for command in runner.commands if command[1:3] == ["-m", "venv"]]
    assert Path(create_commands[0][-1]).name == "base"
    assert Path(create_commands[1][-1]).name == "mlx"
    assert create_commands[0][-1] != create_commands[1][-1]
    assert report.session_directory is not None
    assert Path(report.session_directory).name.startswith(".orpheus-profile-verify-")


def test_verifier_never_touches_preexisting_profile_paths(tmp_path) -> None:
    sentinel = tmp_path / "base" / "sentinel.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("owned by user", encoding="utf-8")

    report = verifier.verify_profiles(["base"], runner=FakeRunner(), work_dir=tmp_path)

    assert report.profiles[0].status == "passed"
    assert sentinel.read_text(encoding="utf-8") == "owned by user"
    assert not tuple(tmp_path.glob(".orpheus-profile-verify-*"))


def test_keep_env_preserves_only_unique_owned_session(tmp_path) -> None:
    report = verifier.verify_profiles(
        ["base"], runner=FakeRunner(), work_dir=tmp_path, keep_env=True
    )

    session = Path(report.session_directory or "")
    assert session.parent == tmp_path
    assert session.is_dir()
    assert (session / verifier.SESSION_MARKER_NAME).is_file()
    assert Path(report.profiles[0].environment or "").parent == session


def test_session_creation_refuses_collision_and_symlink_workdir(monkeypatch, tmp_path) -> None:
    collision_name = ".orpheus-profile-verify-fixed"
    collision = tmp_path / collision_name
    collision.mkdir()
    sentinel = collision / "sentinel"
    sentinel.write_text("untouched", encoding="utf-8")
    monkeypatch.setattr(verifier, "_new_session_id", lambda: "fixed")

    with pytest.raises(verifier.SessionSafetyError, match="already exists"):
        verifier.verify_profiles(["base"], runner=FakeRunner(), work_dir=tmp_path)
    assert sentinel.read_text(encoding="utf-8") == "untouched"

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(verifier.SessionSafetyError, match="symlink"):
        verifier.verify_profiles(["base"], runner=FakeRunner(), work_dir=linked)


def test_cleanup_refuses_tampered_ownership_marker(tmp_path) -> None:
    session = verifier._create_owned_session(tmp_path)
    sentinel = session.path / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    (session.path / verifier.SESSION_MARKER_NAME).write_text(
        json.dumps({"schema_version": 1, "session": session.path.name, "token": "wrong"}),
        encoding="utf-8",
    )

    with pytest.raises(verifier.SessionSafetyError, match="does not match"):
        verifier._cleanup_owned_session(session)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_clean_environment_blocks_pip_and_user_site_configuration() -> None:
    clean = verifier.clean_environment(
        {
            "PATH": "/bin",
            "HOME": "/home/test",
            "HTTPS_PROXY": "http://proxy.invalid",
            "PIP_INDEX_URL": "https://user:pass@example.invalid/simple",
            "PIP_TRUSTED_HOST": "example.invalid",
            "PIP_CONFIG_FILE": "/tmp/host-pip.conf",
            "PYTHONPATH": "/tmp/injected",
            "PYTHONUSERBASE": "/tmp/user-site",
        }
    )

    assert clean["PIP_CONFIG_FILE"] == os.devnull
    assert clean["PIP_NO_INPUT"] == "1"
    assert clean["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
    assert clean["PYTHONNOUSERSITE"] == "1"
    assert clean["HTTPS_PROXY"] == "http://proxy.invalid"
    assert "PIP_INDEX_URL" not in clean
    assert "PIP_TRUSTED_HOST" not in clean
    assert "PYTHONPATH" not in clean
    assert "PYTHONUSERBASE" not in clean


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_timeout_terminates_child_process_group() -> None:
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
        "print(child.pid,flush=True); time.sleep(30)"
    )
    result = verifier.run_command(
        [sys.executable, "-c", parent_code, child_code],
        cwd=verifier.REPO_ROOT,
        timeout=0.2,
        env=verifier.clean_environment(),
    )

    assert result.timed_out
    child_pid = int(result.stdout.strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(child_pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not status or status.startswith("Z"):
            break
        time.sleep(0.05)
    assert not status or status.startswith("Z")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_timeout_supervisor_starts_and_terminates_process_group(monkeypatch) -> None:
    popen_options = {}
    signals = []

    class FakeProcess:
        pid = 4242
        returncode = None
        waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(["fake"], timeout)
            self.returncode = -15
            return self.returncode

        def kill(self):
            self.returncode = -9

    process = FakeProcess()

    def fake_popen(command, **kwargs):
        popen_options.update(kwargs)
        return process

    monkeypatch.setattr(verifier.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(verifier.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    result = verifier.run_command(["fake"], cwd=verifier.REPO_ROOT, timeout=0.01, env={})

    assert result.timed_out
    assert popen_options["start_new_session"] is True
    assert signals == [
        (4242, verifier.signal.SIGTERM),
        (4242, verifier.signal.SIGKILL),
    ]


def test_failed_command_output_is_bounded_and_redacted() -> None:
    result = verifier.run_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x'*100000 + ' password=hunter2\\n'); raise SystemExit(1)",
        ],
        cwd=verifier.REPO_ROOT,
        timeout=5,
        env=verifier.clean_environment(),
    )

    assert result.returncode == 1
    assert len(result.stderr) <= verifier.COMMAND_OUTPUT_TAIL_CHARS
    assert "hunter2" not in result.stderr


def test_docs_say_verifier_runs_exactly_requested_profiles() -> None:
    dependencies = (verifier.REPO_ROOT / "docs" / "DEPENDENCIES.md").read_text(encoding="utf-8")
    assert "runs exactly the profiles requested" in dependencies
    assert "request both `base` and the platform profile" in dependencies


def test_venv_python_symlink_identity_is_preserved_through_audit(monkeypatch, tmp_path) -> None:
    real_python = tmp_path / "python-real"
    real_python.write_text("#!/bin/sh\n", encoding="utf-8")
    real_python.chmod(0o755)
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(real_python)
    validation_commands = []

    def validate_runner(command, **kwargs):
        validation_commands.append(list(command))
        return _ok("Python 3.12")

    monkeypatch.setattr(verifier, "run_command", validate_runner)
    validated = verifier.validate_python(venv_python)
    runner = FakeRunner()
    report = verifier.verify_profiles(
        ["base"],
        runner=runner,
        source_python=validated,
        work_dir=tmp_path / "work",
    )

    assert validated == venv_python.absolute()
    assert validated.is_symlink()
    assert validation_commands[0][0] == str(venv_python.absolute())
    audit_command = next(command for command in runner.commands if "pip_audit" in command)
    assert audit_command[0] == str(venv_python.absolute())
    assert report.profiles[0].status == "passed"


def test_empty_invalid_audit_json_reports_safe_stderr(tmp_path) -> None:
    class EmptyAuditRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout, env):
            if "pip_audit" in command:
                return verifier.CommandResult(
                    1,
                    "",
                    "audit unavailable password=hunter2",
                    0.01,
                    False,
                )
            return super().__call__(command, cwd=cwd, timeout=timeout, env=env)

    report = verifier.verify_profiles(["base"], runner=EmptyAuditRunner(), work_dir=tmp_path)

    assert report.profiles[0].status == "failed"
    assert "audit unavailable" in report.profiles[0].reason
    assert "hunter2" not in report.profiles[0].reason
    assert "[REDACTED]" in report.profiles[0].reason


def test_exception_file_rejects_wrong_alias_package_and_profile(tmp_path) -> None:
    template = {
        "schema_version": 1,
        "exceptions": [
            {
                "advisory_id": "PYSEC-2026-139",
                "aliases": ["CVE-2026-4538"],
                "package": "torch",
                "profile": "cuda",
                "owner": "maintainers",
                "reason": "temporary",
                "created": "2026-07-14",
                "expires": "2026-10-14",
            }
        ],
    }
    for field, value in (("aliases", ["CVE-FAKE"]), ("package", "numpy"), ("profile", "base")):
        payload = json.loads(json.dumps(template))
        payload["exceptions"][0][field] = value
        path = tmp_path / f"{field}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(verifier.ExceptionFileError):
            verifier.load_audit_exceptions(path, today=date(2026, 7, 15))


def test_exception_file_rejects_future_created_date(tmp_path) -> None:
    payload = {
        "schema_version": 1,
        "exceptions": [
            {
                "advisory_id": "PYSEC-2026-139",
                "aliases": ["CVE-2026-4538"],
                "package": "torch",
                "profile": "cuda",
                "owner": "maintainers",
                "reason": "temporary",
                "created": "2026-07-16",
                "expires": "2026-10-14",
            }
        ],
    }
    path = tmp_path / "future.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(verifier.ExceptionFileError, match="future"):
        verifier.load_audit_exceptions(path, today=date(2026, 7, 15))


@pytest.mark.parametrize(
    ("profile", "inventory", "package"),
    [
        ("base", ["transformers"], "transformers"),
        ("base", ["mlx-lm"], "mlx-lm"),
        ("base", ["misaki"], "misaki"),
        ("base", ["spacy"], "spacy"),
        ("mlx", ["accelerate"], "accelerate"),
        ("cuda", ["mlx-lm"], "mlx-lm"),
    ],
)
def test_profile_inventory_rejects_every_cross_profile_package(
    monkeypatch, tmp_path, profile, inventory, package
) -> None:
    monkeypatch.setattr(verifier, "hardware_preflight", lambda profile, **_: (True, "available"))
    report = verifier.verify_profiles(
        [profile], runner=FakeRunner(inventory=inventory), work_dir=tmp_path
    )

    assert report.profiles[0].status == "failed"
    assert package in report.profiles[0].reason


def test_safe_output_redacts_credentials_paths_and_control_characters(monkeypatch, capsys) -> None:
    home = str(Path.home())
    unsafe = (
        f"https://user:pass@example.test/path?token=abc&api_key=def "
        f"Authorization=Bearer top-secret Bearer xyz sk-secret123 password=hunter2 "
        f"{home}/private\nnext\x00line"
    )
    sanitized = verifier.sanitize_output(unsafe)

    for secret in ("user:pass", "abc", "def", "top-secret", "xyz", "sk-secret123", "hunter2"):
        assert secret not in sanitized
    assert home not in sanitized
    assert "\n" not in sanitized
    assert "\x00" not in sanitized
    assert len(sanitized) <= verifier.SUMMARY_MAX_CHARS

    report = verifier.VerificationReport(
        (
            verifier.ProfileResult(
                "base",
                "failed",
                unsafe,
                (verifier.StageResult("install", "failed", unsafe),),
                f"{home}/environment",
            ),
        )
    )
    encoded = json.dumps(report.to_dict())
    assert "hunter2" not in encoded
    assert home not in encoded
    verifier._print_human(report)
    assert "hunter2" not in capsys.readouterr().out


def _record_hash(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={digest}", str(len(data))


def _make_data_artifact(site: Path) -> tuple[Path, Path]:
    package = site / "en_core_web_sm"
    dist_info = site / "en_core_web_sm-3.8.0.dist-info"
    package.mkdir(parents=True)
    dist_info.mkdir()
    module = package / "__init__.py"
    metadata = dist_info / "METADATA"
    direct_url = dist_info / "direct_url.json"
    module.write_text("__version__ = '3.8.0'\n", encoding="utf-8")
    metadata.write_text("Name: en-core-web-sm\nVersion: 3.8.0\n", encoding="utf-8")
    direct_url.write_text(
        json.dumps(
            {
                "url": verifier.EN_CORE_WEB_SM_URL,
                "archive_info": {
                    "hash": f"sha256={verifier.EN_CORE_WEB_SM_SHA256}",
                    "hashes": {"sha256": verifier.EN_CORE_WEB_SM_SHA256},
                },
            }
        ),
        encoding="utf-8",
    )
    rows = []
    for path in (module, metadata, direct_url):
        digest, size = _record_hash(path)
        rows.append(f"{path.relative_to(site).as_posix()},{digest},{size}\n")
    rows.append("en_core_web_sm-3.8.0.dist-info/RECORD,,\n")
    (dist_info / "RECORD").write_text("".join(rows), encoding="utf-8")
    return package, dist_info


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout regression")
def test_isolated_smoke_does_not_mutate_artifact_record_with_pyc(tmp_path) -> None:
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    site = (
        environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    package, _ = _make_data_artifact(site)
    python = environment / "bin" / "python"

    result = verifier.run_command(
        [python, "-B", "-I", "-c", "import en_core_web_sm"],
        cwd=verifier.REPO_ROOT,
        timeout=10,
        env=verifier.clean_environment(),
    )

    assert result.returncode == 0
    assert not (package / "__pycache__").exists()
    assert verifier.verify_data_artifact("mlx", [site]).stage.status == "passed"


def test_valid_data_artifact_is_integrity_verified_and_base_never_accepts_it(tmp_path) -> None:
    site = tmp_path / "site"
    _, dist_info = _make_data_artifact(site)

    verified = verifier.verify_data_artifact("mlx", [site])
    assert verified.stage.status == "passed"
    assert verified.excluded_metadata == dist_info
    assert verified.excluded_name == "en-core-web-sm"
    assert verifier.verify_data_artifact("base", [site]).stage.status == "failed"


@pytest.mark.parametrize("corruption", ["version", "url", "hash", "file", "traversal"])
def test_data_artifact_integrity_rejects_every_mismatch(tmp_path, corruption) -> None:
    site = tmp_path / "site"
    package, dist_info = _make_data_artifact(site)
    if corruption == "version":
        (dist_info / "METADATA").write_text(
            "Name: en-core-web-sm\nVersion: 3.8.1\n", encoding="utf-8"
        )
    elif corruption == "url":
        payload = json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))
        payload["url"] = "https://example.invalid/model.whl"
        (dist_info / "direct_url.json").write_text(json.dumps(payload), encoding="utf-8")
    elif corruption == "hash":
        payload = json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))
        payload["archive_info"]["hashes"]["sha256"] = "0" * 64
        (dist_info / "direct_url.json").write_text(json.dumps(payload), encoding="utf-8")
    elif corruption == "file":
        (package / "__init__.py").write_text("corrupt\n", encoding="utf-8")
    else:
        with (dist_info / "RECORD").open("a", encoding="utf-8") as record:
            record.write("../escape,sha256=AAAA,1\n")

    assert verifier.verify_data_artifact("cuda", [site]).stage.status == "failed"


def test_data_artifact_integrity_rejects_ambiguous_duplicate_distribution(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _make_data_artifact(first)
    _make_data_artifact(second)

    result = verifier.verify_data_artifact("mlx", [first, second])

    assert result.stage.status == "failed"
    assert "exactly one" in result.stage.summary


def test_local_artifact_is_excluded_only_after_verification_and_audit_coverage_is_exact(
    monkeypatch, tmp_path
) -> None:
    site = tmp_path / "site"
    _make_data_artifact(site)
    pip_info = site / "pip-26.1.2.dist-info"
    pip_info.mkdir()
    (pip_info / "METADATA").write_text("Name: pip\nVersion: 26.1.2\n", encoding="utf-8")
    audit = {
        "dependencies": [{"name": "pip", "version": "26.1.2", "vulns": []}],
        "fixes": [],
    }
    monkeypatch.setattr(verifier, "hardware_preflight", lambda profile, **_: (True, "available"))

    passed = verifier.verify_profiles(
        ["mlx"],
        runner=FakeRunner(
            audit=audit,
            inventory=["pip", "en-core-web-sm"],
            site_paths=[site],
        ),
        work_dir=tmp_path / "work",
    )
    assert passed.profiles[0].status == "passed"
    assert (
        next(
            stage for stage in passed.profiles[0].stages if stage.name == "artifact_integrity"
        ).status
        == "passed"
    )

    incomplete = verifier.verify_profiles(
        ["mlx"],
        runner=FakeRunner(
            audit={"dependencies": [], "fixes": []},
            inventory=["pip", "en-core-web-sm"],
            site_paths=[site],
        ),
        work_dir=tmp_path / "work-incomplete",
    )
    assert incomplete.profiles[0].status == "failed"
    assert "coverage" in incomplete.profiles[0].reason


def test_metadata_view_rejects_nested_directory_symlink_without_copying_external_file(
    tmp_path,
) -> None:
    site = tmp_path / "site"
    metadata = site / "example-1.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: example\nVersion: 1.0\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_text("must not be copied", encoding="utf-8")
    (metadata / "linked-data").symlink_to(external, target_is_directory=True)
    destination = tmp_path / "audit-metadata"

    with pytest.raises(ValueError, match="symlink"):
        verifier._copy_metadata_view([site], destination, None)

    assert not (destination / metadata.name / "linked-data" / "secret.txt").exists()


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "message"),
    [
        ("METADATA_VIEW_MAX_FILES", 1, "safety bounds"),
        ("METADATA_VIEW_MAX_BYTES", 1, "safety bounds"),
    ],
)
def test_metadata_view_enforces_file_and_byte_bounds(
    monkeypatch, tmp_path, limit_name, limit_value, message
) -> None:
    site = tmp_path / "site"
    metadata = site / "example-1.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: example\nVersion: 1.0\n", encoding="utf-8")
    (metadata / "WHEEL").write_text("Wheel-Version: 1.0\n", encoding="utf-8")
    monkeypatch.setattr(verifier, limit_name, limit_value)

    with pytest.raises(ValueError, match=message):
        verifier._copy_metadata_view([site], tmp_path / "view", None)


def test_json_main_exit_codes(monkeypatch, capsys) -> None:
    skipped = verifier.VerificationReport(
        (verifier.ProfileResult("cuda", "skipped", "no GPU", ()),)
    )
    monkeypatch.setattr(verifier, "verify_profiles", lambda *args, **kwargs: skipped)

    assert verifier.main(["--profile", "cuda", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["profiles"][0]["status"] == "skipped"
    assert verifier.main(["--profile", "cuda", "--json", "--allow-skipped"]) == 0


def test_main_reports_workdir_setup_safety_error_as_redacted_json(tmp_path, capsys) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "password=private-workdir"
    linked.symlink_to(real, target_is_directory=True)

    exit_code = verifier.main(["--profile", "base", "--work-dir", str(linked), "--json"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert set(payload) == {"error"}
    assert "private-workdir" not in payload["error"]
    assert "[REDACTED]" in payload["error"]
    assert "Traceback" not in captured.out


def test_main_reports_cleanup_safety_error_as_redacted_human_message(monkeypatch, capsys) -> None:
    monkeypatch.setattr(verifier, "validate_python", lambda path: path)

    def fail_cleanup(*args, **kwargs):
        raise verifier.SessionSafetyError(
            f"cleanup password=hunter2 at {Path.home()}/private-session"
        )

    monkeypatch.setattr(verifier, "verify_profiles", fail_cleanup)

    assert verifier.main(["--profile", "base"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("verification error:")
    assert "hunter2" not in captured.err
    assert str(Path.home()) not in captured.err
    assert "[REDACTED]" in captured.err
    assert "Traceback" not in captured.err
