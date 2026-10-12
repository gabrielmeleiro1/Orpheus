"""Managed model storage transactions and recovery."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
import threading
from typing import Any, Callable
from uuid import uuid4
from weakref import WeakSet

from modules.contracts.durable_files import atomic_write_json, sync_directory
from modules.runtime_setup import hub_cache
from modules.runtime_setup import state as setup_runtime
from modules.runtime_setup.catalog import MODEL_GROUPS, resolve_model_groups
from modules.runtime_setup.hub_cache import (
    hf_constants,
    _managed_cache_paths,
    _managed_model_repo_ids,
    _release_shared_store_files,
    _repo_file_bytes,
)


class PreviewInProgress(setup_runtime.SetupExecutionError):
    """Model files are owned by a preview operation."""


class ModelDeletionInProgress(RuntimeError):
    """Preview operations cannot use model files during deletion."""


_model_ownership_lock = threading.Lock()
_preview_users = 0
_deletion_owner: int | None = None
_preview_services: WeakSet[Any] = WeakSet()


@contextmanager
def preview_model_use(service: Any):
    global _preview_users
    with _model_ownership_lock:
        if _deletion_owner is not None:
            raise ModelDeletionInProgress("Model deletion is in progress. Wait for it to finish before previewing a voice.")
        _preview_users += 1
        _preview_services.add(service)
    try:
        yield
    finally:
        with _model_ownership_lock:
            _preview_users -= 1


@contextmanager
def _model_deletion_reservation():
    global _deletion_owner
    # Order: conversion reservation (job lock released), SetupLock, ownership,
    # then voice runtime lock. The bookkeeping lock never spans any work/wait;
    # previews reserve ownership before taking their runtime lock.
    owner = threading.get_ident()
    with _model_ownership_lock:
        nested = _deletion_owner == owner
        if not nested:
            if _preview_users:
                raise PreviewInProgress("Wait for the current voice preview to finish before deleting a model.")
            if _deletion_owner is not None:
                raise setup_runtime.SetupExecutionError("Model deletion is already in progress.")
            _deletion_owner = owner
    try:
        yield
    finally:
        if not nested:
            with _model_ownership_lock:
                _deletion_owner = None


def _discard_preview_models(model_groups: tuple[str, ...] | None) -> None:
    if model_groups is None or any(group.startswith("cleaner-") for group in model_groups):
        from modules.tts.cleaner_cuda import clear_cleaner_cache as clear_cuda_cleaner_cache
        from modules.tts.cleaner_mlx import clear_cleaner_cache as clear_mlx_cleaner_cache

        # Cleaner instances can unload while retaining their process cache.
        clear_cuda_cleaner_cache()
        clear_mlx_cleaner_cache()
    if model_groups is not None and not any(group.startswith("tts-") for group in model_groups):
        return
    from modules.tts.model_cache import clear_model_cache

    # Runtime variants can share snapshots. Retire all preview variants before
    # removing TTS files, including cached models with no live preview owner.
    for service in list(_preview_services):
        service.discard_preview_models()
    clear_model_cache()


def scan_cache_dir() -> Any:
    return hub_cache.scan_cache_dir()


def _shared_store_files(repo: Path) -> set[Path]:
    return hub_cache._shared_store_files(repo)


def _staged_model_manifests(cache_root: Path) -> list[tuple[str, Path, Path, dict[str, Any]]]:
    repo_ids = {"models--" + model_id.replace("/", "--"): model_id for model_id in _managed_model_repo_ids()}
    manifests = []
    for manifest_path in cache_root.glob(".orpheus-delete-*.json"):
        try:
            if manifest_path.is_symlink():
                continue
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("original") not in repo_ids:
                continue
            name = data.get("staged")
            if not isinstance(name, str) or not re.fullmatch(r"\.orpheus-delete-[0-9a-f]{32}", name):
                continue
            if manifest_path.name != name + ".json" or not isinstance(data.get("settings_committed"), bool):
                continue
            stage = cache_root / name
            if stage.is_symlink() or stage.is_junction():
                continue
            manifests.append((repo_ids[data["original"]], stage, manifest_path, data))
        except (FileNotFoundError, ValueError, TypeError):
            continue
    return manifests


def _staged_settings_committed(data: dict[str, Any], *, cache_root: Path | None = None) -> bool | None:
    if data["settings_committed"]:
        return True
    # The settings replacement may have completed just before the flag write.
    state, error = setup_runtime._read_setup_state(setup_runtime.default_state_path())
    if error:
        return None
    current_id = state["installation_id"] if state else None
    if data.get("uninstall_runtime") and state is None:
        if data.get("previous_installation_id") is not None:
            return True
        # An unconfigured runtime had no state to remove. Its first durable
        # committed flag commits the whole transaction, including a kill before
        # the remaining repositories receive their flags. Before that point,
        # state absence means rollback, just as it did before staging began.
        root = cache_root or Path(hf_constants.HF_HUB_CACHE)
        return any(
            item.get("uninstall_runtime") and item["settings_committed"]
            and item.get("installation_id") == data.get("installation_id")
            for _, _, _, item in _staged_model_manifests(root)
        )
    if current_id is not None and current_id == data.get("installation_id"):
        return True
    if "previous_installation_id" in data and current_id == data["previous_installation_id"]:
        return False
    # Older journals cannot distinguish rollback from overwritten commit evidence.
    return None


def _manifest_shared_files(cache_root: Path, data: dict[str, Any]) -> set[Path]:
    names = data.get("shared_files", [])
    if not isinstance(names, list):
        return set()
    names = [name.replace("\\", "/") for name in names if isinstance(name, str)]
    return {
        cache_root.resolve() / name for name in names
        if re.fullmatch(r"blobs/[0-9a-f]{2}/[0-9a-f]{64}", name)
    }


def _commit_staged_models(staged: dict[str, Path]) -> None:
    for path in staged.values():
        manifest_path = path.with_name(path.name + ".json")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["settings_committed"] = True
        setup_runtime._atomic_write_json(manifest_path, data)


def _setup_reset_path() -> Path:
    return setup_runtime.default_state_path().with_name("setup-reset.json")


def read_setup_reset_id() -> str | None:
    """Read the last completed uninstall generation independently of setup state."""
    try:
        data = json.loads(_setup_reset_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    reset_id = data.get("setup_reset_id") if isinstance(data, dict) else None
    return reset_id if isinstance(reset_id, str) and reset_id else None


def reset_setup_preferences(config: Any, app_state: Any, reset_id: str | None = None) -> None:
    """Restore conversion defaults and publish a client preference reset."""
    reset_id = reset_id or uuid4().hex
    completed = getattr(app_state, "completed_setup_resets", frozenset())
    if reset_id in completed or getattr(app_state, "setup_reset_id", None) == reset_id:
        return
    # Persist delivery evidence before the uninstall journals can be retired.
    reset_path = _setup_reset_path()
    reset_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(reset_path, {"setup_reset_id": reset_id}, permissions=0o600)
    reset_preferences = getattr(config, "reset_setup_preferences", None)
    if callable(reset_preferences):
        reset_preferences()
    app_state.setup_reset_id = reset_id
    app_state.completed_setup_resets = completed | {reset_id}


def _recover_staged_models(
    *, cache_root: Path | None = None, model_groups: tuple[str, ...] | None = None,
    on_uninstall_complete: Callable[[str], None] | None = None,
) -> tuple[list[str], int]:
    """Recover cache transactions while the caller holds the setup lock."""
    root = cache_root or Path(hf_constants.HF_HUB_CACHE)
    owners = _managed_model_repo_ids()
    # Adoption also calls recovery when storage needs no changes.
    if not any(
        model_groups is None or owners[model_id] in model_groups
        for model_id, *_ in _staged_model_manifests(root)
    ):
        return [], 0
    with _model_deletion_reservation():
        # Re-read journals under ownership; the initial scan is only a hint.
        completed: set[Path] = set()
        failed: set[Path] = set()
        recovered_groups: set[str] = set()
        freed = 0
        # Keep separate entries for repeated transactions for the same model.
        manifests = [entry for entry in _staged_model_manifests(root)
                     if not entry[3].get("uninstall_runtime") or on_uninstall_complete is not None]
        uninstall_ids = {
            data.get("installation_id") for model_id, _, _, data in manifests
            if data.get("uninstall_runtime")
            and (model_groups is None or owners[model_id] in model_groups)
        }
        committed_entries = []
        for model_id, stage, manifest_path, data in manifests:
            group = owners[model_id]
            if (model_groups is not None and group not in model_groups
                    and data.get("installation_id") not in uninstall_ids):
                continue
            original = root / data["original"]
            try:
                committed = _staged_settings_committed(data, cache_root=root)
                if committed is None:
                    continue
                if committed:
                    if not stage.exists() and original.exists():
                        continue
                    if not data["settings_committed"]:
                        data["settings_committed"] = True
                        setup_runtime._atomic_write_json(manifest_path, data)
                    committed_entries.append((model_id, stage, original))
                else:
                    if stage.exists():
                        if original.exists() or original.is_symlink():
                            continue
                        stage.rename(original)
                    manifest_path.unlink(missing_ok=True)
            except (OSError, setup_runtime.SetupExecutionError):
                continue
        for model_id, stage, original in committed_entries:
            # Delete each entry even if another transaction names the same repo.
            try:
                _, removed = _delete_runtime_models(model_paths={model_id: stage}, failed_files=failed)
                freed += removed
                if not stage.exists() and not original.exists():
                    recovered_groups.add(owners[model_id])
                completed.add(stage)
            except (OSError, setup_runtime.SetupExecutionError):
                continue
        # Earlier uninstall journals may have waited for the last stage to go.
        _retire_staged_manifests(root, completed, failed, on_uninstall_complete=on_uninstall_complete)
        return [group for group in MODEL_GROUPS if group in recovered_groups], freed


@contextmanager
def _stage_runtime_models(
    group_id: str | None, *, installation_id: str, uninstall_runtime: bool = False,
):
    from huggingface_hub.errors import CacheNotFound

    with _model_deletion_reservation():
        _discard_preview_models((group_id,) if group_id is not None else None)
        state, error = setup_runtime._read_setup_state(setup_runtime.default_state_path())
        if error:
            raise setup_runtime.SetupExecutionError(error)
        previous_installation_id = state["installation_id"] if state else None
        try:
            cache = scan_cache_dir()
        except CacheNotFound:
            cache = None
        staged: dict[str, Path] = {}
        originals: dict[str, Path] = {}
        try:
            for model_id, path in _managed_cache_paths(cache).items():
                if group_id is not None and model_id not in resolve_model_groups((group_id,)):
                    continue
                if not path.exists() or path.is_symlink() or path.is_junction():
                    continue
                destination = path.with_name(f".orpheus-delete-{uuid4().hex}")
                originals[model_id] = path
                staged[model_id] = destination
                setup_runtime._atomic_write_json(destination.with_name(destination.name + ".json"), {
                    "original": path.name,
                    "staged": destination.name,
                    "settings_committed": False,
                    "installation_id": installation_id,
                    "previous_installation_id": previous_installation_id,
                    **({"uninstall_runtime": True} if uninstall_runtime else {}),
                    "shared_files": sorted(payload.relative_to(path.parent.resolve()).as_posix() for payload in _shared_store_files(path)),
                })
                path.rename(destination)
                sync_directory(path.parent)
            yield staged
        finally:
            # Keep committed residuals recoverable; roll back uncommitted renames.
            for model_id, path in staged.items():
                manifest_path = path.with_name(path.name + ".json")
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if _staged_settings_committed(data, cache_root=path.parent) is not False:
                        continue
                    if path.exists():
                        if originals[model_id].exists():
                            continue
                        path.rename(originals[model_id])
                    manifest_path.unlink(missing_ok=True)
                except (OSError, setup_runtime.SetupExecutionError):
                    pass


def _retire_staged_manifests(
    root: Path, completed: set[Path], failed: set[Path], *,
    on_uninstall_complete: Callable[[str], None] | None = None,
) -> None:
    manifests = _staged_model_manifests(root)
    for _, stage, manifest, data in manifests:
        try:
            if stage not in completed or stage.exists() or not data["settings_committed"]:
                continue
            if (root / data["original"]).exists():
                continue
            if _manifest_shared_files(root, data) & failed:
                continue
            if data.get("uninstall_runtime"):
                if on_uninstall_complete is None:
                    continue
                # Keep the transaction's witnesses until every repository is gone.
                transaction = [entry for entry in manifests
                               if entry[3].get("installation_id") == data["installation_id"]]
                # A missing stage with a surviving canonical repo may mean the
                # rename was lost. Keep every witness until recovery is certain.
                if any(path.exists() or not item["settings_committed"]
                       or (root / item["original"]).exists()
                       or _manifest_shared_files(root, item) & failed
                       for _, path, _, item in transaction):
                    continue
                state_path = setup_runtime.default_state_path()
                state, error = setup_runtime._read_setup_state(state_path)
                if error or (state and state["installation_id"] != data["previous_installation_id"]):
                    continue
                if state is not None:
                    state_path.unlink(missing_ok=True)
                    sync_directory(state_path.parent)
                # The durable transaction ID makes replay idempotent, even if
                # journal garbage collection fails after publishing completion.
                on_uninstall_complete(data["installation_id"])
            manifest.unlink(missing_ok=True)
        except (OSError, setup_runtime.SetupExecutionError):
            continue


@_model_deletion_reservation()
def _delete_runtime_models(
    *,
    cache_scanner: Callable[[], Any] | None = None,
    model_groups: tuple[str, ...] | None = None,
    cache_root: Path | None = None,
    model_paths: dict[str, Path] | None = None,
    failed_files: set[Path] | None = None,
    on_uninstall_complete: Callable[[str], None] | None = None,
) -> tuple[list[str], int]:
    """Delete only HF cache repos owned by our model groups; leave others intact."""
    from huggingface_hub.errors import CacheNotFound

    _discard_preview_models(model_groups)
    if model_paths is None:
        try:
            cache = (cache_scanner or scan_cache_dir)()
        except CacheNotFound:
            return [], 0
        if cache_scanner is not None and not cache.repos and cache_root is None:
            return [], 0
        model_paths = _managed_cache_paths(cache, cache_root=cache_root)
    owners = _managed_model_repo_ids()
    deleted_groups: set[str] = set()
    shared_files: dict[Path, set[Path]] = {}
    freed = 0
    completed: dict[Path, set[Path]] = {}
    for model_id, path in model_paths.items():
        group_id = owners[model_id]
        if model_groups is not None and group_id not in model_groups:
            continue
        manifest = path.with_name(path.name + ".json")
        if path.name.startswith(".orpheus-delete-") and manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            shared_files.setdefault(path.parent, set()).update(_manifest_shared_files(path.parent, data))
            completed.setdefault(path.parent, set()).add(path)
        if not path.exists() or path.is_symlink() or path.is_junction():
            continue
        before = _repo_file_bytes(path)
        shared_files.setdefault(path.parent, set()).update(_shared_store_files(path))
        try:
            shutil.rmtree(path)
        except OSError:
            pass  # The rescan below reports partial deletion, including locked files.
        freed += max(0, before - _repo_file_bytes(path))
        if not path.exists():
            deleted_groups.add(group_id)
    for root, files in shared_files.items():
        # Staged residuals have noncanonical names absent from Hub's manifests.
        retained = set().union(*(_shared_store_files(path) for path in model_paths.values() if path.parent == root))
        failed: set[Path] = set()
        freed += _release_shared_store_files(files - retained, root, failed_files=failed)
        if failed_files is not None:
            failed_files.update(failed)
        _retire_staged_manifests(root, completed.get(root, set()), failed,
                                on_uninstall_complete=on_uninstall_complete)
    ordered = [group_id for group_id in MODEL_GROUPS if group_id in deleted_groups]
    return ordered, freed


def _residual_notice(groups: list[str]) -> dict[str, Any]:
    if not groups:
        return {}
    return {"residual_model_groups": groups, "message": "Couldn't delete some files — they may be in use. Close other apps and retry."}
