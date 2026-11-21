"""Hugging Face cache inspection and conservative shared-blob cleanup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from huggingface_hub import constants as hf_constants

from modules.runtime_setup.catalog import MODEL_GROUPS, pinned_revision_for, resolve_model_groups
from modules.runtime_setup.receipts import ModelReceiptVerifier


def scan_cache_dir(*args: Any, **kwargs: Any) -> Any:
    """Load Hub's full cache scanner only for storage/revision discovery."""
    from huggingface_hub import scan_cache_dir as scan

    return scan(*args, **kwargs)


def _staged_model_manifests(cache_root: Path) -> list[tuple[str, Path, Path, dict[str, Any]]]:
    # Resolve journal ownership lazily to avoid a cache/service import cycle.
    from modules.app.services.model_storage_service import _staged_model_manifests as read_manifests

    return read_manifests(cache_root)


def _manifest_shared_files(cache_root: Path, data: dict[str, Any]) -> set[Path]:
    from modules.app.services.model_storage_service import _manifest_shared_files as shared_files

    return shared_files(cache_root, data)


def _integrity_cache_path() -> Path | None:
    """The desktop app's shared receipt-check cache, as model snapshots use it.

    Without ``AUDIOBOOK_DATA_DIR`` (set by the desktop shell) checks stay in
    memory rather than writing beside a guessed default.
    """
    if not os.environ.get("AUDIOBOOK_DATA_DIR", "").strip():
        return None
    from modules.runtime_setup.state import default_state_path

    return default_state_path().with_name("model-integrity-cache.json")


def _default_receipt_verifier() -> ModelReceiptVerifier:
    # Resolved per call so it follows the current data directory; earlier
    # content checks come from the shared file instead of re-hashing snapshots.
    try:
        cache_path = _integrity_cache_path()
    except Exception:
        cache_path = None
    return ModelReceiptVerifier(cache_path=cache_path)


def _repo_has_complete_revision(
    repo: Any,
    *,
    model_id: str,
    receipt_verifier: ModelReceiptVerifier,
) -> bool:
    pinned_revision = pinned_revision_for(model_id)
    for revision in getattr(repo, "revisions", ()):
        snapshot_path = getattr(revision, "snapshot_path", None)
        commit_hash = getattr(revision, "commit_hash", None)
        if (
            isinstance(snapshot_path, Path)
            and isinstance(commit_hash, str)
            and (pinned_revision is None or commit_hash == pinned_revision)
            and receipt_verifier.verify(
                snapshot_path,
                model_id=model_id,
                revision=commit_hash,
            )
        ):
            return True
    return False


def _check_model_group_assets(
    group_ids: tuple[str, ...] | list[str],
    *,
    cache_scanner: Callable[[], Any] | None = None,
    receipt_verifier: ModelReceiptVerifier | None = None,
) -> dict[str, dict[str, Any]]:
    """Inspect the local HF cache without network access or path disclosure."""
    if not group_ids:
        return {}
    verifier = receipt_verifier or _default_receipt_verifier()
    if cache_scanner is None:
        # The curated catalog pins every model to a known snapshot. Its receipt
        # validates the exact files and their content, so reading unrelated HF
        # repositories adds no readiness evidence (and scales with all models
        # installed by other applications). Keep scans for explicit adapters and
        # future unpinned catalogs that need revision discovery.
        pinned = _check_pinned_model_group_assets(group_ids, verifier)
        if pinned is not None:
            return pinned
        cache_scanner = scan_cache_dir
    from huggingface_hub.errors import CacheNotFound

    try:
        cache = cache_scanner()
    except CacheNotFound:
        return {
            group_id: {"ready": False, "reason": "missing"}
            for group_id in group_ids
        }
    except Exception:
        return {
            group_id: {"ready": False, "reason": "unavailable"}
            for group_id in group_ids
        }
    repos = {
        str(repo.repo_id): repo
        for repo in getattr(cache, "repos", ())
        if getattr(repo, "repo_type", "model") == "model"
    }
    result: dict[str, dict[str, Any]] = {}
    for group_id in group_ids:
        model_ids = resolve_model_groups((group_id,))
        missing = any(
            model_id not in repos
            or (
                (pinned_revision := pinned_revision_for(model_id)) is not None
                and not any(
                    getattr(revision, "commit_hash", None) == pinned_revision
                    for revision in getattr(repos[model_id], "revisions", ())
                )
            )
            for model_id in model_ids
        )
        incomplete = any(
            model_id in repos
            and not _repo_has_complete_revision(
                repos[model_id],
                model_id=model_id,
                receipt_verifier=verifier,
            )
            for model_id in model_ids
        )
        reason = "missing" if missing else "incomplete" if incomplete else None
        result[group_id] = {"ready": reason is None, "reason": reason}
    return result


def _check_pinned_model_group_assets(
    group_ids: tuple[str, ...] | list[str],
    verifier: ModelReceiptVerifier,
) -> dict[str, dict[str, Any]] | None:
    groups = {group: resolve_model_groups((group,)) for group in group_ids}
    revisions = {
        model: pinned_revision_for(model)
        for models in groups.values() for model in models
    }
    if any(revision is None for revision in revisions.values()):
        return None
    cache = Path(hf_constants.HF_HUB_CACHE)
    reasons: dict[str, str | None] = {}
    for model, revision in revisions.items():
        assert revision is not None
        snapshot = cache / ("models--" + model.replace("/", "--")) / "snapshots" / revision
        try:
            if not snapshot.is_dir():
                reasons[model] = "missing"
            elif verifier.verify(snapshot, model_id=model, revision=revision):
                reasons[model] = None
            else:
                reasons[model] = "incomplete"
        except OSError:
            reasons[model] = "unavailable"
    result: dict[str, dict[str, Any]] = {}
    for group, models in groups.items():
        model_reasons = {reasons[model] for model in models}
        reason = next(
            (reason for reason in ("missing", "unavailable", "incomplete") if reason in model_reasons),
            None,
        )
        result[group] = {"ready": reason is None, "reason": reason}
    return result


def _managed_model_repo_ids() -> dict[str, str]:
    """Map each managed HF repo id to the model group that owns it."""
    owners: dict[str, str] = {}
    for group_id in MODEL_GROUPS:
        for model_id in resolve_model_groups((group_id,)):
            owners.setdefault(str(model_id), group_id)
    return owners


def _managed_cache_paths(cache: Any, *, cache_root: Path | None = None) -> dict[str, Path]:
    # Only construct names from our fixed model catalog, never request paths or warnings.
    roots = {Path(repo.repo_path).parent for repo in getattr(cache, "repos", ())}
    if cache_root is not None:
        roots = {cache_root}
    elif not roots:
        roots = {Path(hf_constants.HF_HUB_CACHE)}
    return {
        model_id: root / ("models--" + model_id.replace("/", "--"))
        for root in roots for model_id in _managed_model_repo_ids()
    }


def _owned_shared_store(cache_root: Path) -> Path | None:
    try:
        from huggingface_hub.utils._shared_blobs import is_shared_blobs_dir

        store = cache_root / "blobs"
        if store.is_symlink() or store.is_junction():
            return None
        if store.resolve() != cache_root.resolve() / "blobs":
            return None
        return store.resolve() if is_shared_blobs_dir(store) else None
    except Exception:
        # Private Hub helpers may be absent or incompatible; retain the payloads.
        return None


def _shared_store_files(repo: Path) -> set[Path]:
    """Payloads in huggingface_hub's cache-wide blob store that ``repo`` links to."""
    try:
        store = _owned_shared_store(repo.parent)
        if store is None:
            return set()
        entries = list((repo / "blobs").iterdir())
    except OSError:
        return set()
    files: set[Path] = set()
    for entry in entries:
        if not entry.is_symlink():
            continue
        try:
            target = entry.resolve(strict=True)
        except OSError:
            continue
        if target.is_relative_to(store) and target.is_file():
            files.add(target)
    return files


def _release_shared_store_files(
    files: set[Path], cache_root: Path, *, failed_files: set[Path] | None = None,
) -> int:
    """Collect through Hub, reporting candidates that still need a sweep retry.

    Keep the byte-count return for callers that only report storage usage.
    Transaction owners pass ``failed_files`` before retiring their journals.
    """
    failed = failed_files if failed_files is not None else set()
    if not files:
        return 0
    store = _owned_shared_store(cache_root)
    if store is None:
        failed.update(files)
        return 0
    try:
        from huggingface_hub.utils._shared_blobs import (
            _read_valid_manifest_references,
            sweep_shared_blob,
        )
        manifests = _staged_model_manifests(cache_root)
    except (ImportError, OSError):
        failed.update(files)
        return 0
    retained: set[Path] = set()
    for _, stage, _, data in manifests:
        if stage.exists():
            retained.update(_manifest_shared_files(cache_root, data))
            retained.update(_shared_store_files(stage))
    freed = 0
    for payload in files - retained:
        try:
            if not payload.is_relative_to(store) or payload.resolve() != payload:
                failed.add(payload)
                continue
            try:
                size = payload.stat().st_size
            except FileNotFoundError:
                continue  # An earlier sweep already removed this candidate.
            removed = sweep_shared_blob(payload, cache_dir=cache_root)
            # Hub may return zero after removal if its .refs cleanup fails.
            freed += removed or (size if not payload.exists() else 0)
            if payload.exists() and not _read_valid_manifest_references(payload, cache_root):
                # Hub also returns zero for lock, metadata and unlink failures.
                # Only verified live references make retention a completed sweep.
                failed.add(payload)
        except Exception:
            failed.add(payload)
    return freed


def _cache_bytes(path: Path, *other_paths: Path) -> int:
    paths = [repo for repo in (path, *other_paths) if not repo.is_symlink() and not repo.is_junction()]
    total = sum(_repo_file_bytes(repo) for repo in paths)
    for payload in set().union(*(_shared_store_files(repo) for repo in paths)):
        try:
            total += payload.stat().st_size
        except OSError:
            pass
    return total


def _repo_file_bytes(path: Path) -> int:
    if path.is_symlink() or path.is_junction():
        return 0
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink() and not (Path(root) / name).is_junction()]
        for name in files:
            file = Path(root) / name
            if not file.is_symlink():
                try:
                    total += file.stat().st_size
                except FileNotFoundError:
                    pass
    return total
