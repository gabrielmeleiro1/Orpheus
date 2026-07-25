"""Safe evidence directory creation, atomic writes, and log redaction."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_EVIDENCE_ROOT = Path(".artifacts") / "release-qualification"
SENTINEL_NAME = ".orpheus-release-qualification"

_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_BEARER_TERMINATED_RE = re.compile(r"(Bearer\s+)\S+(?=\s)", re.IGNORECASE)
_URL_CREDENTIAL_RE = re.compile(r"(https?://)([^/@\s]+):([^/@\s]+)@")
_API_KEY_RE = re.compile(
    r"(?i)(api[_-]?key\s*[=:]\s*)([A-Za-z0-9_\-]{16,})"
)
_API_KEY_TERMINATED_RE = re.compile(
    r"(?i)(api[_-]?key\s*[=:]\s*)([A-Za-z0-9_\-]{16,})(?=[^A-Za-z0-9_\-])"
)
_AWS_KEY_RE = re.compile(r"\b(AKIA[0-9A-Z]{16})\b")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _assert_safe_base(base: Path) -> Path:
    resolved = base.resolve()
    allowed_root = (_repo_root() / DEFAULT_EVIDENCE_ROOT).resolve()
    if resolved != allowed_root and not resolved.is_relative_to(allowed_root):
        raise ValueError(
            "unsafe evidence base: must be under repository .artifacts/release-qualification"
        )
    return resolved


@dataclass(frozen=True)
class EvidenceRoot:
    path: Path
    release_id: str
    expected_root: Path

    @classmethod
    def create(
        cls,
        *,
        release_id: str,
        base: Path,
        release_dirname: str | None = None,
    ) -> EvidenceRoot:
        if not release_id.strip():
            raise ValueError("release_id must be non-empty")
        expected_root = _assert_safe_base(base)
        expected_root.mkdir(parents=True, exist_ok=True)

        dirname = release_dirname or f"{release_id}-{uuid4().hex[:8]}"
        candidate = (expected_root / dirname).resolve()
        if not candidate.is_relative_to(expected_root):
            raise ValueError("unsafe evidence path: traversal outside expected root")

        candidate.mkdir(parents=False, exist_ok=False)
        sentinel = candidate / SENTINEL_NAME
        sentinel.write_text(f"{release_id}\n", encoding="utf-8")
        return cls(path=candidate, release_id=release_id, expected_root=expected_root)

    def cleanup(self) -> None:
        path = self.path.resolve()
        expected_root = self.expected_root.resolve()
        allowed_root = (_repo_root() / DEFAULT_EVIDENCE_ROOT).resolve()
        if expected_root != allowed_root and not expected_root.is_relative_to(allowed_root):
            raise ValueError(
                "refuse cleanup: expected root outside repository .artifacts/release-qualification"
            )
        if not path.is_relative_to(expected_root):
            raise ValueError("refuse cleanup: path outside expected root")
        if path == expected_root:
            raise ValueError("refuse cleanup: refusing to remove expected root itself")

        sentinel = path / SENTINEL_NAME
        if not sentinel.is_file():
            raise ValueError("refuse cleanup: sentinel missing")
        contents = sentinel.read_text(encoding="utf-8").strip()
        if contents != self.release_id:
            raise ValueError("refuse cleanup: sentinel mismatched")

        shutil.rmtree(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def redact_secrets(
    text: str,
    *,
    username: str | None = None,
    final: bool = True,
) -> str:
    bearer_re = _BEARER_RE if final else _BEARER_TERMINATED_RE
    api_key_re = _API_KEY_RE if final else _API_KEY_TERMINATED_RE
    redacted = bearer_re.sub(r"\1[REDACTED]", text)
    redacted = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]:[REDACTED]@", redacted)
    redacted = api_key_re.sub(r"\1[REDACTED]", redacted)
    redacted = _AWS_KEY_RE.sub("[REDACTED]", redacted)
    user = username if username is not None else (os.environ.get("USER") or os.environ.get("USERNAME") or "")
    if user:
        redacted = _redact_username(redacted, user)
    return redacted


class StreamingSecretRedactor:
    """Redact secrets across chunk boundaries with a bounded carry buffer."""

    MAX_CARRY = 4096

    def __init__(self, *, username: str | None = None) -> None:
        self._username = username
        self._carry = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._carry += text
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        if not self._carry:
            return ""
        redacted = redact_secrets(
            self._carry, username=self._username, final=final
        )
        if final:
            self._carry = ""
            return redacted
        if len(redacted) <= self.MAX_CARRY:
            self._carry = redacted
            return ""
        emit, self._carry = redacted[: -self.MAX_CARRY], redacted[-self.MAX_CARRY :]
        return emit


def _redact_username(text: str, user: str) -> str:
    """Redact username only in path/home contexts, never as a raw substring."""
    escaped = re.escape(user)
    # Home/profile path segments: /Users/<user>, /home/<user>, C:\Users\<user>
    text = re.sub(
        rf"(^|[/\\])Users[/\\]{escaped}(?=[/\\]|$)",
        r"\1Users/[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"(^|[/\\])home[/\\]{escaped}(?=[/\\]|$)",
        r"\1home/[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text
