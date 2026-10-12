"""Download model snapshots and record their verified contents."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Iterable

from modules.hf_cache import configure_hf_cache
from modules.runtime_setup.catalog import _normalize_model_id, _REVISION_RE, pinned_revision_for
from modules.runtime_setup.receipts import (
    DOWNLOAD_RECEIPT_FILENAME,
    _file_size_and_sha256,
    _parse_download_receipt,
    _remove_snapshot_entry,
    _remove_unexpected_snapshot_entries,
    _resolved_allowed_file_target,
    _validate_parsed_receipt,
    write_download_receipt,
)

configure_hf_cache()


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
    if receipt_path.exists():
        trusted = (
            _parse_download_receipt(snapshot_path, model_id=model_id, revision=receipt_revision)
            if receipt_revision is not None else None
        )
        if trusted is None:
            # An unreadable receipt cannot certify any of the cached bytes.
            if local_dir is None:
                _remove_unexpected_snapshot_entries(snapshot_path)
            snapshot_path = Path(snapshot_download(**kwargs, force_download=True))
        elif not _validate_parsed_receipt(trusted):
            from huggingface_hub import hf_hub_download

            _remove_unexpected_snapshot_entries(trusted.snapshot, (entry.path for entry in trusted.files))
            for entry in trusted.files:
                target = _resolved_allowed_file_target(trusted.snapshot, trusted.snapshot / entry.path)
                try:
                    matches = target is not None and _file_size_and_sha256(target) == (entry.size, entry.sha256)
                except OSError:
                    matches = False
                if not matches:
                    if local_dir is None:
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
