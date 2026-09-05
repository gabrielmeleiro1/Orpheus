#!/usr/bin/env python3
"""Verify runtime profiles in disposable, independently audited environments."""

from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import asdict, dataclass
from datetime import date
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.runtime_profiles import get_runtime_profile  # noqa: E402


DEFAULT_EXCEPTIONS_FILE = REPO_ROOT / "security" / "runtime-audit-exceptions.json"
DEFAULT_COMMAND_TIMEOUT = 120.0
DEFAULT_INSTALL_TIMEOUT = 1800.0
SUMMARY_MAX_CHARS = 300
COMMAND_OUTPUT_MAX_BYTES = 8 * 1024 * 1024
COMMAND_OUTPUT_TAIL_CHARS = 64 * 1024
PROCESS_TERMINATION_GRACE_SECONDS = 2.0
SESSION_MARKER_NAME = ".orpheus-profile-verify-owner.json"
SESSION_PREFIX = ".orpheus-profile-verify-"
METADATA_VIEW_MAX_FILES = 20_000
METADATA_VIEW_MAX_BYTES = 128 * 1024 * 1024
TOOLING_PINS = ("pip==26.2", "setuptools==83.0.0", "wheel==0.47.0")
PROHIBITED_PACKAGES = {
    "base": {
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
    },
    "mlx": {"accelerate", "bitsandbytes", "kokoro", "torch"},
    "cuda": {"mlx", "mlx-lm", "mlx-audio"},
}
RECOGNIZED_EXCEPTIONS = {
    ("cuda", "torch", "PYSEC-2026-139"): ("CVE-2026-4538",),
}

EN_CORE_WEB_SM_NAME = "en-core-web-sm"
EN_CORE_WEB_SM_VERSION = "3.8.0"
EN_CORE_WEB_SM_URL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
)
EN_CORE_WEB_SM_SHA256 = "1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"
DATA_ARTIFACTS = {
    EN_CORE_WEB_SM_NAME: {
        "version": EN_CORE_WEB_SM_VERSION,
        "url": EN_CORE_WEB_SM_URL,
        "sha256": EN_CORE_WEB_SM_SHA256,
        "profiles": frozenset({"mlx", "cuda"}),
    }
}

BASE_SMOKE = (
    """import aiohttp, bs4, ebooklib, fastapi, lxml, numpy, pydub, scipy, soundfile, uvicorn"""
)
MLX_SMOKE = """
import importlib.util
import en_core_web_sm, mlx, mlx_audio, mlx_lm, misaki, spacy, transformers
assert importlib.util.find_spec('mlx_audio.tts') is not None
assert en_core_web_sm.load().lang == 'en'
"""
CUDA_SMOKE = """
import accelerate, bitsandbytes, en_core_web_sm, kokoro, spacy, torch, transformers
assert torch.cuda.is_available(), 'torch cannot access an NVIDIA CUDA device'
assert torch.cuda.device_count() > 0, 'no CUDA devices were enumerated'
assert en_core_web_sm.load().lang == 'en'
"""
SMOKE_CODE = {"base": BASE_SMOKE, "mlx": MLX_SMOKE, "cuda": CUDA_SMOKE}

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_URL_USERINFO = re.compile(r"(?i)\b(https?://)[^/\s@]+@")
_QUERY_CREDENTIAL = re.compile(
    r"(?i)([?&](?:token|api[_-]?key|key|password|secret|auth|authorization)=)[^&\s]+"
)
_BEARER_CREDENTIAL = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_OPENAI_STYLE_KEY = re.compile(r"(?i)\bsk-[a-z0-9_-]+")
_KEY_VALUE_CREDENTIAL = re.compile(
    r"(?i)\b(token|api[_-]?key|key|password|secret|auth|authorization)\s*=\s*[^\s,;]+"
)


class ExceptionFileError(ValueError):
    """Raised when the narrowly scoped audit exception file is invalid."""


class SessionSafetyError(RuntimeError):
    """Raised when a verifier work session cannot be proven process-owned."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False


@dataclass(frozen=True)
class StageResult:
    name: str
    status: str
    summary: str
    duration: float = 0.0


@dataclass(frozen=True)
class ProfileResult:
    profile: str
    status: str
    reason: str
    stages: tuple[StageResult, ...]
    environment: str | None = None


@dataclass(frozen=True)
class VerificationReport:
    profiles: tuple[ProfileResult, ...]
    session_directory: str | None = None

    @property
    def has_accepted_risk(self) -> bool:
        return any(
            stage.status == "accepted_risk" for profile in self.profiles for stage in profile.stages
        )

    def exit_code(self, *, allow_skipped: bool = False) -> int:
        if any(profile.status == "failed" for profile in self.profiles):
            return 1
        if not allow_skipped and any(profile.status == "skipped" for profile in self.profiles):
            return 2
        return 0

    def to_dict(self) -> dict[str, Any]:
        return _sanitize_value(
            {
                "profiles": [asdict(profile) for profile in self.profiles],
                "accepted_risk": self.has_accepted_risk,
                "session_directory": self.session_directory,
            }
        )


@dataclass(frozen=True)
class AuditException:
    advisory_id: str
    aliases: tuple[str, ...]
    package: str
    profile: str
    owner: str
    reason: str
    created: date
    expires: date

    @property
    def identifiers(self) -> frozenset[str]:
        return frozenset((self.advisory_id, *self.aliases))


Runner = Callable[..., CommandResult]


@dataclass(frozen=True)
class ArtifactVerification:
    stage: StageResult
    excluded_metadata: Path | None
    excluded_name: str | None


@dataclass(frozen=True)
class OwnedSession:
    path: Path
    parent: Path
    token: str


def sanitize_output(value: object, *, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Redact credentials, local home paths, and terminal control input."""
    text = str(value)
    text = _CONTROL_CHARACTERS.sub(" ", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = _QUERY_CREDENTIAL.sub(r"\1[REDACTED]", text)
    text = _BEARER_CREDENTIAL.sub("Bearer [REDACTED]", text)
    text = _OPENAI_STYLE_KEY.sub("[REDACTED]", text)
    text = _KEY_VALUE_CREDENTIAL.sub(r"\1=[REDACTED]", text)
    home = str(Path.home())
    if home and home != os.path.sep:
        text = text.replace(home, "~")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_output(value)
    if isinstance(value, dict):
        return {sanitize_output(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return value


def _canonical_package_name(value: object) -> str:
    return re.sub(r"[-_.]+", "-", str(value).strip().lower())


def _normalize_artifact_url(value: object) -> str:
    parsed = urlsplit(str(value))
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("artifact URL is not an exact HTTPS archive URL")
    hostname = parsed.hostname.lower()
    netloc = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    path = quote(unquote(parsed.path), safe="/-._~")
    return urlunsplit(("https", netloc, path, "", ""))


def _distribution_metadata_entries(site_paths: Sequence[Path]) -> list[tuple[str, str, Path]]:
    entries: list[tuple[str, str, Path]] = []
    for site_path in site_paths:
        if not site_path.is_dir():
            continue
        for candidate in site_path.iterdir():
            lowered = candidate.name.lower()
            if not (lowered.endswith(".dist-info") or lowered.endswith(".egg-info")):
                continue
            if candidate.is_symlink():
                raise ValueError(f"ambiguous distribution metadata: {candidate.name}")
            metadata_path = (
                candidate / "METADATA"
                if candidate.is_dir() and lowered.endswith(".dist-info")
                else candidate / "PKG-INFO"
                if candidate.is_dir()
                else candidate
            )
            try:
                metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                raise ValueError(f"unreadable distribution metadata: {candidate.name}") from None
            name = _canonical_package_name(metadata.get("Name", ""))
            version = str(metadata.get("Version", "")).strip()
            if not name or not version:
                raise ValueError(f"incomplete distribution metadata: {candidate.name}")
            entries.append((name, version, candidate))
    return entries


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _verify_record(site_root: Path, dist_info: Path) -> None:
    record_path = dist_info / "RECORD"
    try:
        with record_path.open(newline="", encoding="utf-8") as record_file:
            rows = list(csv.reader(record_file))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("data artifact RECORD is missing or unreadable") from exc
    if not rows:
        raise ValueError("data artifact RECORD is empty")

    root = site_root.resolve(strict=True)
    record_relative = record_path.relative_to(site_root).as_posix()
    recorded_paths: set[str] = set()
    for row in rows:
        if len(row) != 3:
            raise ValueError("data artifact RECORD row is malformed")
        relative_text, encoded_hash, expected_size = row
        if (
            not relative_text
            or "\\" in relative_text
            or relative_text.startswith("/")
            or any(part in {"", ".", ".."} for part in relative_text.split("/"))
        ):
            raise ValueError("data artifact RECORD path is unsafe")
        if relative_text in recorded_paths:
            raise ValueError("data artifact RECORD contains duplicate paths")
        recorded_paths.add(relative_text)
        unresolved_candidate = site_root / Path(*relative_text.split("/"))
        relative_candidate = unresolved_candidate.relative_to(site_root)
        current = site_root
        for part in relative_candidate.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("data artifact RECORD path contains a symlink")
        candidate = unresolved_candidate.resolve(strict=False)
        if not _path_is_within(candidate, root):
            raise ValueError("data artifact RECORD path escapes site-packages")
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("data artifact RECORD file is missing or ambiguous")
        if relative_text == record_relative and not encoded_hash and not expected_size:
            continue
        if not encoded_hash or not expected_size:
            raise ValueError("only RECORD may omit its hash and size")
        try:
            algorithm, encoded_digest = encoded_hash.split("=", 1)
            size = int(expected_size)
            expected_digest = base64.urlsafe_b64decode(
                encoded_digest + "=" * (-len(encoded_digest) % 4)
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("data artifact RECORD hash or size is malformed") from exc
        if algorithm.lower() != "sha256" or size < 0:
            raise ValueError("data artifact RECORD must use SHA256 and a valid size")
        digest = hashlib.sha256()
        actual_size = 0
        with candidate.open("rb") as artifact_file:
            for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
                digest.update(chunk)
                actual_size += len(chunk)
        if actual_size != size or digest.digest() != expected_digest:
            raise ValueError("data artifact RECORD file integrity mismatch")

    artifact_roots = [dist_info, site_root / "en_core_web_sm"]
    if any(
        path.is_symlink()
        for artifact_root in artifact_roots
        if artifact_root.is_dir()
        for path in artifact_root.rglob("*")
    ):
        raise ValueError("data artifact contains an ambiguous symlink")
    installed_files = {
        path.relative_to(site_root).as_posix()
        for artifact_root in artifact_roots
        if artifact_root.is_dir()
        for path in artifact_root.rglob("*")
        if path.is_file()
    }
    if installed_files != recorded_paths:
        raise ValueError("data artifact files do not exactly match RECORD")


def verify_data_artifact(profile: str, site_paths: Sequence[Path]) -> ArtifactVerification:
    """Verify the single allowlisted spaCy data wheel before audit exclusion."""
    started = time.monotonic()
    try:
        entries = _distribution_metadata_entries(site_paths)
        matches = [entry for entry in entries if entry[0] == EN_CORE_WEB_SM_NAME]
        if profile == "base":
            if matches:
                raise ValueError("base profile contains a local-only data artifact")
            return ArtifactVerification(
                StageResult(
                    "artifact_integrity",
                    "passed",
                    "no excluded data artifacts installed",
                    time.monotonic() - started,
                ),
                None,
                None,
            )
        policy = DATA_ARTIFACTS.get(EN_CORE_WEB_SM_NAME)
        if policy is None or profile not in policy["profiles"]:
            raise ValueError("profile is not allowed to exclude data artifacts")
        if len(matches) != 1:
            raise ValueError("local profile requires exactly one en-core-web-sm distribution")
        _, version, dist_info = matches[0]
        if version != policy["version"]:
            raise ValueError("en-core-web-sm version does not match the allowlist")
        direct_url_path = dist_info / "direct_url.json"
        try:
            direct_url = json.loads(direct_url_path.read_text(encoding="utf-8"))
            archive_info = direct_url["archive_info"]
            hashes = archive_info["hashes"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("en-core-web-sm direct_url.json is missing or malformed") from exc
        expected_hash = str(policy["sha256"])
        if _normalize_artifact_url(direct_url.get("url")) != _normalize_artifact_url(policy["url"]):
            raise ValueError("en-core-web-sm source URL does not match the allowlist")
        if not isinstance(hashes, dict) or hashes.get("sha256") != expected_hash:
            raise ValueError("en-core-web-sm archive SHA256 does not match the allowlist")
        if archive_info.get("hash") != f"sha256={expected_hash}":
            raise ValueError("en-core-web-sm archive hash declaration is incomplete")
        _verify_record(dist_info.parent, dist_info)
    except (OSError, ValueError) as exc:
        return ArtifactVerification(
            StageResult(
                "artifact_integrity",
                "failed",
                sanitize_output(exc),
                time.monotonic() - started,
            ),
            None,
            None,
        )
    return ArtifactVerification(
        StageResult(
            "artifact_integrity",
            "passed",
            "en-core-web-sm source and installed RECORD verified",
            time.monotonic() - started,
        ),
        dist_info,
        EN_CORE_WEB_SM_NAME,
    )


def _copy_validated_metadata_file(source: Path, target: Path, expected_size: int) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size != expected_size:
            raise ValueError("distribution metadata changed during copy")
        with (
            os.fdopen(descriptor, "rb", closefd=False) as source_file,
            target.open("xb") as target_file,
        ):
            copied = 0
            while copied < expected_size:
                chunk = source_file.read(min(1024 * 1024, expected_size - copied))
                if not chunk:
                    raise ValueError("distribution metadata changed during copy")
                target_file.write(chunk)
                copied += len(chunk)
            if source_file.read(1):
                raise ValueError("distribution metadata grew during copy")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)


def _copy_metadata_view(
    site_paths: Sequence[Path], destination: Path, excluded: Path | None
) -> set[str]:
    """Copy only a fully validated, bounded metadata inventory without links."""
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("distribution metadata view destination is not empty")
    copied_names: set[str] = set()
    planned_targets: set[str] = set()
    plans: list[tuple[str, Path, bool, list[Path], list[tuple[Path, Path, int]]]] = []
    file_count = 0
    byte_count = 0
    for name, _, metadata_path in _distribution_metadata_entries(site_paths):
        if excluded is not None and metadata_path.resolve() == excluded.resolve():
            continue
        if metadata_path.name in planned_targets or metadata_path.is_symlink():
            raise ValueError("distribution metadata view is ambiguous")
        planned_targets.add(metadata_path.name)
        directories: list[Path] = []
        files: list[tuple[Path, Path, int]] = []
        metadata_is_file = metadata_path.is_file()
        entries = [metadata_path] if metadata_is_file else list(metadata_path.rglob("*"))
        for entry in entries:
            entry_stat = entry.lstat()
            if stat.S_ISLNK(entry_stat.st_mode):
                raise ValueError("distribution metadata contains a symlink")
            relative = Path() if entry == metadata_path else entry.relative_to(metadata_path)
            if stat.S_ISDIR(entry_stat.st_mode):
                directories.append(relative)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise ValueError("distribution metadata contains a special file")
            file_count += 1
            byte_count += entry_stat.st_size
            if file_count > METADATA_VIEW_MAX_FILES or byte_count > METADATA_VIEW_MAX_BYTES:
                raise ValueError("distribution metadata view exceeds safety bounds")
            files.append((entry, relative, entry_stat.st_size))
        plans.append((name, metadata_path, metadata_is_file, directories, files))

    for name, metadata_path, metadata_is_file, directories, files in plans:
        target = destination / metadata_path.name
        if metadata_is_file:
            source, _, size = files[0]
            _copy_validated_metadata_file(source, target, size)
        else:
            target.mkdir()
            for relative in sorted(directories, key=lambda path: len(path.parts)):
                (target / relative).mkdir()
            for source, relative, size in files:
                _copy_validated_metadata_file(source, target / relative, size)
        copied_names.add(name)
    return copied_names


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _new_session_token() -> str:
    return uuid.uuid4().hex


def _assert_no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise SessionSafetyError(f"work directory contains a symlink: {current}")


def _prepare_work_parent(work_dir: Path | None) -> Path:
    if work_dir is None:
        parent = Path(tempfile.gettempdir()).resolve()
    else:
        parent = work_dir.expanduser().absolute()
        _assert_no_symlink_components(parent)
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionSafetyError("work directory could not be created safely") from exc
        _assert_no_symlink_components(parent)
    if not parent.is_dir() or parent.is_symlink():
        raise SessionSafetyError("work directory must be a real directory")
    return parent


def _create_owned_session(work_dir: Path | None) -> OwnedSession:
    parent = _prepare_work_parent(work_dir)
    session = parent / f"{SESSION_PREFIX}{_new_session_id()}"
    try:
        os.mkdir(session, 0o700)
    except FileExistsError as exc:
        raise SessionSafetyError(f"verifier session already exists: {session.name}") from exc
    except OSError as exc:
        raise SessionSafetyError("verifier session could not be created atomically") from exc
    token = _new_session_token()
    marker = session / SESSION_MARKER_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(marker, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker_file:
            json.dump({"schema_version": 1, "session": session.name, "token": token}, marker_file)
    except OSError as exc:
        try:
            session.rmdir()
        except OSError:
            pass
        raise SessionSafetyError("verifier ownership marker could not be created") from exc
    return OwnedSession(session, parent, token)


def _cleanup_owned_session(session: OwnedSession) -> None:
    path = session.path.absolute()
    if path.parent != session.parent.absolute() or not path.name.startswith(SESSION_PREFIX):
        raise SessionSafetyError("refusing cleanup outside the owned verifier session")
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise SessionSafetyError("owned verifier session disappeared before cleanup") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise SessionSafetyError("owned verifier session changed type before cleanup")
    marker = path / SESSION_MARKER_NAME
    try:
        marker_mode = os.lstat(marker).st_mode
        if stat.S_ISLNK(marker_mode) or not stat.S_ISREG(marker_mode):
            raise SessionSafetyError("verifier ownership marker is not a regular file")
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionSafetyError("verifier ownership marker could not be validated") from exc
    expected = {"schema_version": 1, "session": path.name, "token": session.token}
    if payload != expected:
        raise SessionSafetyError("verifier ownership marker does not match this process")
    quarantine = session.parent / f"{path.name}.cleanup-{session.token}"
    if quarantine.exists() or quarantine.is_symlink():
        raise SessionSafetyError("verifier cleanup quarantine already exists")
    try:
        os.rename(path, quarantine)
        moved_payload = json.loads((quarantine / SESSION_MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionSafetyError("verifier session changed during cleanup") from exc
    if moved_payload != expected:
        raise SessionSafetyError("verifier ownership changed during cleanup")
    shutil.rmtree(quarantine)


def _read_output_tail(output_file: Any, limit: int) -> tuple[str, bool]:
    output_file.flush()
    output_file.seek(0, os.SEEK_END)
    size = output_file.tell()
    output_file.seek(max(0, size - limit))
    content = output_file.read(limit).decode("utf-8", errors="replace")
    return content, size > limit


def _windows_taskkill_path() -> Path | None:
    system_root = os.environ.get("SystemRoot", "")
    if system_root:
        candidate = Path(system_root) / "System32" / "taskkill.exe"
        if candidate.is_absolute() and candidate.is_file():
            return candidate
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
        if 0 < length < len(buffer):
            candidate = Path(buffer.value) / "taskkill.exe"
            if candidate.is_absolute() and candidate.is_file():
                return candidate
    except (AttributeError, OSError):
        return None
    return None


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    else:
        taskkill = _windows_taskkill_path()
        if taskkill is not None:
            try:
                killer = subprocess.Popen(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
                killer.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def run_command(
    command: Sequence[str], *, cwd: Path, timeout: float, env: Mapping[str, str]
) -> CommandResult:
    """Run an argument-array command with bounded output and whole-tree timeout cleanup."""
    started = time.monotonic()
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
    ):
        popen_options: dict[str, Any] = {
            "cwd": cwd,
            "env": dict(env),
            "stdin": subprocess.DEVNULL,
            "stdout": stdout_file,
            "stderr": stderr_file,
            "shell": False,
        }
        if os.name == "nt":
            popen_options["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
            )
        else:
            popen_options["start_new_session"] = True
        try:
            process = subprocess.Popen(list(command), **popen_options)
        except OSError as exc:
            return CommandResult(127, "", sanitize_output(exc), time.monotonic() - started, False)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(process)

        output_limit = (
            COMMAND_OUTPUT_TAIL_CHARS
            if timed_out or process.returncode
            else COMMAND_OUTPUT_MAX_BYTES
        )
        stdout, stdout_truncated = _read_output_tail(stdout_file, output_limit)
        stderr, stderr_truncated = _read_output_tail(stderr_file, output_limit)
        if timed_out or process.returncode or stdout_truncated or stderr_truncated:
            stdout = sanitize_output(stdout, limit=COMMAND_OUTPUT_TAIL_CHARS)
            stderr = sanitize_output(stderr, limit=COMMAND_OUTPUT_TAIL_CHARS)
        if stdout_truncated or stderr_truncated:
            returncode = process.returncode if process.returncode else 70
            stderr = sanitize_output(
                f"command output exceeded safety bound; {stderr}",
                limit=COMMAND_OUTPUT_TAIL_CHARS,
            )
        else:
            returncode = process.returncode if process.returncode is not None else -1
        return CommandResult(
            returncode,
            stdout,
            stderr,
            time.monotonic() - started,
            timed_out,
        )


def clean_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a small non-secret environment shared by every profile subprocess."""
    original = os.environ if source is None else source
    allowed = (
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "WINDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
    )
    clean = {key: original[key] for key in allowed if original.get(key)}
    clean.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    return clean


def _safe_summary(result: CommandResult, success: str) -> str:
    if result.timed_out:
        return "command timed out"
    if result.returncode == 0:
        return sanitize_output(success)
    diagnostic_text = (result.stderr or result.stdout or "command failed").strip()
    diagnostic = diagnostic_text.splitlines()[-1] if diagnostic_text else "command failed"
    return sanitize_output(diagnostic)


def _stage(name: str, result: CommandResult, success: str) -> StageResult:
    return StageResult(
        name,
        "passed" if result.returncode == 0 and not result.timed_out else "failed",
        _safe_summary(result, success),
        result.duration,
    )


def _parse_date(value: Any, field: str, index: int) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ExceptionFileError(f"exception {index} has malformed {field}") from exc


def load_audit_exceptions(
    path: Path = DEFAULT_EXCEPTIONS_FILE, *, today: date | None = None
) -> tuple[AuditException, ...]:
    """Load and strictly validate time-bounded, advisory-specific exceptions."""
    current = today or date.today()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExceptionFileError(f"cannot read audit exceptions: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ExceptionFileError("audit exceptions require schema_version 1")
    entries = payload.get("exceptions")
    if not isinstance(entries, list):
        raise ExceptionFileError("audit exceptions must be a list")

    parsed: list[AuditException] = []
    required = {
        "advisory_id",
        "aliases",
        "package",
        "profile",
        "owner",
        "reason",
        "created",
        "expires",
    }
    for index, item in enumerate(entries):
        if not isinstance(item, dict) or set(item) != required:
            raise ExceptionFileError(f"exception {index} has malformed fields")
        if not all(
            isinstance(item[key], str) and item[key].strip() for key in required - {"aliases"}
        ):
            raise ExceptionFileError(f"exception {index} has empty or malformed values")
        aliases = item["aliases"]
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias for alias in aliases
        ):
            raise ExceptionFileError(f"exception {index} has malformed aliases")
        key = (item["profile"].lower(), item["package"].lower(), item["advisory_id"])
        if key not in RECOGNIZED_EXCEPTIONS:
            raise ExceptionFileError(f"exception {index} references an unrecognized advisory")
        if tuple(aliases) != RECOGNIZED_EXCEPTIONS[key]:
            raise ExceptionFileError(f"exception {index} has unrecognized advisory aliases")
        created = _parse_date(item["created"], "created", index)
        expires = _parse_date(item["expires"], "expires", index)
        if created > expires:
            raise ExceptionFileError(f"exception {index} expires before it was created")
        if created > current:
            raise ExceptionFileError(f"exception {index} has a future created date")
        if expires < current:
            raise ExceptionFileError(f"exception {index} expired on {expires.isoformat()}")
        parsed.append(
            AuditException(
                item["advisory_id"],
                tuple(aliases),
                item["package"].lower(),
                item["profile"].lower(),
                item["owner"],
                item["reason"],
                created,
                expires,
            )
        )
    return tuple(parsed)


def _audit_vulnerabilities(
    payload: Any,
) -> list[tuple[str, str, frozenset[str], tuple[str, ...]]]:
    if isinstance(payload, list):
        dependencies = payload
    elif isinstance(payload, dict):
        errors = payload.get("errors", [])
        if errors:
            raise ValueError("pip-audit dependency collection was incomplete")
        dependencies = payload.get("dependencies")
        if not isinstance(dependencies, list) or not isinstance(payload.get("fixes", []), list):
            raise ValueError("pip-audit JSON has a malformed report envelope")
    else:
        raise ValueError("pip-audit JSON must be a report object")
    found: list[tuple[str, str, frozenset[str], tuple[str, ...]]] = []
    for package in dependencies:
        if not isinstance(package, dict):
            raise ValueError("pip-audit package entry is malformed")
        name = _canonical_package_name(package.get("name", ""))
        version = package.get("version")
        if not name or not isinstance(version, str) or not version.strip():
            raise ValueError("pip-audit package identity is incomplete")
        if package.get("skip_reason"):
            raise ValueError(f"pip-audit skipped distribution: {name}")
        vulnerabilities = package.get("vulns")
        if not isinstance(vulnerabilities, list):
            raise ValueError(f"pip-audit did not audit distribution: {name}")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict) or not vulnerability.get("id"):
                raise ValueError("pip-audit vulnerability entry is malformed")
            identifiers = frozenset(
                (str(vulnerability["id"]), *(str(v) for v in vulnerability.get("aliases", [])))
            )
            fixes = tuple(str(version) for version in vulnerability.get("fix_versions", []))
            found.append((name, str(vulnerability["id"]), identifiers, fixes))
    return found


def _audit_distribution_names(payload: Any) -> set[str]:
    """Return audited names after enforcing the same strict report completeness."""
    _audit_vulnerabilities(payload)
    dependencies = payload if isinstance(payload, list) else payload["dependencies"]
    return {_canonical_package_name(package["name"]) for package in dependencies}


def evaluate_audit(profile: str, payload: Any, exceptions: Sequence[AuditException]) -> StageResult:
    """Evaluate exact audit findings; an exception is visible accepted risk."""
    try:
        vulnerabilities = _audit_vulnerabilities(payload)
    except ValueError as exc:
        return StageResult("audit", "failed", sanitize_output(exc))
    if not vulnerabilities:
        return StageResult("audit", "passed", "zero known vulnerabilities")
    accepted: list[str] = []
    unexpected: list[str] = []
    for package, advisory, identifiers, fixes in vulnerabilities:
        match = next(
            (
                exception
                for exception in exceptions
                if exception.profile == profile
                and exception.package == package
                and bool(exception.identifiers & identifiers)
            ),
            None,
        )
        if match and not fixes:
            accepted.append(f"{package}:{advisory}")
        elif match:
            unexpected.append(
                f"{package}:{advisory} (fixed in {', '.join(fixes)}; remove exception and upgrade)"
            )
        else:
            unexpected.append(f"{package}:{advisory}")
    if unexpected:
        return StageResult(
            "audit",
            "failed",
            sanitize_output("unexpected vulnerabilities: " + ", ".join(unexpected)),
        )
    return StageResult(
        "audit",
        "accepted_risk",
        sanitize_output("accepted risk (release gate is not clean): " + ", ".join(accepted)),
    )


def hardware_preflight(
    profile: str,
    *,
    runner: Runner = run_command,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """Check OS/CPU and, for CUDA, the NVIDIA driver before installing."""
    runtime = get_runtime_profile(profile)
    if not runtime.is_compatible():
        return False, runtime.incompatibility_reason() or "incompatible platform"
    if profile != "cuda":
        return True, "available"
    result = runner(
        ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
        cwd=REPO_ROOT,
        timeout=DEFAULT_COMMAND_TIMEOUT,
        env=dict(env or clean_environment()),
    )
    if result.timed_out:
        return False, "NVIDIA driver preflight timed out"
    if result.returncode != 0 or not result.stdout.strip():
        return False, "no usable NVIDIA GPU/driver was detected"
    return True, "available"


def _venv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")


def _run_profile(
    profile: str,
    *,
    root: Path,
    runner: Runner,
    source_python: Path,
    command_timeout: float,
    install_timeout: float,
    exceptions: Sequence[AuditException],
) -> ProfileResult:
    stages: list[StageResult] = []
    runtime = get_runtime_profile(profile)
    environment = root / profile
    if environment.exists() or environment.is_symlink():
        raise SessionSafetyError("profile environment unexpectedly already exists")
    clean_env = clean_environment()

    available, reason = hardware_preflight(profile, runner=runner, env=clean_env)
    if not available:
        return ProfileResult(profile, "skipped", reason, ())

    commands: list[tuple[str, list[str], float, str]] = [
        (
            "create_environment",
            [str(source_python), "-m", "venv", str(environment)],
            command_timeout,
            "fresh virtual environment created",
        ),
    ]
    venv_python = _venv_python(environment)
    commands.extend(
        [
            (
                "bootstrap_tooling",
                [str(venv_python), "-m", "pip", "install", "--upgrade", *TOOLING_PINS],
                install_timeout,
                "pinned packaging tools installed",
            ),
            (
                "install",
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-compile",
                    "-r",
                    str(runtime.requirements_file),
                ],
                install_timeout,
                "profile manifest installed",
            ),
            (
                "pip_check",
                [str(venv_python), "-m", "pip", "check"],
                command_timeout,
                "dependency graph is consistent",
            ),
            (
                "import_smoke",
                [str(venv_python), "-B", "-I", "-c", SMOKE_CODE[profile]],
                command_timeout,
                "profile imports and no-download API checks passed",
            ),
            (
                "prohibited_packages",
                [
                    str(venv_python),
                    "-B",
                    "-I",
                    "-c",
                    "import importlib.metadata as m,json; print(json.dumps(sorted({d.metadata['Name'].lower() for d in m.distributions()})))",
                ],
                command_timeout,
                "package inventory captured",
            ),
        ]
    )
    inventory: list[str] = []
    canonical_inventory: set[str] = set()
    for name, command, timeout, success in commands:
        result = runner(command, cwd=REPO_ROOT, timeout=timeout, env=clean_env)
        stage = _stage(name, result, success)
        if name == "prohibited_packages" and stage.status == "passed":
            try:
                inventory = json.loads(result.stdout)
                if not isinstance(inventory, list) or not all(
                    isinstance(package, str) for package in inventory
                ):
                    raise TypeError
                canonical_inventory = {_canonical_package_name(package) for package in inventory}
                prohibited = sorted(PROHIBITED_PACKAGES[profile] & canonical_inventory)
            except (json.JSONDecodeError, TypeError):
                stage = StageResult(
                    name, "failed", "package inventory was not valid JSON", result.duration
                )
            else:
                stage = StageResult(
                    name,
                    "failed" if prohibited else "passed",
                    "prohibited packages installed: " + ", ".join(prohibited)
                    if prohibited
                    else "prohibited packages are absent",
                    result.duration,
                )
        stages.append(stage)
        if stage.status == "failed":
            return ProfileResult(profile, "failed", stage.summary, tuple(stages), str(environment))

    site_result = runner(
        [
            str(venv_python),
            "-B",
            "-I",
            "-c",
            "import json,site; print(json.dumps(site.getsitepackages()))",
        ],
        cwd=REPO_ROOT,
        timeout=command_timeout,
        env=clean_env,
    )
    try:
        raw_site_paths = json.loads(site_result.stdout) if site_result.returncode == 0 else []
        if (
            not isinstance(raw_site_paths, list)
            or not raw_site_paths
            or not all(isinstance(path, str) and path for path in raw_site_paths)
        ):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        artifact_stage = StageResult(
            "artifact_integrity",
            "failed",
            "could not resolve target site-packages",
            site_result.duration,
        )
        stages.append(artifact_stage)
        return ProfileResult(
            profile, "failed", artifact_stage.summary, tuple(stages), str(environment)
        )
    else:
        site_paths = [Path(path).resolve() for path in raw_site_paths]

    artifact = verify_data_artifact(profile, site_paths)
    stages.append(artifact.stage)
    if artifact.stage.status == "failed":
        return ProfileResult(
            profile, "failed", artifact.stage.summary, tuple(stages), str(environment)
        )

    expected_audit_names = canonical_inventory - (
        {artifact.excluded_name} if artifact.excluded_name else set()
    )
    with tempfile.TemporaryDirectory(
        prefix=f"{profile}-", suffix="-audit-metadata", dir=root
    ) as metadata_directory:
        metadata_view = Path(metadata_directory)
        try:
            copied_names = _copy_metadata_view(
                site_paths, metadata_view, artifact.excluded_metadata
            )
        except (OSError, ValueError) as exc:
            audit_stage = StageResult("audit", "failed", sanitize_output(exc))
        else:
            if copied_names != expected_audit_names:
                audit_stage = StageResult(
                    "audit",
                    "failed",
                    "audit metadata coverage does not match installed distributions",
                )
            else:
                audit_command = [
                    str(source_python),
                    "-m",
                    "pip_audit",
                    "--path",
                    str(metadata_view),
                    "--format",
                    "json",
                    "--strict",
                    "--progress-spinner",
                    "off",
                ]
                audit_result = runner(
                    audit_command,
                    cwd=REPO_ROOT,
                    timeout=command_timeout,
                    env=clean_env,
                )
                if audit_result.timed_out or audit_result.returncode not in {0, 1}:
                    audit_stage = StageResult(
                        "audit", "failed", _safe_summary(audit_result, ""), audit_result.duration
                    )
                else:
                    try:
                        audit_payload = json.loads(audit_result.stdout)
                        audited_names = _audit_distribution_names(audit_payload)
                    except json.JSONDecodeError as exc:
                        summary = (
                            _safe_summary(audit_result, "")
                            if not audit_result.stdout.strip() and audit_result.stderr.strip()
                            else sanitize_output(exc)
                        )
                        audit_stage = StageResult(
                            "audit",
                            "failed",
                            summary,
                            audit_result.duration,
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        audit_stage = StageResult(
                            "audit",
                            "failed",
                            sanitize_output(exc or "pip-audit returned invalid JSON"),
                            audit_result.duration,
                        )
                    else:
                        if audited_names != expected_audit_names:
                            audit_stage = StageResult(
                                "audit",
                                "failed",
                                "pip-audit coverage does not match installed distributions",
                                audit_result.duration,
                            )
                        else:
                            evaluated = evaluate_audit(profile, audit_payload, exceptions)
                            audit_stage = StageResult(
                                evaluated.name,
                                evaluated.status,
                                evaluated.summary,
                                audit_result.duration,
                            )
    stages.append(audit_stage)
    status = (
        "failed"
        if audit_stage.status == "failed"
        else ("accepted_risk" if audit_stage.status == "accepted_risk" else "passed")
    )
    return ProfileResult(profile, status, audit_stage.summary, tuple(stages), str(environment))


def verify_profiles(
    profiles: Sequence[str],
    *,
    runner: Runner = run_command,
    source_python: Path | None = None,
    work_dir: Path | None = None,
    keep_env: bool = False,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    install_timeout: float = DEFAULT_INSTALL_TIMEOUT,
    exceptions_file: Path = DEFAULT_EXCEPTIONS_FILE,
) -> VerificationReport:
    """Verify every requested profile in a separate, clean virtual environment."""
    exceptions = load_audit_exceptions(exceptions_file)
    requested = tuple(dict.fromkeys(profiles))
    for profile in requested:
        get_runtime_profile(profile)
    session = _create_owned_session(work_dir)
    selected_python = (source_python or Path(sys.executable)).expanduser().absolute()
    try:
        results = tuple(
            _run_profile(
                profile,
                root=session.path,
                runner=runner,
                source_python=selected_python,
                command_timeout=command_timeout,
                install_timeout=install_timeout,
                exceptions=exceptions,
            )
            for profile in requested
        )
        return VerificationReport(
            results,
            str(session.path) if keep_env else None,
        )
    finally:
        if not keep_env:
            _cleanup_owned_session(session)


def validate_python(path: Path) -> Path:
    preserved = path.expanduser().absolute()
    try:
        executable = preserved.is_file() and os.access(preserved, os.X_OK)
    except (OSError, ValueError):
        executable = False
    if not executable:
        raise ValueError(f"Python executable is not executable: {preserved}")
    result = run_command(
        [str(preserved), "--version"],
        cwd=REPO_ROOT,
        timeout=DEFAULT_COMMAND_TIMEOUT,
        env=clean_environment(),
    )
    if result.returncode != 0 or result.timed_out:
        raise ValueError(f"Python executable validation failed: {preserved}")
    return preserved


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        action="append",
        choices=("base", "mlx", "cuda"),
        required=True,
        help="Verify exactly this profile; repeat to verify a release matrix.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Developer-only source interpreter used to create clean environments.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    parser.add_argument(
        "--allow-skipped",
        action="store_true",
        help="Return success when requested hardware profiles are unavailable.",
    )
    parser.add_argument(
        "--keep-env", action="store_true", help="Keep profile environments for diagnosis."
    )
    parser.add_argument("--work-dir", type=Path, help="Directory for clean profile environments.")
    parser.add_argument("--command-timeout", type=float, default=DEFAULT_COMMAND_TIMEOUT)
    parser.add_argument("--install-timeout", type=float, default=DEFAULT_INSTALL_TIMEOUT)
    return parser.parse_args(argv)


def _print_human(report: VerificationReport) -> None:
    for profile in report.profiles:
        print(sanitize_output(f"{profile.profile}: {profile.status} - {profile.reason}"))
        for stage in profile.stages:
            print(
                sanitize_output(
                    f"  {stage.name}: {stage.status} ({stage.duration:.1f}s) - {stage.summary}"
                )
            )
    if report.has_accepted_risk:
        print("RELEASE GATE: accepted risk recorded; this is not a clean vulnerability pass.")
    if report.session_directory:
        print(sanitize_output(f"Retained verifier session: {report.session_directory}"))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command_timeout <= 0 or args.install_timeout <= 0:
        raise SystemExit("timeouts must be positive")
    try:
        source_python = validate_python(args.python)
        report = verify_profiles(
            args.profile,
            source_python=source_python,
            work_dir=args.work_dir,
            keep_env=args.keep_env,
            command_timeout=args.command_timeout,
            install_timeout=args.install_timeout,
        )
    except (ValueError, ExceptionFileError, SessionSafetyError) as exc:
        if args.json:
            print(json.dumps({"error": sanitize_output(exc)}, sort_keys=True))
        else:
            print(f"verification error: {sanitize_output(exc)}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        _print_human(report)
    return report.exit_code(allow_skipped=args.allow_skipped)


if __name__ == "__main__":
    raise SystemExit(main())
