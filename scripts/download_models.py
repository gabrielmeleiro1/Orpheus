"""Download model snapshots required by Orpheus runtimes."""

from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat as stat_module
import sys
import tempfile
import threading
from typing import Any, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.hf_cache import configure_hf_cache  # noqa: E402

configure_hf_cache()

from modules.tts.runtime_spec import (  # noqa: E402
    CLEANER_CUDA_DEFAULT_MODEL,
    CLEANER_MLX_DEFAULT_MODEL,
    KOKORO_MLX_DEFAULT_MODEL_REPO,
    KOKORO_PACKAGE_DEFAULT_MODEL,
)


DEFAULT_MODEL_IDS = (KOKORO_PACKAGE_DEFAULT_MODEL,)
DOWNLOAD_RECEIPT_FILENAME = ".orpheus-download-complete.json"
DOWNLOAD_RECEIPT_SCHEMA_VERSION = 2
_REVISION_RE = re.compile(r"[0-9a-f]{40,64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_RECEIPT_BYTES = 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
MODEL_GROUPS: Mapping[str, tuple[str, ...]] = {
    "tts-mlx": (KOKORO_MLX_DEFAULT_MODEL_REPO,),
    "tts-cpu-cuda": (KOKORO_PACKAGE_DEFAULT_MODEL,),
    "cleaner-mlx": (CLEANER_MLX_DEFAULT_MODEL,),
    "cleaner-cuda": (CLEANER_CUDA_DEFAULT_MODEL,),
    "cleaner-cuda-large": ("Qwen/Qwen2.5-3B-Instruct",),
    "cleaner-mlx-small": ("mlx-community/Llama-3.2-1B-Instruct-4bit",),
}

# Pinned Hugging Face commit revisions for every curated model. Each download is
# fetched at exactly this commit, so a fresh install is reproducible and an
# upstream repo update (or compromise) cannot silently change what ships. Bump a
# SHA here deliberately when adopting a new model revision.
MODEL_REVISIONS: Mapping[str, str] = {
    KOKORO_MLX_DEFAULT_MODEL_REPO: "a71e4d38b236d968966a2002c4c895dbd12b1c3c",
    KOKORO_PACKAGE_DEFAULT_MODEL: "f3ff3571791e39611d31c381e3a41a3af07b4987",
    CLEANER_MLX_DEFAULT_MODEL: "7f0dc925e0d0afb0322d96f9255cfddf2ba5636e",
    CLEANER_CUDA_DEFAULT_MODEL: "989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
    "Qwen/Qwen2.5-3B-Instruct": "aa8e72537993ba99e69dfaafa59ed015b17504d1",
    "mlx-community/Llama-3.2-1B-Instruct-4bit": "08231374eeacb049a0eade7922910865b8fce912",
}


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


def _normalize_model_id(model_id: str) -> str:
    normalized_model_id = model_id.strip()
    if "://" in normalized_model_id:
        raise ValueError(
            f"Model must be a Hugging Face repository id, not a URL: {model_id!r}."
        )
    return normalized_model_id


def pinned_revision_for(model_id: str) -> str | None:
    """Return the pinned commit revision for a curated model, if one exists."""
    return MODEL_REVISIONS.get(_normalize_model_id(model_id))


def _deduplicate_model_ids(model_ids: Iterable[str]) -> tuple[str, ...]:
    """Normalize model ids and preserve their first-seen order."""
    normalized_ids = (_normalize_model_id(model_id) for model_id in model_ids)
    return tuple(dict.fromkeys(model_id for model_id in normalized_ids if model_id))


def resolve_model_groups(group_ids: Iterable[str]) -> tuple[str, ...]:
    """Resolve allowlisted model groups to stable, deduplicated model ids."""
    resolved: list[str] = []
    for group_id in group_ids:
        normalized_group_id = group_id.strip().lower()
        try:
            resolved.extend(MODEL_GROUPS[normalized_group_id])
        except KeyError as exc:
            available = ", ".join(MODEL_GROUPS)
            raise ValueError(
                f"Unknown model group {group_id!r}. Available groups: {available}."
            ) from exc
    return _deduplicate_model_ids(resolved)


def _is_transient_path(relative_path: PurePosixPath) -> bool:
    parts = relative_path.parts
    return bool(parts) and (
        parts[0] in {".cache", ".locks"}
        or relative_path.name == DOWNLOAD_RECEIPT_FILENAME
        or relative_path.name.endswith((".lock", ".incomplete"))
    )


def _resolved_allowed_file_target(snapshot_path: Path, file_path: Path) -> Path | None:
    try:
        snapshot_root = snapshot_path.resolve(strict=True)
        target = file_path.resolve(strict=True)
    except OSError:
        return None
    if not target.is_file():
        return None
    if target.is_relative_to(snapshot_root):
        return target
    if snapshot_path.parent.name != "snapshots":
        return None
    blobs_root = (snapshot_path.parent.parent / "blobs").resolve()
    return target if target.is_relative_to(blobs_root) else None


def _allowed_file_target(snapshot_path: Path, file_path: Path) -> bool:
    return _resolved_allowed_file_target(snapshot_path, file_path) is not None


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
    manifest: list[dict[str, object]] = []
    for file_path in sorted(root.rglob("*")):
        relative = file_path.relative_to(root)
        relative_posix = PurePosixPath(relative.as_posix())
        if _is_transient_path(relative_posix):
            continue
        if file_path.is_symlink() and not _allowed_file_target(root, file_path):
            raise ValueError("Model snapshot contains an unsafe or missing file.")
        if file_path.is_dir():
            continue
        target = _resolved_allowed_file_target(root, file_path)
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
    # Path ordering is case-insensitive on Windows; receipt parsing sorts POSIX
    # strings. Use the same canonical order on every platform, including when
    # validating receipts written by older Windows builds.
    return sorted(manifest, key=lambda entry: str(entry["path"]))


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
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=snapshot,
            prefix=f".{DOWNLOAD_RECEIPT_FILENAME}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, receipt_path)
        temporary_path = None
        try:
            directory_fd = os.open(snapshot, os.O_RDONLY)
        except OSError:
            return receipt_path
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return receipt_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _valid_receipt_relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
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


def _read_receipt_json(receipt_path: Path) -> tuple[Any, tuple[int, ...]] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(receipt_path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat_module.S_ISREG(before.st_mode) or before.st_size > _MAX_RECEIPT_BYTES:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_RECEIPT_BYTES + 1)
        after = os.fstat(descriptor)
        if len(raw) > _MAX_RECEIPT_BYTES or _stat_identity(before) != _stat_identity(after):
            return None
    finally:
        os.close(descriptor)
    try:
        return json.loads(raw), _stat_identity(after)
    except (UnicodeDecodeError, json.JSONDecodeError):
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


def _snapshot_metadata_fingerprint(parsed: _ParsedReceipt) -> tuple[Any, ...] | None:
    try:
        root_identity = _stat_identity(parsed.snapshot.stat())
        expected_files = {item.path: item for item in parsed.files}
        expected_paths = set(expected_files)
        actual: list[tuple[Any, ...]] = []
        for file_path in sorted(parsed.snapshot.rglob("*")):
            relative = PurePosixPath(file_path.relative_to(parsed.snapshot).as_posix())
            if _is_transient_path(relative):
                continue
            if file_path.is_symlink() and not _allowed_file_target(parsed.snapshot, file_path):
                return None
            if file_path.is_dir():
                continue
            relative_text = relative.as_posix()
            if relative_text not in expected_paths:
                return None
            target = _resolved_allowed_file_target(parsed.snapshot, file_path)
            if target is None:
                return None
            lexical_stat = file_path.lstat()
            target_stat = target.stat()
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
                    _stat_identity(target_stat),
                    link_identity,
                )
            )
        if {item[0] for item in actual} != expected_paths:
            return None
        return (
            DOWNLOAD_RECEIPT_SCHEMA_VERSION,
            parsed.model_id,
            parsed.revision,
            parsed.receipt_identity,
            root_identity,
            tuple(actual),
        )
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

    def __init__(self, *, max_entries: int = 64) -> None:
        self._max_entries = max(1, min(int(max_entries), 256))
        self._condition = threading.Condition()
        self._entries: OrderedDict[tuple[str, str, bytes], tuple[Any, ...]] = OrderedDict()
        self._inflight: set[tuple[str, str, bytes]] = set()

    @staticmethod
    def _key(parsed: _ParsedReceipt) -> tuple[str, str, bytes]:
        snapshot_token = hashlib.sha256(os.fsencode(str(parsed.snapshot))).digest()
        return parsed.model_id, parsed.revision, snapshot_token

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
            with self._condition:
                cached = self._entries.get(key)
                if cached == fingerprint:
                    self._entries.move_to_end(key)
                    return True
                if key not in self._inflight:
                    self._inflight.add(key)
                    break
                self._condition.wait()

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
            if valid and after_fingerprint == fingerprint:
                self._entries[key] = fingerprint
                self._entries.move_to_end(key)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            else:
                self._entries.pop(key, None)
                valid = False
            self._inflight.discard(key)
            self._condition.notify_all()
        return valid

    def invalidate(self) -> None:
        with self._condition:
            self._entries.clear()
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download model assets required for local Orpheus runtimes.",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help=(
            "Hugging Face model id to download. May be supplied more than once. "
            "Defaults to the package Kokoro model."
        ),
    )
    parser.add_argument(
        "--group",
        action="append",
        dest="groups",
        help=(
            "Curated model group to download. May be supplied more than once. "
            "Use --list-groups to see the allowlist."
        ),
    )
    parser.add_argument(
        "--list-groups",
        action="store_true",
        help="List curated model groups and exit.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Optional Hugging Face cache directory. Defaults to the user cache.",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=None,
        help=(
            "Optional folder for downloaded snapshots. A single model uses the "
            "folder directly; multiple models use deterministic subfolders."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the model ids that would be downloaded without fetching them.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON Lines output.",
    )
    return parser


def download_model_snapshot(
    model_id: str,
    *,
    cache_dir: Path | None = None,
    local_dir: Path | None = None,
) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface-hub is required to download model assets. "
            "Install requirements.txt first."
        ) from exc

    kwargs: dict[str, object] = {"repo_id": model_id}
    pinned_revision = pinned_revision_for(model_id)
    if pinned_revision is not None:
        kwargs["revision"] = pinned_revision
    if cache_dir is not None:
        kwargs["cache_dir"] = str(cache_dir)
    if local_dir is not None:
        kwargs["local_dir"] = str(local_dir)

    snapshot_path = Path(snapshot_download(**kwargs))
    # Record the trusted pinned revision when we have one; otherwise fall back to
    # the resolved snapshot directory name (the commit sha under snapshots/).
    receipt_revision = pinned_revision or (
        snapshot_path.name.lower()
        if snapshot_path.parent.name == "snapshots"
        and _REVISION_RE.fullmatch(snapshot_path.name.lower())
        else None
    )
    write_download_receipt(
        snapshot_path,
        model_id=model_id,
        revision=receipt_revision,
    )
    return snapshot_path


def _local_dir_for_model(
    local_dir: Path | None,
    model_id: str,
    *,
    multiple_models: bool,
) -> Path | None:
    if local_dir is None or not multiple_models:
        return local_dir
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", model_id).strip("._-")
    safe_name = (safe_name or "model")[:80]
    fingerprint = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:12]
    return local_dir / f"{safe_name}-{fingerprint}"


def download_required_models(
    model_ids: Iterable[str],
    *,
    cache_dir: Path | None = None,
    local_dir: Path | None = None,
    dry_run: bool = False,
    emit_output: bool = True,
    json_output: bool = False,
) -> list[Path]:
    normalized_model_ids = tuple(
        normalized_model_id
        for model_id in model_ids
        if (normalized_model_id := _normalize_model_id(model_id))
    )
    multiple_models = len(normalized_model_ids) > 1
    snapshots: list[Path] = []
    for normalized_model_id in normalized_model_ids:
        if dry_run:
            if emit_output and not json_output:
                print(f"Would download {normalized_model_id}")
            continue
        snapshot_path = download_model_snapshot(
            normalized_model_id,
            cache_dir=cache_dir,
            local_dir=_local_dir_for_model(
                local_dir,
                normalized_model_id,
                multiple_models=multiple_models,
            ),
        )
        snapshots.append(snapshot_path)
        if emit_output:
            if json_output:
                print(
                    json.dumps(
                        {
                            "event": "downloaded",
                            "model": normalized_model_id,
                            "path": str(snapshot_path),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            else:
                print(
                    f"Downloaded {normalized_model_id} -> {snapshot_path}",
                    flush=True,
                )
    return snapshots


def _print_groups(*, json_output: bool) -> None:
    groups = [
        {"id": group_id, "models": list(model_ids)}
        for group_id, model_ids in MODEL_GROUPS.items()
    ]
    if json_output:
        print(json.dumps({"groups": groups}, sort_keys=True))
        return
    for group in groups:
        print(f"{group['id']}: {', '.join(group['models'])}")


def _print_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps({"error": message, "event": "error"}, sort_keys=True),
            file=sys.stderr,
        )
        return
    print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_groups:
        _print_groups(json_output=args.json)
        return 0

    try:
        grouped_model_ids = resolve_model_groups(args.groups or ())
        if grouped_model_ids or args.models:
            model_ids = _deduplicate_model_ids((*grouped_model_ids, *(args.models or ())))
        else:
            model_ids = DEFAULT_MODEL_IDS
    except ValueError as exc:
        _print_error(str(exc), json_output=args.json)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    "event": "plan",
                    "models": list(model_ids),
                },
                sort_keys=True,
            ),
            flush=True,
        )
    else:
        print(f"Models to download: {', '.join(model_ids)}", flush=True)

    try:
        snapshots = download_required_models(
            model_ids,
            cache_dir=args.cache_dir,
            local_dir=args.local_dir,
            dry_run=args.dry_run,
            json_output=args.json,
        )
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        _print_error(f"Model download failed: {detail}", json_output=args.json)
        return 1
    if args.dry_run:
        return 0
    if not snapshots:
        print("No model ids were provided.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
