"""Download model snapshots and record their verified contents."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
from typing import Iterable

from modules.hf_cache import configure_hf_cache, hf_hub_cache_path
from modules.runtime_setup.catalog import _normalize_model_id, _REVISION_RE, pinned_revision_for
from modules.runtime_setup.receipts import (
    DOWNLOAD_RECEIPT_FILENAME,
    _SHA256_RE,
    _file_size_and_sha256,
    _is_transient_path,
    _parse_download_receipt,
    _remove_snapshot_entry,
    _remove_unexpected_snapshot_entries,
    _resolved_allowed_file_target,
    _validate_parsed_receipt,
    write_download_receipt,
)

configure_hf_cache()

_GIT_SHA1_RE = re.compile(r"[0-9a-f]{40}")


def verified_cached_model_snapshot(
    model_id: str, *, cache_dir: Path | None = None, tqdm_class: type | None = None,
) -> Path | None:
    """Return a receipt-verified pinned snapshot without any Hub requests."""
    revision = pinned_revision_for(model_id)
    if revision is None:
        return None
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    kwargs: dict[str, object] = {"repo_id": model_id, "revision": revision, "local_files_only": True}
    if cache_dir is not None:
        kwargs["cache_dir"] = str(cache_dir)
    if tqdm_class is not None:
        kwargs["tqdm_class"] = tqdm_class
    try:
        snapshot = Path(snapshot_download(**kwargs))
    except LocalEntryNotFoundError:
        return None
    trusted = _parse_download_receipt(snapshot, model_id=model_id, revision=revision)
    return snapshot if trusted is not None and _validate_parsed_receipt(trusted) else None


def download_model_snapshot(
    model_id: str,
    *,
    cache_dir: Path | None = None,
    local_dir: Path | None = None,
    tqdm_class: type | None = None,
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
    if tqdm_class is not None:
        kwargs["tqdm_class"] = tqdm_class

    if pinned_revision is not None and local_dir is None:
        cached_snapshot = verified_cached_model_snapshot(model_id, cache_dir=cache_dir, tqdm_class=tqdm_class)
        if cached_snapshot is not None:
            return cached_snapshot

    download_root = _download_root(model_id, cache_dir=cache_dir, local_dir=local_dir)
    present_before = _files_under(download_root)
    started_ns = time.time_ns()
    snapshot_path = Path(snapshot_download(**kwargs))
    # Record the trusted pinned revision when we have one; otherwise fall back to
    # the resolved snapshot directory name (the commit sha under snapshots/).
    receipt_revision = pinned_revision or (
        snapshot_path.name.lower()
        if snapshot_path.parent.name == "snapshots"
        and _REVISION_RE.fullmatch(snapshot_path.name.lower())
        else None
    )
    receipt_path = snapshot_path / DOWNLOAD_RECEIPT_FILENAME
    # A symlinked receipt (even a dangling one) is as untrusted as a damaged one.
    if receipt_path.exists() or receipt_path.is_symlink():
        trusted = (
            _parse_download_receipt(snapshot_path, model_id=model_id, revision=receipt_revision)
            if receipt_revision is not None else None
        )
        if trusted is None:
            # An unreadable receipt cannot certify any of the cached bytes.
            if local_dir is None and _owned_snapshot(snapshot_path, download_root, local_dir=None):
                _remove_unexpected_snapshot_entries(snapshot_path)
            snapshot_path = Path(snapshot_download(**kwargs, force_download=True))
        elif not _validate_parsed_receipt(trusted):
            from huggingface_hub import hf_hub_download

            hub_owned = local_dir is None and _owned_snapshot(trusted.snapshot, download_root, local_dir=None)
            if hub_owned:
                # Only the Hub-owned cache snapshot may be pruned; a local_dir
                # can hold the user's own files.
                _remove_unexpected_snapshot_entries(trusted.snapshot, (entry.path for entry in trusted.files))
            for entry in trusted.files:
                target = _resolved_allowed_file_target(trusted.snapshot, trusted.snapshot / entry.path)
                try:
                    matches = target is not None and _file_size_and_sha256(target) == (entry.size, entry.sha256)
                except OSError:
                    matches = False
                if not matches:
                    if hub_owned:
                        # Hub force-download refreshes blobs but preserves an
                        # existing snapshot pointer. Copied Windows snapshots
                        # need the damaged entry removed before publication.
                        _remove_snapshot_entry(trusted.snapshot / entry.path)
                    hf_hub_download(
                        **{**kwargs, "revision": receipt_revision},
                        filename=entry.path, force_download=True,
                    )
            if not _validate_parsed_receipt(trusted):
                raise RuntimeError(f"Repaired cache for {model_id} still fails its download receipt.")
        if trusted is not None:
            return snapshot_path
    elif _owned_snapshot(snapshot_path, download_root, local_dir=local_dir):
        # The Hub keeps files and blobs it finds already present without
        # checking them; a receipt must only certify bytes that match their
        # published hashes. Files this call downloaded are not hashed twice.
        blob_dir = _entry_key(download_root / "blobs" / "_").parent
        reused = _reused_snapshot_files(
            snapshot_path,
            present_before,
            started_ns,
            # Without symlinks the Hub copies an existing blob into the
            # snapshot, which then looks like a fresh file.
            copies_reused=local_dir is None and any(key.parent == blob_dir for key in present_before),
        )
        if reused:
            _refresh_unverified_files(
                snapshot_path,
                reused,
                download_kwargs={**kwargs, "revision": receipt_revision},
                local_dir=local_dir,
            )
    write_download_receipt(
        snapshot_path,
        model_id=model_id,
        revision=receipt_revision,
    )
    return snapshot_path


def _download_root(
    model_id: str,
    *,
    cache_dir: Path | None,
    local_dir: Path | None,
) -> Path:
    """The directory the Hub writes this model into: its cache repo or ``local_dir``."""
    if local_dir is not None:
        return Path(local_dir)
    if cache_dir is not None:
        cache_root = Path(cache_dir)
    else:
        hf_constants = sys.modules.get("huggingface_hub.constants")
        cache_root = Path(hf_constants.HF_HUB_CACHE) if hf_constants is not None else hf_hub_cache_path()
    # The Hub's repo folder naming: "models--<org>--<name>".
    return cache_root / "--".join(("models", *model_id.split("/")))


def _files_under(root: Path) -> set[Path]:
    """Absolute paths of every entry (snapshot files and blobs) below ``root``."""
    try:
        if not root.is_dir():
            return set()
        return {
            _entry_key(path) for path in root.rglob("*") if path.is_symlink() or not path.is_dir()
        }
    except OSError:
        return set()


def _entry_key(path: Path) -> Path:
    """Compare entries by location without following the entry itself."""
    return Path(os.path.realpath(path.parent)) / path.name


def _owned_snapshot(snapshot: Path, download_root: Path, *, local_dir: Path | None) -> bool:
    """Whether ``snapshot`` is exactly where the Hub writes this model.

    Cache mode needs ``<repo>/snapshots/<revision>`` of this repo; local_dir
    mode needs the folder itself. Anything else is left untouched.
    """
    try:
        if local_dir is not None:
            return os.path.realpath(snapshot) == os.path.realpath(download_root)
        return (
            snapshot.parent.name == "snapshots"
            and os.path.realpath(snapshot.parent.parent) == os.path.realpath(download_root)
            and snapshot.is_dir()
        )
    except OSError:
        return False


def _entry_inside(snapshot: Path, entry: Path) -> bool:
    """Whether ``entry`` itself (not what it links to) lies below ``snapshot``."""
    root = os.path.realpath(snapshot)
    return os.path.commonpath((root, str(_entry_key(entry)))) == root and _entry_key(entry) != Path(root)


def _reused_snapshot_files(
    snapshot: Path,
    present_before: set[Path],
    started_ns: int,
    *,
    copies_reused: bool = False,
) -> set[str]:
    """Snapshot files whose bytes predate this download, so nothing checked them.

    That covers entries already in the snapshot, entries linked to a blob that
    was already in the repository (another revision's), and links into a
    store whose blob is older than this call.
    """
    reused = set()
    try:
        entries = [path for path in snapshot.rglob("*") if path.is_symlink() or not path.is_dir()]
    except OSError:
        return reused
    for entry in entries:
        relative = PurePosixPath(entry.relative_to(snapshot).as_posix())
        if _is_transient_path(relative):
            continue
        if _entry_key(entry) in present_before or (copies_reused and not entry.is_symlink()):
            reused.add(relative.as_posix())
            continue
        try:
            if entry.is_symlink():
                first_hop = Path(os.path.normpath(entry.parent / os.readlink(entry)))
                if _entry_key(first_hop) in present_before:
                    reused.add(relative.as_posix())
                    continue
            if entry.stat().st_mtime_ns < started_ns:
                reused.add(relative.as_posix())
        except OSError:
            reused.add(relative.as_posix())
    return reused


def _published_etag(snapshot: Path, relative: str, *, local_dir: Path | None) -> str | None:
    """The content hash the Hub recorded locally for a file in the snapshot."""
    if local_dir is None:
        entry = snapshot / relative
        try:
            # Cache snapshots link to blobs named by the file's ETag.
            return PurePosixPath(os.readlink(entry).replace("\\", "/")).name if entry.is_symlink() else None
        except OSError:
            return None
    try:
        from huggingface_hub._local_folder import read_download_metadata
    except ImportError:
        return None
    try:
        metadata = read_download_metadata(Path(local_dir), relative)
    except OSError:
        return None
    return metadata.etag if metadata is not None else None


def _matches_etag(file_path: Path, etag: str) -> bool:
    """Whether ``file_path`` holds the bytes an LFS or git ETag names."""
    etag = etag.strip().strip('"').lower()
    if _SHA256_RE.fullmatch(etag):
        return _file_size_and_sha256(file_path)[1] == etag
    if not _GIT_SHA1_RE.fullmatch(etag):
        return False
    digest = hashlib.sha1(usedforsecurity=False)
    with file_path.open("rb") as handle:
        digest.update(b"blob %d\0" % os.fstat(handle.fileno()).st_size)
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == etag


def _refresh_unverified_files(
    snapshot: Path,
    relative_paths: Iterable[str],
    *,
    download_kwargs: dict[str, object],
    local_dir: Path | None,
) -> None:
    """Re-download each reused file that does not match its published hash.

    In a cache snapshot every entry belongs to the Hub. A local_dir may also
    hold unrelated files: only paths the revision lists (or that carry the
    Hub's download metadata) are checked or rewritten, and nothing there is
    ever deleted.
    """
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError

    relative_paths = sorted(relative_paths)
    etags = {relative: _published_etag(snapshot, relative, local_dir=local_dir) for relative in relative_paths}
    if local_dir is not None:
        remote = _remote_file_hashes(download_kwargs, relative_paths)
        if remote is None:
            unknown = [relative for relative, etag in etags.items() if etag is None]
            if unknown:
                raise RuntimeError(
                    f"Could not check {len(unknown)} existing file(s) in the download folder "
                    "against the model's published hashes; try again when online."
                )
        else:
            # Files the revision does not list are the user's: leave them be.
            etags = {
                relative: remote.get(relative, etag)
                for relative, etag in etags.items()
                if relative in remote or etag is not None
            }
    else:
        # Copied entries (no symlinks, e.g. on Windows) carry no local hash:
        # ask the Hub instead of downloading the whole file again.
        missing = [relative for relative, etag in etags.items() if etag is None]
        if missing:
            etags.update(_remote_file_hashes(download_kwargs, missing) or {})

    for relative in relative_paths:
        if relative not in etags:
            continue
        entry = snapshot / relative
        if not _entry_inside(snapshot, entry):
            continue
        target = _resolved_allowed_file_target(snapshot, entry)
        etag = etags[relative]
        try:
            verified = target is not None and etag is not None and _matches_etag(target, etag)
        except OSError:
            verified = False
        if verified:
            continue
        if local_dir is None:
            # Force-download refreshes the blob but keeps an existing pointer.
            _remove_snapshot_entry(entry)
        try:
            hf_hub_download(**download_kwargs, filename=relative, force_download=True)
        except EntryNotFoundError:
            # Not part of this revision: leave it out of the Hub's snapshot.
            if local_dir is None and (entry.exists() or entry.is_symlink()):
                _remove_snapshot_entry(entry)


def _remote_file_hashes(download_kwargs: dict[str, object], paths: list[str]) -> dict[str, str] | None:
    """Published hashes (LFS sha256 or git blob id) of the ``paths`` in the revision.

    Paths the revision does not contain are absent. ``None`` means the Hub
    could not be asked; callers then refuse to trust the files.
    """
    try:
        from huggingface_hub import HfApi

        infos = HfApi().get_paths_info(
            str(download_kwargs["repo_id"]),
            paths,
            revision=download_kwargs.get("revision"),
            repo_type="model",
        )
    except Exception:
        return None
    hashes = {}
    for info in infos:
        lfs = getattr(info, "lfs", None)
        published = getattr(lfs, "sha256", None) if lfs is not None else getattr(info, "blob_id", None)
        path = getattr(info, "path", None)
        if isinstance(path, str) and isinstance(published, str):
            hashes[path] = published
    return hashes


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
