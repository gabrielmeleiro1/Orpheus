"""Tests for strict macOS release preflight checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import pytest

from tools.release_qualification.model import load_qualification_config
from tools.release_qualification.preflight import (
    PINNED_RUST_VERSION,
    PreflightError,
    PreflightFacts,
    collect_preflight,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "release_qualification.toml"


@dataclass
class FakeHost:
    system: str = "Darwin"
    machine: str = "arm64"
    macos_version: str = "14.5"
    os_build: str = "23F79"
    python_version: tuple[int, int, int] = (3, 12, 8)
    node_version: str = "v20.18.0"
    rust_version: str = "1.88.0"
    tools_present: frozenset[str] = frozenset(
        {"hdiutil", "otool", "codesign", "file", "ffmpeg", "ffprobe"}
    )
    free_bytes: int = 50 * 1024**3
    required_bytes: int = 20 * 1024**3
    git_commit: str = "abc123def456"
    git_branch: str = "feature/app-performance"
    git_tag: str | None = "v1.0.0"
    git_tag_commit: str | None = "abc123def456"
    git_dirty_tracked: tuple[str, ...] = ()
    resolve_tags: dict[str, str | None] | None = None
    package_version: str = "1.0.0"
    tauri_version: str = "1.0.0"
    config_hash: str = "cfghash"
    workflow_exists: bool = True


def _readers(host: FakeHost) -> dict[str, Callable[..., object]]:
    def platform_system() -> str:
        return host.system

    def platform_machine() -> str:
        return host.machine

    def macos_product_version() -> str:
        return host.macos_version

    def macos_build_version() -> str:
        return host.os_build

    def python_version_info() -> tuple[int, int, int]:
        return host.python_version

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in host.tools_present else None

    def run_version(argv: tuple[str, ...]) -> str:
        if argv[:2] == ("node", "--version"):
            return host.node_version
        if argv[:2] == ("rustc", "--version"):
            return f"rustc {host.rust_version} (aaaaaaaaaaa 2025-01-01)"
        raise AssertionError(f"unexpected version command: {argv}")

    def disk_usage(_path: Path) -> tuple[int, int]:
        # free, required
        return host.free_bytes, host.required_bytes

    def git_status() -> Mapping[str, object]:
        return {
            "commit": host.git_commit,
            "branch": host.git_branch,
            "tag": host.git_tag,
            "tag_commit": host.git_tag_commit,
            "dirty_tracked": list(host.git_dirty_tracked),
        }

    def resolve_tag_commit(tag: str) -> str | None:
        mapping = host.resolve_tags if host.resolve_tags is not None else {}
        return mapping.get(tag)

    def read_package_version(_root: Path) -> str:
        return host.package_version

    def read_tauri_version(_root: Path) -> str:
        return host.tauri_version

    def config_hash(_path: Path) -> str:
        return host.config_hash

    def workflow_present(_root: Path) -> bool:
        return host.workflow_exists

    return {
        "platform_system": platform_system,
        "platform_machine": platform_machine,
        "macos_product_version": macos_product_version,
        "macos_build_version": macos_build_version,
        "python_version_info": python_version_info,
        "which": which,
        "run_version": run_version,
        "disk_usage": disk_usage,
        "git_status": git_status,
        "resolve_tag_commit": resolve_tag_commit,
        "read_package_version": read_package_version,
        "read_tauri_version": read_tauri_version,
        "config_hash": config_hash,
        "workflow_present": workflow_present,
    }


def _collect(host: FakeHost, *, release_version: str = "1.0.0"):
    config = load_qualification_config(DEFAULT_CONFIG)
    return collect_preflight(
        repo_root=REPO_ROOT,
        config=config,
        config_path=DEFAULT_CONFIG,
        release_version=release_version,
        **_readers(host),
    )


@pytest.mark.parametrize(
    "workflow", ["source-validation.yml", "release-artifact-validation.yml"]
)
def test_pinned_rust_version_matches_the_ci_toolchain(workflow: str):
    text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    assert f"rustup toolchain install {PINNED_RUST_VERSION} " in text
    assert f"rustup default {PINNED_RUST_VERSION}" in text


def test_non_darwin_host_fails():
    host = FakeHost(system="Linux")
    with pytest.raises(PreflightError, match="darwin|Darwin|macOS"):
        _collect(host)


def test_non_arm64_host_fails():
    host = FakeHost(machine="x86_64")
    with pytest.raises(PreflightError, match="arm64"):
        _collect(host)


def test_macos_below_14_fails():
    host = FakeHost(macos_version="13.6.7")
    with pytest.raises(PreflightError, match="14"):
        _collect(host)


def test_macos_major_only_14_is_accepted():
    host = FakeHost(macos_version="14")
    facts = _collect(host)
    assert facts.macos_version == "14"


def test_macos_minimum_comes_from_config():
    from dataclasses import replace

    config = replace(load_qualification_config(DEFAULT_CONFIG), minimum_macos="15.1")
    with pytest.raises(PreflightError, match="below required 15.1"):
        collect_preflight(
            repo_root=REPO_ROOT,
            config=config,
            config_path=DEFAULT_CONFIG,
            release_version="1.0.0",
            **_readers(FakeHost(macos_version="15.0")),
        )


def test_python_below_3_12_fails():
    host = FakeHost(python_version=(3, 11, 9))
    with pytest.raises(PreflightError, match="3\\.12|Python"):
        _collect(host)


def test_node_below_20_fails():
    host = FakeHost(node_version="v18.20.0")
    with pytest.raises(PreflightError, match="Node|20"):
        _collect(host)


def test_rust_not_equal_to_pinned_toolchain_fails():
    host = FakeHost(rust_version="1.87.0")
    with pytest.raises(PreflightError, match="1\\.88\\.0|Rust"):
        _collect(host)

    host = FakeHost(rust_version="1.89.0")
    with pytest.raises(PreflightError, match="1\\.88\\.0|Rust"):
        _collect(host)


@pytest.mark.parametrize(
    "missing",
    ["hdiutil", "otool", "codesign", "file", "ffmpeg", "ffprobe"],
)
def test_missing_required_tool_fails(missing: str):
    present = frozenset({"hdiutil", "otool", "codesign", "file", "ffmpeg", "ffprobe"}) - {
        missing
    }
    host = FakeHost(tools_present=present)
    with pytest.raises(PreflightError, match=missing):
        _collect(host)


def test_dirty_tracked_files_fail_while_ignored_evidence_does_not():
    host = FakeHost(git_dirty_tracked=("tools/release_qualification/preflight.py",))
    with pytest.raises(PreflightError, match="dirty|tracked|clean"):
        _collect(host)

    # Ignored evidence paths are not reported as dirty tracked files by the git reader.
    host = FakeHost(git_dirty_tracked=())
    _collect(host)


def test_default_git_status_tracked_dirty_vs_ignored_evidence(tmp_path: Path):
    """Exercise production default_git_status against a real temporary git repo."""
    import subprocess

    from tools.release_qualification.preflight import default_git_status

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / ".gitignore").write_text(
        ".artifacts/\n.artifacts/release-qualification/\n", encoding="utf-8"
    )
    tracked = repo / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    evidence = repo / ".artifacts" / "release-qualification" / "run-1"
    evidence.mkdir(parents=True)
    (evidence / "report.json").write_text("{}", encoding="utf-8")

    clean = default_git_status(repo)
    assert list(clean["dirty_tracked"]) == []

    tracked.write_text("dirty\n", encoding="utf-8")
    dirty = default_git_status(repo)
    dirty_paths = [str(item) for item in dirty["dirty_tracked"]]
    assert dirty_paths == ["tracked.txt"], dirty_paths
    assert not any(
        "release-qualification" in item or ".artifacts" in item for item in dirty_paths
    )


def test_detached_and_uncommitted_state_recorded_precisely():
    host = FakeHost(git_branch="HEAD", git_tag=None, git_tag_commit=None)
    facts = _collect(host)
    assert facts.branch == "HEAD"
    assert facts.tag is None
    assert facts.commit == "abc123def456"
    assert "branch" in facts.as_allowlisted_dict()
    assert facts.as_allowlisted_dict()["tag"] is None


def test_insufficient_disk_space_fails_with_required_and_available_bytes():
    host = FakeHost(free_bytes=5 * 1024**3, required_bytes=20 * 1024**3)
    with pytest.raises(PreflightError, match="disk|space") as exc_info:
        _collect(host)
    message = str(exc_info.value)
    assert str(host.free_bytes) in message
    assert str(host.required_bytes) in message


def test_tauri_package_and_release_versions_must_agree():
    host = FakeHost(package_version="1.0.0", tauri_version="0.9.0")
    with pytest.raises(PreflightError, match="version"):
        _collect(host)

    host = FakeHost(package_version="0.9.0", tauri_version="0.9.0")
    with pytest.raises(PreflightError, match="version"):
        _collect(host, release_version="1.0.0")


def test_existing_tag_must_point_at_current_commit():
    host = FakeHost(git_tag="v1.0.0", git_tag_commit="deadbeef0000")
    with pytest.raises(PreflightError, match="tag|commit"):
        _collect(host, release_version="1.0.0")


def test_expected_tag_resolved_even_when_head_has_other_exact_tag():
    """v{release} must be checked even if HEAD is exact-matched to another tag."""
    host = FakeHost(
        git_tag="v0.9.0",
        git_tag_commit="abc123def456",
        resolve_tags={"v1.0.0": "othercommit999"},
    )
    with pytest.raises(PreflightError, match="tag|commit"):
        _collect(host, release_version="1.0.0")


def test_expected_tag_on_other_commit_when_head_untagged():
    host = FakeHost(
        git_tag=None,
        git_tag_commit=None,
        resolve_tags={"v1.0.0": "othercommit999"},
    )
    with pytest.raises(PreflightError, match="tag|commit"):
        _collect(host, release_version="1.0.0")


def test_injected_git_status_does_not_disable_default_tag_resolver(monkeypatch):
    """Omitting resolve_tag_commit must still install production tag resolution."""
    calls: list[str] = []

    def fake_resolve(repo_root: Path, tag: str) -> str | None:
        calls.append(tag)
        assert repo_root == REPO_ROOT
        return "othercommit999"

    monkeypatch.setattr(
        "tools.release_qualification.preflight.default_resolve_tag_commit",
        fake_resolve,
    )
    host = FakeHost(git_tag="v0.9.0", git_tag_commit="abc123def456")
    readers = _readers(host)
    readers.pop("resolve_tag_commit")
    config = load_qualification_config(DEFAULT_CONFIG)
    with pytest.raises(PreflightError, match="tag|commit"):
        collect_preflight(
            repo_root=REPO_ROOT,
            config=config,
            config_path=DEFAULT_CONFIG,
            release_version="1.0.0",
            **readers,
        )
    assert calls == ["v1.0.0"]


def test_matching_tag_and_versions_pass():
    host = FakeHost()
    facts = _collect(host, release_version="1.0.0")
    assert isinstance(facts, PreflightFacts)
    assert facts.release_version == "1.0.0"
    assert facts.rust_version == PINNED_RUST_VERSION
    assert facts.python_version.startswith("3.12")
    assert facts.node_version.startswith("20") or facts.node_version.startswith("v20")


def test_github_actions_workflow_is_informational_not_a_dependency():
    host = FakeHost(workflow_exists=False)
    facts = _collect(host)
    payload = facts.as_allowlisted_dict()
    assert "source_validation_workflow" in payload
    assert payload["source_validation_workflow"] == "missing_informational"
    assert "PATH" not in payload
    assert "environ" not in payload
    # Never dump the complete environment.
    assert not any(key.lower() == "env" for key in payload)


def test_preflight_records_only_allowlisted_facts():
    host = FakeHost()
    facts = _collect(host)
    payload = facts.as_allowlisted_dict()
    allowed = {
        "system",
        "machine",
        "macos_version",
        "os_build",
        "python_version",
        "node_version",
        "rust_version",
        "commit",
        "branch",
        "tag",
        "disk_free_bytes",
        "disk_required_bytes",
        "config_hash",
        "release_version",
        "package_version",
        "tauri_version",
        "source_validation_workflow",
        "tools",
    }
    assert set(payload) <= allowed
    assert payload["config_hash"] == "cfghash"
    assert payload["commit"] == "abc123def456"
    assert json.dumps(payload, sort_keys=True)
