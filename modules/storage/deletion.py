"""Durable inventories for reversible audiobook deletion."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import stat

from modules.contracts.durable_files import atomic_write_json, sync_directory, sync_file as sync_file, unlink_file

DELETE_MANIFEST = "transaction.json"
DELETE_PRESERVE_MARKER = ".preserve"
LOCAL_DELETE_STAGING = ".orpheus-delete-staging"
# Book id recorded for an output folder that no library row owned.
UNOWNED_FOLDER_BOOK_ID = 0

logger = logging.getLogger(__name__)


def _contains_reference(original: Path, reference: Path) -> bool:
    original, reference = original.resolve(), reference.resolve()
    if reference.is_relative_to(original):
        return True
    # Conservatively retain case variants even when directory case semantics differ.
    folded_root = os.path.normcase(str(original)).casefold()
    folded_path = os.path.normcase(str(reference)).casefold()
    return folded_path == folded_root or folded_path.startswith(folded_root.rstrip(os.sep) + os.sep)


def delete_staging_policy(
    entry: Path,
    artifacts: list[tuple[Path, Path]] | None,
    live_references: set[Path],
    *,
    book_exists: bool = False,
    book_has_paths: bool = False,
    database_replaced: bool = False,
    audio_suffixes: tuple[str, ...] = (".m4b", ".m4a", ".mp3"),
) -> tuple[str, list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    """Return the decision, artifacts to restore, and artifacts to keep staged."""
    marker, preserve = entry / ".committed", entry / DELETE_PRESERVE_MARKER
    if any(path.is_symlink() or path.is_junction() for path in (entry, marker, preserve)):
        raise ValueError("Invalid deletion marker path")
    committed = marker.is_file()
    if artifacts is None:
        known_names = {f"{DELETE_MANIFEST}.tmp", ".committed"}
        try:
            referenced = any(_contains_reference(entry, path) for path in live_references)
        except OSError:
            return "preserve", [], []
        if preserve.exists() or referenced:
            return "preserve", [], []
        if any(
            child.is_symlink() or child.is_junction() or not child.is_file()
            or (child.name not in known_names and not (committed and child.suffix.lower() in audio_suffixes))
            for child in entry.iterdir()
        ):
            return "preserve", [], []
        return "remove", [], []

    try:
        protected = [
            artifact for artifact in artifacts
            if any(_contains_reference(artifact[1], path) for path in live_references)
        ]
        if protected and not committed:
            return "preserve", [], []
        referenced = [
            artifact for artifact in artifacts
            if artifact not in protected
            and any(_contains_reference(artifact[0], path) for path in live_references)
        ]
    except OSError:
        return "preserve", [], []
    if book_exists and not committed and not book_has_paths:
        # Older rows can lack paths; an uncommitted delete still needs rollback.
        referenced = artifacts
    if referenced or protected:
        # Moving protected bytes would invalidate the live staged paths.
        return "restore", referenced, protected
    if not committed and (database_replaced or book_exists or preserve.exists()):
        return "preserve", [], []
    return "remove", [], []


def restore_staged_artifacts(artifacts: list[tuple[Path, Path]]) -> None:
    for original, staged in reversed(artifacts):
        if not staged.exists():
            continue
        if os.path.lexists(original):
            logger.warning("Skipping staged restore because destination already exists: %s", original)
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, original)
            sync_directory(original.parent)
            sync_directory(staged.parent)
        except OSError:
            logger.warning("Failed to restore staged artifact from %s to %s", staged, original, exc_info=True)


def mark_delete_transaction_committed(entry: Path) -> None:
    marker = entry / ".committed"
    marker.touch()
    sync_file(marker)
    sync_directory(entry)


def preserve_delete_transaction(entry: Path) -> None:
    marker = entry / DELETE_PRESERVE_MARKER
    with marker.open("ab") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(entry)


def preserve_delete_transactions(root: Path) -> None:
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        return
    for entry in root.iterdir():
        manifest = entry / DELETE_MANIFEST
        if entry.is_symlink() or entry.is_junction() or manifest.is_symlink() or not manifest.is_file():
            continue
        preserve_delete_transaction(entry)


def write_delete_transaction(entry: Path, book_id: int, artifacts: list[tuple[Path, Path]]) -> None:
    payload = {
        "book_id": book_id,
        "artifacts": [{"original": str(original), "staged": str(staged)} for original, staged in artifacts],
    }
    atomic_write_json(
        entry / DELETE_MANIFEST, payload,
        temporary_path=entry / f"{DELETE_MANIFEST}.tmp",
        cleanup="none", sync_parent=sync_directory,
    )
    for directory in (entry.parent, entry.parent.parent):
        sync_directory(directory)


def read_delete_transaction(entry: Path, *, allowed_roots: tuple[Path, ...]) -> tuple[int, list[tuple[Path, Path]]]:
    manifest = entry / DELETE_MANIFEST
    if entry.is_symlink() or entry.is_junction() or manifest.is_symlink():
        raise ValueError("Invalid deletion transaction path")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    book_id = payload["book_id"]
    # 0 (UNOWNED_FOLDER_BOOK_ID) marks a folder no book owned; no row ever has it.
    if not isinstance(book_id, int) or isinstance(book_id, bool) or book_id < 0:
        raise ValueError("Invalid deletion transaction book")
    artifacts = []
    for item in payload["artifacts"]:
        original, staged = Path(item["original"]), Path(item["staged"])
        if (
            not original.is_absolute() or not staged.is_absolute()
            or not any(original.resolve().is_relative_to(root.resolve()) for root in allowed_roots)
            or staged.parent != original.parent / LOCAL_DELETE_STAGING / entry.name
            or staged.is_symlink()
            or any(path.is_symlink() or path.is_junction() for path in (staged.parent, staged.parent.parent))
        ):
            raise ValueError("Invalid deletion artifact path")
        artifacts.append((original, staged))
    return book_id, artifacts


def _retry_read_only_removal(function, raw_path: str, error: BaseException) -> None:
    path = Path(raw_path)
    if (
        os.name != "nt" or not isinstance(error, PermissionError)
        or path.is_symlink() or path.is_junction()
        or getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(raw_path)


def remove_staged_artifacts(artifacts: list[tuple[Path, Path]]) -> None:
    for _original, staged in artifacts:
        if not staged.exists():
            continue
        if staged.is_dir():
            shutil.rmtree(staged, onexc=_retry_read_only_removal)
        else:
            unlink_file(staged)
        sync_directory(staged.parent)


def finish_delete_transaction(entry: Path, artifacts: list[tuple[Path, Path]]) -> bool:
    if any(staged.exists() for _original, staged in artifacts):
        return False
    for parent in {staged.parent for _original, staged in artifacts}:
        if parent.exists():
            parent.rmdir()
        try:
            parent.parent.rmdir()
        except OSError:
            pass
    try:
        (entry / DELETE_MANIFEST).unlink(missing_ok=True)
        (entry / ".committed").unlink(missing_ok=True)
        (entry / DELETE_PRESERVE_MARKER).unlink(missing_ok=True)
    except PermissionError:
        # A sharing violation can outlast recovery; the stale sweep retries it.
        return False
    if not any(entry.iterdir()):
        entry.rmdir()
        sync_directory(entry.parent)
    return True
