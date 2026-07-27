"""Strict macOS release-host preflight for local qualification."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .model import QualificationConfig

PINNED_RUST_VERSION = "1.88.0"
MINIMUM_MACOS = (14, 0)
MINIMUM_PYTHON = (3, 12)
MINIMUM_NODE_MAJOR = 20
REQUIRED_TOOLS: tuple[str, ...] = (
    "hdiutil",
    "otool",
    "codesign",
    "file",
    "ffmpeg",
    "ffprobe",
)
DEFAULT_REQUIRED_DISK_BYTES = 20 * 1024**3
SOURCE_VALIDATION_WORKFLOW = Path(".github") / "workflows" / "source-validation.yml"


class PreflightError(ValueError):
    """Raised when the local host fails a release preflight gate."""


@dataclass(frozen=True)
class PreflightFacts:
    ok: bool
    system: str
    machine: str
    macos_version: str
    os_build: str
    python_version: str
    node_version: str
    rust_version: str
    commit: str
    branch: str
    tag: str | None
    disk_free_bytes: int
    disk_required_bytes: int
    config_hash: str
    release_version: str
    package_version: str
    tauri_version: str
    source_validation_workflow: str
    tools: tuple[str, ...]

    def as_allowlisted_dict(self) -> dict[str, object]:
        return {
            "system": self.system,
            "machine": self.machine,
            "macos_version": self.macos_version,
            "os_build": self.os_build,
            "python_version": self.python_version,
            "node_version": self.node_version,
            "rust_version": self.rust_version,
            "commit": self.commit,
            "branch": self.branch,
            "tag": self.tag,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_required_bytes": self.disk_required_bytes,
            "config_hash": self.config_hash,
            "release_version": self.release_version,
            "package_version": self.package_version,
            "tauri_version": self.tauri_version,
            "source_validation_workflow": self.source_validation_workflow,
            "tools": list(self.tools),
        }


def _parse_version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    if not parts:
        raise PreflightError(f"unable to parse version: {text!r}")
    return tuple(parts)


def _normalize_version_tuple(text: str, *, width: int) -> tuple[int, ...]:
    parts = list(_parse_version_tuple(text))
    if len(parts) < width:
        parts.extend([0] * (width - len(parts)))
    return tuple(parts)


def _parse_node_major(text: str) -> int:
    cleaned = text.strip().lstrip("vV")
    return _parse_version_tuple(cleaned)[0]


def _parse_rustc_version(text: str) -> str:
    tokens = text.strip().split()
    if len(tokens) < 2 or tokens[0] != "rustc":
        raise PreflightError(f"unable to parse rustc version: {text!r}")
    return tokens[1]


def default_platform_system() -> str:
    return platform.system()


def default_platform_machine() -> str:
    return platform.machine()


def default_macos_product_version() -> str:
    completed = subprocess.run(
        ["sw_vers", "-productVersion"],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise PreflightError("unable to read macOS product version")
    return completed.stdout.strip()


def default_macos_build_version() -> str:
    completed = subprocess.run(
        ["sw_vers", "-buildVersion"],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise PreflightError("unable to read macOS build version")
    return completed.stdout.strip()


def default_python_version_info() -> tuple[int, int, int]:
    info = platform.python_version_tuple()
    return int(info[0]), int(info[1]), int(info[2])


def default_which(name: str) -> str | None:
    return shutil.which(name)


def default_run_version(argv: Sequence[str]) -> str:
    completed = subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise PreflightError(f"failed to run version command: {' '.join(argv)}")
    return (completed.stdout or completed.stderr).strip()


def default_disk_usage(path: Path) -> tuple[int, int]:
    usage = shutil.disk_usage(path)
    return int(usage.free), DEFAULT_REQUIRED_DISK_BYTES


def default_git_status(repo_root: Path) -> Mapping[str, object]:
    def _git(*args: str, strip: bool = True) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
        if completed.returncode != 0:
            raise PreflightError(f"git {' '.join(args)} failed")
        text = completed.stdout
        return text.strip() if strip else text.rstrip("\n")

    commit = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    # Porcelain lines begin with a two-char status; do not strip leading spaces.
    dirty_raw = _git("status", "--porcelain", "--untracked-files=no", strip=False)
    dirty_tracked = tuple(
        line[3:] for line in dirty_raw.splitlines() if line and not line.startswith("??")
    )

    tag: str | None = None
    tag_commit: str | None = None
    describe = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if describe.returncode == 0 and describe.stdout.strip():
        tag = describe.stdout.strip()
        tag_commit = commit

    # Exact-match HEAD tag only. Expected release-tag lookup (v{version}) is
    # performed separately by collect_preflight via resolve_tag_commit.
    return {
        "commit": commit,
        "branch": branch,
        "tag": tag,
        "tag_commit": tag_commit,
        "dirty_tracked": list(dirty_tracked),
    }


def default_resolve_tag_commit(repo_root: Path, tag: str) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", f"{tag}^{{}}"],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def default_read_package_version(repo_root: Path) -> str:
    path = repo_root / "desktop" / "package.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    version = str(payload.get("version", "")).strip()
    if not version:
        raise PreflightError("desktop/package.json version is missing")
    return version


def default_read_tauri_version(repo_root: Path) -> str:
    path = repo_root / "desktop" / "src-tauri" / "tauri.conf.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    version = str(payload.get("version", "")).strip()
    if not version:
        raise PreflightError("tauri.conf.json version is missing")
    return version


def default_config_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_toolchain_fingerprint() -> str:
    """Hash allowlisted toolchain facts for resume fingerprinting."""
    py = default_python_version_info()
    try:
        node_version = default_run_version(("node", "--version")).strip().lstrip("vV")
    except Exception:  # noqa: BLE001 - fingerprint must still be stable enough to compare
        node_version = "unavailable"
    try:
        rust_version = _parse_rustc_version(default_run_version(("rustc", "--version")))
    except Exception:  # noqa: BLE001
        rust_version = "unavailable"
    tools = {name: default_which(name) is not None for name in REQUIRED_TOOLS}
    payload = {
        "python": f"{py[0]}.{py[1]}.{py[2]}",
        "node": node_version,
        "rustc": rust_version,
        "tools": tools,
        "pinned_rust": PINNED_RUST_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def default_workflow_present(repo_root: Path) -> bool:
    return (repo_root / SOURCE_VALIDATION_WORKFLOW).is_file()


def collect_preflight(
    *,
    repo_root: Path,
    config: QualificationConfig,
    config_path: Path,
    release_version: str,
    platform_system: Callable[[], str] = default_platform_system,
    platform_machine: Callable[[], str] = default_platform_machine,
    macos_product_version: Callable[[], str] = default_macos_product_version,
    macos_build_version: Callable[[], str] = default_macos_build_version,
    python_version_info: Callable[[], tuple[int, int, int]] = default_python_version_info,
    which: Callable[[str], str | None] = default_which,
    run_version: Callable[[Sequence[str]], str] = default_run_version,
    disk_usage: Callable[[Path], tuple[int, int]] = default_disk_usage,
    git_status: Callable[[], Mapping[str, object]] | None = None,
    resolve_tag_commit: Callable[[str], str | None] | None = None,
    read_package_version: Callable[[Path], str] = default_read_package_version,
    read_tauri_version: Callable[[Path], str] = default_read_tauri_version,
    config_hash: Callable[[Path], str] = default_config_hash,
    workflow_present: Callable[[Path], bool] = default_workflow_present,
) -> PreflightFacts:
    root = Path(repo_root)
    errors: list[str] = []

    system = platform_system()
    machine = platform_machine()
    if system.lower() != "darwin":
        errors.append(f"unsupported system {system!r}; macOS (Darwin) required")
    if machine != config.architecture:
        errors.append(
            f"unsupported architecture {machine!r}; {config.architecture} required"
        )

    macos_version = macos_product_version()
    os_build = macos_build_version()
    if _normalize_version_tuple(macos_version, width=len(MINIMUM_MACOS)) < MINIMUM_MACOS:
        errors.append(f"macOS {macos_version} is below required 14.0")

    py = python_version_info()
    python_version = f"{py[0]}.{py[1]}.{py[2]}"
    if py[:2] < MINIMUM_PYTHON:
        errors.append(f"Python {python_version} is below required 3.12")

    node_raw = run_version(("node", "--version"))
    if _parse_node_major(node_raw) < MINIMUM_NODE_MAJOR:
        errors.append(f"Node {node_raw} is below required 20")
    node_version = node_raw.lstrip("vV")

    rust_raw = run_version(("rustc", "--version"))
    rust_version = _parse_rustc_version(rust_raw)
    if rust_version != PINNED_RUST_VERSION:
        errors.append(
            f"Rust {rust_version} does not equal pinned toolchain {PINNED_RUST_VERSION}"
        )

    missing_tools = [name for name in REQUIRED_TOOLS if which(name) is None]
    if missing_tools:
        errors.append(f"missing required tools: {', '.join(missing_tools)}")

    free_bytes, required_bytes = disk_usage(root)
    if free_bytes < required_bytes:
        errors.append(
            f"insufficient disk space: available={free_bytes} required={required_bytes}"
        )

    if git_status is None:
        status = default_git_status(root)
    else:
        status = git_status()
    commit = str(status["commit"])
    branch = str(status["branch"])
    tag_raw = status.get("tag")
    tag_value = None if tag_raw is None else str(tag_raw)
    tag_commit_raw = status.get("tag_commit")
    dirty = tuple(str(item) for item in list(status.get("dirty_tracked", [])))
    if dirty:
        errors.append(f"dirty tracked files present: {', '.join(dirty)}")

    package_version = read_package_version(root)
    tauri_version = read_tauri_version(root)
    requested = config.require_version(release_version)
    if not (package_version == tauri_version == requested):
        errors.append(
            "version mismatch: "
            f"package={package_version} tauri={tauri_version} release={requested}"
        )

    expected_tag = f"v{requested}"
    tag_commit = None if tag_commit_raw is None else str(tag_commit_raw)

    # Always resolve the expected release tag, independent of any other exact
    # HEAD tag reported by git status. Omitting resolve_tag_commit still installs
    # the production resolver so injected git_status cannot silently skip this.
    resolver = resolve_tag_commit
    if resolver is None:

        def _default_resolver(tag: str) -> str | None:
            return default_resolve_tag_commit(root, tag)

        resolver = _default_resolver

    looked_up = resolver(expected_tag)
    if looked_up is not None:
        tag_value = expected_tag
        tag_commit = looked_up

    if tag_value == expected_tag and tag_commit != commit:
        errors.append(
            f"tag {tag_value} points at commit {tag_commit!r}, expected {commit}"
        )

    cfg_hash = config_hash(Path(config_path))
    workflow_state = (
        "present_informational" if workflow_present(root) else "missing_informational"
    )

    if errors:
        raise PreflightError("; ".join(errors))

    return PreflightFacts(
        ok=True,
        system=system,
        machine=machine,
        macos_version=macos_version,
        os_build=os_build,
        python_version=python_version,
        node_version=node_version,
        rust_version=rust_version,
        commit=commit,
        branch=branch,
        tag=tag_value,
        disk_free_bytes=free_bytes,
        disk_required_bytes=required_bytes,
        config_hash=cfg_hash,
        release_version=requested,
        package_version=package_version,
        tauri_version=tauri_version,
        source_validation_workflow=workflow_state,
        tools=REQUIRED_TOOLS,
    )
