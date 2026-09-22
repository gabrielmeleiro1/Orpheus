"""
Database/storage maintenance helpers for auditing and aggressive pruning.

This module owns backup/retention, the orphan-deletion primitives and the
prune driver, and is the single import surface for maintenance: the path,
audit and reconciliation halves live in ``maintenance_paths``,
``maintenance_audit`` and ``maintenance_reconcile`` and are re-exported here.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from shutil import rmtree
import json
import sqlite3
from typing import Any

from modules.storage.maintenance_audit import (
    _newest_mtime,
    _recency_filter,
    audit_storage_state,
)
from modules.storage.maintenance_paths import (
    APP_OWNED_DB_PARENT_NAMES,
    BUSY_TIMEOUT_MS,
    DEFAULT_CLEANUP_GRACE_SECONDS,
    RESERVED_OUTPUT_DIR_NAMES,
    _connect,
    canonical_sources_dir,
    is_app_owned_durable_source,
    path_is_within_root,
    resolve_app_owned_sources_root,
    resolve_stored_path,
    unique_destination_path,
    workspace_root_for_db,
)
from modules.storage.maintenance_reconcile import (
    AUDIO_ARTIFACT_SUFFIXES,
    DURABLE_PLAYBACK_SUFFIXES,
    _candidate_book_output_dirs,
    _dir_contains_finished_audio,
    _dir_has_direct_audio,
    _durable_audio_file_in_dir,
    _find_ondisk_playback_artifact,
    _move_referenced_upload_sources,
    _referenced_output_dir_keys,
    _unclaimed_finished_audio_dirs,
)
from modules.storage.permissions import ensure_owner_only_directory, owner_only

__all__ = [
    "APP_OWNED_DB_PARENT_NAMES",
    "AUDIO_ARTIFACT_SUFFIXES",
    "BUSY_TIMEOUT_MS",
    "DEFAULT_CLEANUP_GRACE_SECONDS",
    "DEFAULT_RETENTION_KEEP",
    "DURABLE_PLAYBACK_SUFFIXES",
    "RESERVED_OUTPUT_DIR_NAMES",
    "audit_storage_state",
    "backup_database",
    "canonical_sources_dir",
    "is_app_owned_durable_source",
    "path_is_within_root",
    "prune_stale_records_and_files",
    "resolve_app_owned_sources_root",
    "resolve_stored_path",
    "unique_destination_path",
    "workspace_root_for_db",
    # Internal helpers kept importable from this module for callers that
    # reached for them before the split.
    "_candidate_book_output_dirs",
    "_connect",
    "_dir_contains_finished_audio",
    "_dir_has_direct_audio",
    "_durable_audio_file_in_dir",
    "_find_ondisk_playback_artifact",
    "_move_referenced_upload_sources",
    "_newest_mtime",
    "_recency_filter",
    "_referenced_output_dir_keys",
    "_unclaimed_finished_audio_dirs",
]


# Retention cap for our own generated artifacts (timestamped DB backups and
# prune report JSON). Without a cap these directories grow without bound over a
# library's lifetime. Backups double as prune's safety net, so keep a generous
# window rather than a tight one.
DEFAULT_RETENTION_KEEP = 30


def _enforce_retention(
    directory: Path, glob_pattern: str, keep: int | None = None
) -> list[str]:
    """Keep only the ``keep`` newest regular files matching ``glob_pattern``.

    ``keep`` defaults to :data:`DEFAULT_RETENTION_KEEP`, read at call time so the
    cap stays configurable. Only plain files matching the pattern are ever
    removed; symlinks, subdirectories and unrelated files are never touched.
    Best-effort — any deletion error is swallowed so retention can never break the
    caller.
    """
    if keep is None:
        keep = DEFAULT_RETENTION_KEEP
    if keep <= 0 or not directory.is_dir():
        return []
    try:
        matches = [p for p in directory.glob(glob_pattern) if p.is_file() and not p.is_symlink()]
    except OSError:
        return []
    if len(matches) <= keep:
        return []
    timestamped_matches: list[tuple[float, Path]] = []
    for match in matches:
        try:
            timestamped_matches.append((match.stat().st_mtime, match))
        except OSError:
            # A concurrent delete, permission change, or filesystem race should
            # preserve the ambiguous entry and must never fail backup/prune.
            continue
    timestamped_matches.sort(key=lambda item: item[0], reverse=True)
    removed: list[str] = []
    for _, stale in timestamped_matches[keep:]:
        try:
            stale.unlink()
            removed.append(str(stale))
        except OSError:
            continue
    return removed


def backup_database(
    db_path: Path | str,
    backup_dir: Path | str | None = None,
) -> Path:
    """
    Create timestamped SQLite backup and return backup file path.
    """
    source = Path(db_path)
    if not source.exists():
        raise FileNotFoundError(f"Database not found: {source}")

    target_dir = Path(backup_dir) if backup_dir else source.parent / "backups"
    ensure_owner_only_directory(target_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = unique_destination_path(target_dir / f"{source.stem}_{stamp}{source.suffix}")
    source_conn = _connect(source)
    target_conn = _connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    owner_only(target)
    # Cap the backups directory so it does not grow without bound; the just-made
    # backup is newest and always retained.
    _enforce_retention(target_dir, f"{source.stem}_*{source.suffix}")
    return target


def _delete_directory_and_verify(path: Path) -> None:
    rmtree(path)
    if path.exists():
        raise OSError(f"Failed to delete directory: {path}")


def _delete_file_and_verify(path: Path) -> None:
    path.unlink()
    if path.exists():
        raise OSError(f"Failed to delete file: {path}")


def _delete_symlink_and_verify(path: Path) -> None:
    """Remove a symlink itself, never following it to its (possibly external)
    target. ``Path.unlink`` deletes the link entry, so a symlink that an audit
    classified as an orphan is cleaned up without touching whatever it points
    at outside the managed roots."""
    path.unlink()
    if path.is_symlink() or path.exists():
        raise OSError(f"Failed to delete symlink: {path}")


def _is_within_root_no_follow(path: Path, root: Path) -> bool:
    """Lexical containment check that never dereferences symlinks.

    Both operands originate from the managed roots (audit lists only direct
    children of the resolved roots), so a plain ``relative_to`` is a sufficient
    guard against deleting anything outside the managed storage without
    resolving — resolving would follow a symlinked entry to its real target."""
    try:
        Path(path).expanduser().relative_to(Path(root).expanduser())
    except ValueError:
        return False
    return True


def _delete_orphan_entry(path: Path, root: Path, *, expect_dir: bool) -> bool:
    """Delete an orphan artifact under ``root`` safely; return whether removed.

    A symlink is unlinked (the link only, never its target). A real entry is
    verified to live under ``root`` without dereferencing before removal, so a
    symlinked or traversing path can never redirect deletion outside managed
    storage.
    """
    if path.is_symlink():
        _delete_symlink_and_verify(path)
        return True
    if not _is_within_root_no_follow(path, root):
        return False
    if expect_dir:
        if path.exists() and path.is_dir():
            _delete_directory_and_verify(path)
            return True
    elif path.exists() and path.is_file():
        _delete_file_and_verify(path)
        return True
    return False


def prune_stale_records_and_files(
    db_path: Path | str,
    output_dir: Path | str = "output",
    temp_dir: Path | str = "temp",
    aggressive: bool = False,
    grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
) -> dict[str, Any]:
    """
    Aggressively prune stale storage rows/artifacts and emit a JSON report.

    Filesystem artifacts modified within ``grace_seconds`` are preserved even
    when unreferenced; see :func:`audit_storage_state`.
    """
    if not aggressive:
        raise ValueError("Refusing prune operation without aggressive=True")

    db_file = Path(db_path)
    workspace_root = workspace_root_for_db(db_file)
    output_root = resolve_stored_path(str(output_dir), workspace_root)
    temp_root = resolve_stored_path(str(temp_dir), workspace_root)
    assert output_root is not None
    assert temp_root is not None
    sources_dir = canonical_sources_dir(db_file)
    sources_dir.mkdir(parents=True, exist_ok=True)

    audit_before = audit_storage_state(
        db_file, output_dir=output_root, temp_dir=temp_root, grace_seconds=grace_seconds
    )
    backup_path = backup_database(db_file)

    actions: dict[str, Any] = {
        "deleted_chapter_rows": [],
        "deleted_book_rows": [],
        "deleted_output_dirs": [],
        "deleted_temp_uploads": [],
        "deleted_temp_stage_dirs": [],
        "deleted_delete_staging_dirs": [],
        "deleted_durable_sources": [],
        "moved_sources": [],
        "reconciled_playback": [],
        "preserved_book_rows": [],
        "preserved_output_dirs": [],
        "unclaimed_audio_dirs": [],
    }

    conn = _connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        stale_chapter_ids = [int(row["id"]) for row in audit_before["stale_chapter_rows"]]
        if stale_chapter_ids:
            placeholders = ",".join(["?"] * len(stale_chapter_ids))
            conn.execute(
                f"DELETE FROM chapters WHERE id IN ({placeholders})",
                stale_chapter_ids,
            )
            actions["deleted_chapter_rows"] = stale_chapter_ids

        # Delete books without any playable chapter or packaged playback file.
        book_rows = conn.execute("SELECT id, title, playback_path FROM books").fetchall()
        books_to_delete: list[int] = []
        for book_row in book_rows:
            playback_path = resolve_stored_path(
                book_row["playback_path"],
                workspace_root,
                output_root=output_root,
                temp_root=temp_root,
            )
            if playback_path and playback_path.exists() and playback_path.is_file():
                continue

            chapter_rows = conn.execute(
                """
                SELECT mp3_path FROM chapters
                WHERE book_id = ? AND mp3_path IS NOT NULL AND mp3_path != ''
                """,
                (book_row["id"],),
            ).fetchall()
            has_playable_chapter = False
            for chapter_row in chapter_rows:
                chapter_path = resolve_stored_path(
                    chapter_row["mp3_path"],
                    workspace_root,
                    output_root=output_root,
                    temp_root=temp_root,
                )
                if chapter_path and chapter_path.exists():
                    has_playable_chapter = True
                    break
            if has_playable_chapter:
                continue

            # The DB has no usable playback metadata, but a durable audiobook may
            # still exist on disk (e.g. a packaged .m4b whose playback_path was
            # cleared by a schema migration that never probes disk). Refuse to
            # delete such a book and reconcile its playback metadata so the row is
            # playable again and its output directory stays referenced (otherwise
            # the orphan-output-dir sweep below would still rmtree the audio).
            #
            # The title-derived directory guess is only trusted for books that
            # carry no stored pointer at all (the genuine post-migration case). A
            # book whose stored playback/chapter pointer merely failed to resolve
            # (a rename, or a data-dir move that invalidated absolute paths) must
            # not be reconciled by title, or it can be cross-wired onto a
            # different book's audio.
            has_stored_pointer = bool(
                (book_row["playback_path"] or "").strip()
            ) or bool(chapter_rows)
            ondisk_artifact = _find_ondisk_playback_artifact(
                title=book_row["title"],
                playback_raw=book_row["playback_path"],
                chapter_raw_paths=[row["mp3_path"] for row in chapter_rows],
                workspace_root=workspace_root,
                output_root=output_root,
                temp_root=temp_root,
                allow_title_fallback=not has_stored_pointer,
            )
            if ondisk_artifact is not None:
                artifact_path, playback_status = ondisk_artifact
                conn.execute(
                    """
                    UPDATE books
                    SET playback_path = ?,
                        playback_status = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (str(artifact_path), playback_status, int(book_row["id"])),
                )
                actions["reconciled_playback"].append(
                    {
                        "book_id": int(book_row["id"]),
                        "playback_path": str(artifact_path),
                        "playback_status": playback_status,
                    }
                )
                continue

            if int(book_row["id"]) in audit_before["recoverable_book_ids"]:
                actions["preserved_book_rows"].append(int(book_row["id"]))
                continue
            books_to_delete.append(int(book_row["id"]))

        # Safety gate before deleting any fileless row: if a finished-audio
        # directory exists on disk that no surviving book claims (reconciliations
        # above are already reflected on ``conn``), it may be the audio of a
        # moved/renamed book we could not link. We are then *not* confident these
        # rows are truly orphaned, so we keep them rather than destroy library
        # metadata for a book whose audio still exists. Genuine orphans (no
        # unclaimed audio anywhere) are still deleted.
        if books_to_delete:
            unclaimed_audio = _unclaimed_finished_audio_dirs(
                output_root,
                _referenced_output_dir_keys(
                    conn, workspace_root, output_root, temp_root
                ),
            )
            if unclaimed_audio:
                actions["preserved_book_rows"].extend(books_to_delete)
                actions["unclaimed_audio_dirs"] = unclaimed_audio
                books_to_delete = []

        if books_to_delete:
            placeholders = ",".join(["?"] * len(books_to_delete))
            conn.execute(f"DELETE FROM books WHERE id IN ({placeholders})", books_to_delete)
            actions["deleted_book_rows"] = books_to_delete

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    actions["moved_sources"] = _move_referenced_upload_sources(
        db_file=db_file,
        workspace_root=workspace_root,
        output_root=output_root,
        temp_root=temp_root,
        canonical_sources_dir=sources_dir,
    )

    # Delete orphan output directories and stale temp artifacts.
    audit_mid = audit_storage_state(
        db_file, output_dir=output_root, temp_dir=temp_root, grace_seconds=grace_seconds
    )
    for raw in audit_mid["orphan_output_dirs"]:
        path = Path(raw)
        # Never rmtree a real directory that still holds finished audio, even when
        # unreferenced: it may be a moved/renamed book's audiobook that lost its DB
        # linkage. Leaving a stale directory is infinitely preferable to destroying
        # a finished audiobook. Symlinked entries are not protected here — they are
        # unlinked (link only) by _delete_orphan_entry below.
        if _dir_contains_finished_audio(path):
            actions["preserved_output_dirs"].append(str(path))
            continue
        if _delete_orphan_entry(path, output_root, expect_dir=True):
            actions["deleted_output_dirs"].append(str(path))

    temp_upload_dir = temp_root / "web_uploads"
    for raw in audit_mid["stale_temp_uploads"]:
        path = Path(raw)
        if _delete_orphan_entry(path, temp_upload_dir, expect_dir=False):
            actions["deleted_temp_uploads"].append(str(path))

    for raw in audit_mid["stale_temp_stage_dirs"]:
        path = Path(raw)
        if _delete_orphan_entry(path, temp_root, expect_dir=True):
            actions["deleted_temp_stage_dirs"].append(str(path))

    for raw in audit_mid["orphan_durable_sources"]:
        path = Path(raw)
        if _delete_orphan_entry(path, sources_dir, expect_dir=False):
            actions["deleted_durable_sources"].append(str(path))

    for raw in audit_mid.get("orphan_delete_staging_dirs", []):
        path = Path(raw)
        if _delete_orphan_entry(path, temp_root / "delete_staging", expect_dir=True):
            actions["deleted_delete_staging_dirs"].append(str(path))

    delete_staging_root = temp_root / "delete_staging"
    if delete_staging_root.exists() and delete_staging_root.is_dir():
        try:
            if not any(delete_staging_root.iterdir()):
                delete_staging_root.rmdir()
        except OSError:
            pass

    report = {
        "timestamp": datetime.now().isoformat(),
        "database_path": str(db_file.resolve()),
        "backup_path": str(backup_path.resolve()),
        "aggressive": aggressive,
        "audit_before": audit_before,
        "audit_after": audit_storage_state(
            db_file, output_dir=output_root, temp_dir=temp_root, grace_seconds=grace_seconds
        ),
        "actions": actions,
    }

    reports_dir = output_root / "reports"
    ensure_owner_only_directory(reports_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"db_prune_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    owner_only(report_path)
    report["report_path"] = str(report_path.resolve())
    # Cap the reports directory; the report just written is newest and retained.
    _enforce_retention(reports_dir, "db_prune_*.json")
    return report
