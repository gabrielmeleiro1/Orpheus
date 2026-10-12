"""Write and verify model snapshot download receipts."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat as stat_module
import threading
import time
from typing import Any, Iterable

from modules.contracts.durable_files import atomic_write_json, sync_directory_if_openable
from modules.runtime_setup.catalog import _normalize_model_id, _REVISION_RE

DOWNLOAD_RECEIPT_FILENAME = ".orpheus-download-complete.json"
DOWNLOAD_RECEIPT_SCHEMA_VERSION = 2
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_RECEIPT_BYTES = 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
WINDOWS_RECEIPT_CACHE_SETTLE_SECONDS = 2.0
_WINDOWS = os.name == "nt"
_now_ns = time.time_ns


@dataclass(frozen=True)
class _ReceiptFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class _ParsedReceipt:
    snapshot: Path
    model_id: str
    revision: str
    files: tuple[_ReceiptFile, ...]
    receipt_identity: tuple[int, ...]


@dataclass(frozen=True)
class _SnapshotFingerprint:
    identity: tuple[Any, ...]
    cacheable: bool


def _is_transient_path(relative_path: PurePosixPath) -> bool:
    parts = relative_path.parts
    return bool(parts) and (
        parts[0] in {".cache", ".locks"}
        or relative_path.name == DOWNLOAD_RECEIPT_FILENAME
        or relative_path.name.endswith((".lock", ".incomplete"))
    )


def _resolved_allowed_file_target(
    snapshot_path: Path,
    file_path: Path,
    *,
    resolved_snapshot_root: Path | None = None,
) -> Path | None:
    try:
        snapshot_root = (
            snapshot_path.resolve(strict=True)
            if resolved_snapshot_root is None
            else resolved_snapshot_root
        )
        target = file_path.resolve(strict=True)
    except OSError:
        return None
    if not target.is_file():
        return None
    if target.is_relative_to(snapshot_root):
        return target
    if snapshot_path.parent.name != "snapshots":
        return None
    repo_root = snapshot_path.parent.parent
    # huggingface_hub 1.x links large files from the repo's blobs/ to one
    # store shared by the whole cache (<cache>/blobs/<prefix>/<hash>).
    allowed_roots = (repo_root / "blobs", repo_root.parent / "blobs")
    for root in allowed_roots:
        if target.is_relative_to(root.resolve()):
            return target
    return None


def _allowed_file_target(
    snapshot_path: Path,
    file_path: Path,
    *,
    resolved_snapshot_root: Path | None = None,
) -> bool:
    return _resolved_allowed_file_target(
        snapshot_path, file_path, resolved_snapshot_root=resolved_snapshot_root,
    ) is not None


def _snapshot_root_unchanged(root: Path, before: os.stat_result) -> bool:
    """Reject a replaced walk root without rejecting ignored transient churn."""
    if root.resolve(strict=True) != root:
        return False
    after = root.stat()
    # Directory timestamps can change when ignored lock/cache entries appear.
    # The full initial stat remains in the fingerprint; this is a binding check.
    return (
        before.st_dev, before.st_ino, stat_module.S_IFMT(before.st_mode),
    ) == (
        after.st_dev, after.st_ino, stat_module.S_IFMT(after.st_mode),
    )


def _file_size_and_sha256(file_path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
        size = os.fstat(handle.fileno()).st_size
    return size, digest.hexdigest()


def _snapshot_manifest(snapshot_path: Path) -> list[dict[str, object]]:
    root = snapshot_path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Model snapshot is not a directory.")
    root_stat = root.stat()
    # Filesystems without a directory identity retain per-file root resolution.
    reusable_root = root if root_stat.st_ino else None
    manifest: list[dict[str, object]] = []
    for file_path in sorted(root.rglob("*")):
        relative = file_path.relative_to(root)
        relative_posix = PurePosixPath(relative.as_posix())
        if _is_transient_path(relative_posix):
            continue
        if file_path.is_symlink() and not _allowed_file_target(
            root, file_path, resolved_snapshot_root=reusable_root,
        ):
            raise ValueError("Model snapshot contains an unsafe or missing file.")
        if file_path.is_dir():
            continue
        target = _resolved_allowed_file_target(
            root, file_path, resolved_snapshot_root=reusable_root,
        )
        if target is None:
            raise ValueError("Model snapshot contains an unsafe or missing file.")
        size, sha256 = _file_size_and_sha256(target)
        manifest.append(
            {
                "path": relative_posix.as_posix(),
                "size": size,
                "sha256": sha256,
            }
        )
    if not manifest:
        raise ValueError("Model snapshot contains no completed files.")
    if not _snapshot_root_unchanged(root, root_stat):
        raise ValueError("Model snapshot root changed while inspecting files.")
    # Path ordering is case-insensitive on Windows; receipt parsing sorts POSIX
    # strings. Use the same canonical order on every platform, including when
    # validating receipts written by older Windows builds.
    return sorted(manifest, key=lambda entry: str(entry["path"]))


def _remove_snapshot_entry(path: Path) -> None:
    """Remove a snapshot entry itself, retaining any linked blob or directory."""
    if path.is_symlink():
        path.unlink()
    elif getattr(path, "is_junction", lambda: False)():
        path.rmdir()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _remove_unexpected_snapshot_entries(
    snapshot: Path, expected_files: Iterable[str] = (),
) -> None:
    """Remove untrusted snapshot entries while retaining Hub metadata and receipts."""
    expected = {Path(path) for path in expected_files}
    directories = {parent for path in expected for parent in path.parents}

    def clean(directory: Path) -> None:
        for path in directory.iterdir():
            relative = path.relative_to(snapshot)
            if _is_transient_path(PurePosixPath(relative.as_posix())):
                continue
            if relative in expected:
                continue
            if (
                relative in directories
                and not path.is_symlink()
                and not getattr(path, "is_junction", lambda: False)()
                and path.is_dir()
            ):
                clean(path)
            else:
                _remove_snapshot_entry(path)

    clean(snapshot)


def _revision_identity(snapshot_path: Path, manifest: list[dict[str, object]]) -> str:
    candidate = snapshot_path.name.lower()
    if snapshot_path.parent.name == "snapshots" and _REVISION_RE.fullmatch(candidate):
        return candidate
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_download_receipt(
    snapshot_path: Path,
    *,
    model_id: str,
    revision: str | None = None,
) -> Path:
    """Atomically record a complete local snapshot after download succeeds."""
    normalized_model_id = _normalize_model_id(model_id)
    snapshot = snapshot_path.resolve(strict=True)
    manifest = _snapshot_manifest(snapshot)
    normalized_revision = (
        str(revision).strip().lower()
        if revision is not None
        else _revision_identity(snapshot, manifest)
    )
    if not _REVISION_RE.fullmatch(normalized_revision):
        raise ValueError("Invalid model snapshot revision identity.")
    payload = {
        "schema_version": DOWNLOAD_RECEIPT_SCHEMA_VERSION,
        "model_id": normalized_model_id,
        "revision": normalized_revision,
        "files": manifest,
    }
    receipt_path = snapshot / DOWNLOAD_RECEIPT_FILENAME
    atomic_write_json(
        receipt_path, payload, sort_keys=True, separators=(",", ":"), trailing_newline=True,
        permissions=0o600, sync_parent=sync_directory_if_openable, cleanup="before_replace",
    )
    return receipt_path


def _valid_receipt_relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    # On Windows a colon names a drive ("Z:file" joins outside the snapshot)
    # or an alternate data stream; no Windows file name can contain one.
    if os.name == "nt" and ":" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if _is_transient_path(path):
        return None
    return path.as_posix()


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(getattr(value, "st_dev", 0)),
        int(getattr(value, "st_ino", 0)),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


@lru_cache(maxsize=1)
def _windows_basic_info_api():
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_longlong)
            for name in ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")
        ] + [("FileAttributes", wintypes.DWORD)]

    read_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    read_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    read_info.restype = wintypes.BOOL
    return read_info, FileBasicInfo


def _windows_change_time_ns(descriptor: int) -> int | None:
    """Read mutation time; Python 3.12's Windows st_ctime is creation time."""
    try:
        import ctypes
        import msvcrt

        read_info, info_type = _windows_basic_info_api()
        info = info_type()
        if not read_info(msvcrt.get_osfhandle(descriptor), 0, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        # FILETIME counts 100ns intervals since 1601, rather than Unix time.
        value = int(info.ChangeTime)
        return (value - 116_444_736_000_000_000) * 100 if value > 0 else None
    except (AttributeError, ImportError, OSError):
        return None


def _descriptor_identity(descriptor: int, value: os.stat_result) -> tuple[int, ...]:
    identity = _stat_identity(value)
    if _WINDOWS:
        changed = _windows_change_time_ns(descriptor)
        identity += (changed if changed is not None else -1,)
    return identity


def _target_identity(path: Path) -> tuple[int, ...]:
    if not _WINDOWS:
        return _stat_identity(path.stat())
    with path.open("rb") as handle:
        return _descriptor_identity(handle.fileno(), os.fstat(handle.fileno()))


def _stable_identity(identity: tuple[int, ...], cutoff_ns: int) -> bool:
    if not _WINDOWS:
        return True
    # NTFS can stamp rapid same-size writes with exactly the same WriteTime
    # AND ChangeTime. Never trust metadata alone while those ticks are recent.
    # Future timestamps and filesystems without ChangeTime also keep hashing.
    return len(identity) == 7 and identity[6] >= 0 and max(identity[4:]) < cutoff_ns


def _read_receipt_json(receipt_path: Path) -> tuple[Any, tuple[int, ...]] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(receipt_path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        before_identity = _descriptor_identity(descriptor, before)
        if not stat_module.S_ISREG(before.st_mode) or before.st_size > _MAX_RECEIPT_BYTES:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_RECEIPT_BYTES + 1)
        after = os.fstat(descriptor)
        after_identity = _descriptor_identity(descriptor, after)
        if len(raw) > _MAX_RECEIPT_BYTES or before_identity != after_identity:
            return None
    finally:
        os.close(descriptor)
    try:
        return json.loads(raw), after_identity
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        # Deep nesting exhausts the decoder's recursion limit; it is as
        # untrustworthy as any other malformed receipt.
        return None


def _parse_download_receipt(
    snapshot_path: Path,
    *,
    model_id: str,
    revision: str,
) -> _ParsedReceipt | None:
    try:
        snapshot = snapshot_path.resolve(strict=True)
        if not snapshot.is_dir():
            return None
        normalized_model_id = _normalize_model_id(model_id)
        normalized_revision = str(revision).strip().lower()
        if not _REVISION_RE.fullmatch(normalized_revision):
            return None
        receipt_path = snapshot / DOWNLOAD_RECEIPT_FILENAME
        if receipt_path.is_symlink():
            return None
        loaded = _read_receipt_json(receipt_path)
        if loaded is None:
            return None
        payload, receipt_identity = loaded
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "model_id", "revision", "files"}
            or payload.get("schema_version") != DOWNLOAD_RECEIPT_SCHEMA_VERSION
            or payload.get("model_id") != normalized_model_id
            or payload.get("revision") != normalized_revision
        ):
            return None
        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            return None
        files: list[_ReceiptFile] = []
        seen: set[str] = set()
        for entry in raw_files:
            if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
                return None
            relative = _valid_receipt_relative_path(entry.get("path"))
            size = entry.get("size")
            sha256 = entry.get("sha256")
            if (
                relative is None
                or relative in seen
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(sha256, str)
                or not _SHA256_RE.fullmatch(sha256)
            ):
                return None
            seen.add(relative)
            files.append(_ReceiptFile(relative, size, sha256))
        files.sort(key=lambda item: item.path)
        return _ParsedReceipt(
            snapshot=snapshot,
            model_id=normalized_model_id,
            revision=normalized_revision,
            files=tuple(files),
            receipt_identity=receipt_identity,
        )
    except (OSError, TypeError, ValueError):
        return None


def _snapshot_metadata_fingerprint(parsed: _ParsedReceipt) -> _SnapshotFingerprint | None:
    try:
        cutoff_ns = _now_ns() - int(WINDOWS_RECEIPT_CACHE_SETTLE_SECONDS * 1_000_000_000)
        cacheable = _stable_identity(parsed.receipt_identity, cutoff_ns)
        snapshot_root = parsed.snapshot.resolve(strict=True)
        if snapshot_root != parsed.snapshot:
            return None
        root_stat = parsed.snapshot.stat()
        root_identity = _stat_identity(root_stat)
        reusable_root = snapshot_root if root_stat.st_ino else None
        expected_files = {item.path: item for item in parsed.files}
        expected_paths = set(expected_files)
        actual: list[tuple[Any, ...]] = []
        for file_path in sorted(parsed.snapshot.rglob("*")):
            relative = PurePosixPath(file_path.relative_to(parsed.snapshot).as_posix())
            if _is_transient_path(relative):
                continue
            if file_path.is_symlink() and not _allowed_file_target(
                parsed.snapshot, file_path, resolved_snapshot_root=reusable_root,
            ):
                return None
            if file_path.is_dir():
                continue
            relative_text = relative.as_posix()
            if relative_text not in expected_paths:
                return None
            target = _resolved_allowed_file_target(
                parsed.snapshot, file_path, resolved_snapshot_root=reusable_root,
            )
            if target is None:
                return None
            lexical_stat = file_path.lstat()
            target_identity = _target_identity(target)
            cacheable = cacheable and _stable_identity(target_identity, cutoff_ns)
            link_identity = (
                hashlib.sha256(os.fsencode(os.readlink(file_path))).hexdigest()
                if file_path.is_symlink()
                else None
            )
            receipt_file = expected_files[relative_text]
            actual.append(
                (
                    relative_text,
                    receipt_file.size,
                    receipt_file.sha256,
                    _stat_identity(lexical_stat),
                    target_identity,
                    link_identity,
                )
            )
        if {item[0] for item in actual} != expected_paths:
            return None
        if not _snapshot_root_unchanged(snapshot_root, root_stat):
            return None
        return _SnapshotFingerprint((
            DOWNLOAD_RECEIPT_SCHEMA_VERSION,
            parsed.model_id,
            parsed.revision,
            parsed.receipt_identity,
            root_identity,
            tuple(actual),
        ), cacheable=cacheable)
    except (OSError, ValueError):
        return None


def _validate_parsed_receipt(parsed: _ParsedReceipt) -> bool:
    recorded = [
        {"path": item.path, "size": item.size, "sha256": item.sha256}
        for item in parsed.files
    ]
    try:
        return recorded == _snapshot_manifest(parsed.snapshot)
    except (OSError, ValueError):
        return False


class ModelReceiptVerifier:
    """Bounded, single-flight cache of valid content-integrity checks."""

    def __init__(self, *, max_entries: int = 64, cache_path: Path | None = None) -> None:
        self._max_entries = max(1, min(int(max_entries), 256))
        self._cache_path = cache_path
        self._persisted: OrderedDict[str, str] = OrderedDict()
        if cache_path is not None:
            try:
                with cache_path.open("rb") as handle:
                    raw = handle.read(_MAX_RECEIPT_BYTES + 1)
                if len(raw) <= _MAX_RECEIPT_BYTES:
                    payload = json.loads(raw)
                    if isinstance(payload, dict) and payload.get("schema_version") == 1:
                        entries = payload.get("entries")
                        if isinstance(entries, dict):
                            self._persisted.update(
                                (key, value) for key, value in entries.items()
                                if isinstance(key, str) and _SHA256_RE.fullmatch(key)
                                and isinstance(value, str) and _SHA256_RE.fullmatch(value)
                            )
            except (OSError, ValueError):
                pass
        while len(self._persisted) > self._max_entries:
            self._persisted.popitem(last=False)
        self._condition = threading.Condition()
        self._entries: OrderedDict[tuple[str, str, bytes], tuple[Any, ...]] = OrderedDict()
        self._inflight: set[tuple[str, str, bytes]] = set()

    @staticmethod
    def _key(parsed: _ParsedReceipt) -> tuple[str, str, bytes]:
        snapshot_token = hashlib.sha256(os.fsencode(str(parsed.snapshot))).digest()
        return parsed.model_id, parsed.revision, snapshot_token

    @staticmethod
    def _fingerprint_token(identity: tuple[Any, ...]) -> str:
        encoded = json.dumps(identity, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(b"orpheus-receipt-fingerprint-v1\0" + encoded).hexdigest()

    def _save_persisted(self) -> None:
        if self._cache_path is None:
            return
        try:
            atomic_write_json(
                self._cache_path,
                {"schema_version": 1, "entries": self._persisted},
                permissions=0o600, sync_parent=sync_directory_if_openable,
                cleanup="before_replace",
            )
        except OSError:
            # An unwritable performance cache must never change readiness.
            pass

    def verify(self, snapshot_path: Path, *, model_id: str, revision: str) -> bool:
        while True:
            parsed = _parse_download_receipt(
                snapshot_path,
                model_id=model_id,
                revision=revision,
            )
            if parsed is None:
                return False
            fingerprint = _snapshot_metadata_fingerprint(parsed)
            if fingerprint is None:
                return False
            key = self._key(parsed)
            persisted_key = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
            token = self._fingerprint_token(fingerprint.identity)
            with self._condition:
                cached = self._entries.get(key)
                if fingerprint.cacheable and (
                    cached == fingerprint.identity or self._persisted.get(persisted_key) == token
                ):
                    self._entries[key] = fingerprint.identity
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max_entries:
                        self._entries.popitem(last=False)
                    return True
                if key not in self._inflight:
                    self._inflight.add(key)
                    break
                self._condition.wait()

        try:
            valid = _validate_parsed_receipt(parsed)
            after = (
                _parse_download_receipt(
                    snapshot_path,
                    model_id=model_id,
                    revision=revision,
                )
                if valid
                else None
            )
            after_fingerprint = (
                _snapshot_metadata_fingerprint(after) if after is not None else None
            )
            with self._condition:
                if valid and after_fingerprint is not None and after_fingerprint.identity == fingerprint.identity:
                    if fingerprint.cacheable and after_fingerprint.cacheable:
                        self._entries[key] = fingerprint.identity
                        self._entries.move_to_end(key)
                        while len(self._entries) > self._max_entries:
                            self._entries.popitem(last=False)
                        self._persisted[persisted_key] = token
                        self._persisted.move_to_end(persisted_key)
                        while len(self._persisted) > self._max_entries:
                            self._persisted.popitem(last=False)
                        self._save_persisted()
                    else:
                        self._entries.pop(key, None)
                else:
                    self._entries.pop(key, None)
                    if self._persisted.pop(persisted_key, None) is not None:
                        self._save_persisted()
                    valid = False
            return valid
        finally:
            # Interrupted hashing must release the single-flight reservation so
            # concurrent readiness requests and later retries can proceed.
            with self._condition:
                self._inflight.discard(key)
                self._condition.notify_all()

    def invalidate(self) -> None:
        with self._condition:
            self._entries.clear()
            self._persisted.clear()
            self._save_persisted()
            self._condition.notify_all()


def validate_download_receipt(
    snapshot_path: Path,
    *,
    model_id: str,
    revision: str,
) -> bool:
    """Validate a receipt and its exact snapshot without exposing local details."""
    parsed = _parse_download_receipt(
        snapshot_path,
        model_id=model_id,
        revision=revision,
    )
    return parsed is not None and _validate_parsed_receipt(parsed)
