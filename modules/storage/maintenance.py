"""
Database/storage maintenance helpers for auditing and aggressive pruning.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from shutil import copy2, rmtree
import json
import sqlite3
import time
from typing import Any, Optional

from modules.pipeline.book_workspace import sanitize_book_title
from modules.storage.models import PlaybackStatus

# Output sub-directories owned by the maintenance tooling itself (e.g. prune
# report JSON). These must never be treated as orphan/stale artifacts, otherwise
# a later prune run would delete the prior run's own audit history.
RESERVED_OUTPUT_DIR_NAMES = frozenset({"reports"})

# Artifacts newer than this are presumed to belong to queued or in-flight work
# (uploads awaiting conversion, freshly copied sources, output being written)
# that has no database reference yet, and are never classified stale/orphaned.
DEFAULT_CLEANUP_GRACE_SECONDS = 24 * 60 * 60

APP_OWNED_DB_PARENT_NAMES = ("data", "state")


def workspace_root_for_db(db_path: Path) -> Path:
    if db_path.parent.name in APP_OWNED_DB_PARENT_NAMES:
        return db_path.parent.parent.resolve()
    return db_path.parent.resolve()


def resolve_stored_path(
    raw: Optional[str],
    workspace_root: Path,
    *,
    output_root: Path | str | None = None,
    temp_root: Path | str | None = None,
) -> Optional[Path]:
    if not raw:
        return None

    path = Path(raw).expanduser()
    if path.is_absolute():
        return path

    if output_root and path.parts[:1] == ("output",):
        remainder = Path(*path.parts[1:]) if len(path.parts) > 1 else Path()
        return (Path(output_root).expanduser() / remainder).resolve()

    if temp_root and path.parts[:1] == ("temp",):
        remainder = Path(*path.parts[1:]) if len(path.parts) > 1 else Path()
        return (Path(temp_root).expanduser() / remainder).resolve()

    return (workspace_root / path).resolve()


def path_is_within_root(path: Path | str, root: Path | str) -> bool:
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
    except ValueError:
        return False
    return True


def resolve_app_owned_sources_root(
    workspace_root: Path | str,
    db_path: Path | str | None = None,
) -> Path:
    """Return the app-owned durable sources directory for the active database."""
    root = Path(workspace_root).expanduser().resolve()
    if db_path is not None:
        return (Path(db_path).expanduser().resolve().parent / "sources").resolve()
    for parent_name in APP_OWNED_DB_PARENT_NAMES:
        parent = root / parent_name
        if parent.is_dir():
            return (parent / "sources").resolve()
    return (root / "data" / "sources").resolve()


def canonical_sources_dir(db_path: Path | str) -> Path:
    """Return ``{db_parent}/sources`` for the database file location."""
    return Path(db_path).expanduser().resolve().parent / "sources"


def is_app_owned_durable_source(path: Path | str, sources_root: Path | str) -> bool:
    """True when ``path`` resolves inside the app-owned durable sources root."""
    return path_is_within_root(path, sources_root)


def unique_destination_path(base_path: Path) -> Path:
    if not base_path.exists():
        return base_path

    stem = base_path.stem
    suffix = base_path.suffix
    parent = base_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


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
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = unique_destination_path(target_dir / f"{source.stem}_{stamp}{source.suffix}")
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    return target


def _newest_mtime(path: Path) -> float:
    """Return the newest mtime of ``path`` or anything beneath it.

    Errors (e.g. a concurrent delete) yield +inf so the caller treats the
    path as recent and leaves it alone — never the other way around.
    """
    try:
        newest = path.stat().st_mtime
        if path.is_dir():
            for child in path.rglob("*"):
                newest = max(newest, child.stat().st_mtime)
    except OSError:
        return float("inf")
    return newest


def _recency_filter(paths, cutoff: Optional[float]) -> list[str]:
    if cutoff is None:
        return sorted(str(path) for path in paths)
    return sorted(str(path) for path in paths if _newest_mtime(Path(path)) < cutoff)


def _delete_directory_and_verify(path: Path) -> None:
    rmtree(path)
    if path.exists():
        raise OSError(f"Failed to delete directory: {path}")


def _delete_file_and_verify(path: Path) -> None:
    path.unlink()
    if path.exists():
        raise OSError(f"Failed to delete file: {path}")


def audit_storage_state(
    db_path: Path | str,
    output_dir: Path | str = "output",
    temp_dir: Path | str = "temp",
    grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
) -> dict[str, Any]:
    """
    Build a storage audit report (rows, stale pointers, orphan artifacts).

    Unreferenced filesystem artifacts modified within ``grace_seconds`` are
    not reported as stale/orphaned: uploads queued for conversion and
    in-flight conversion output exist before any database row references
    them. Pass ``grace_seconds=0`` to disable the recency exemption.
    """
    recency_cutoff = time.time() - grace_seconds if grace_seconds > 0 else None
    db_file = Path(db_path)
    workspace_root = workspace_root_for_db(db_file)
    output_root = resolve_stored_path(str(output_dir), workspace_root)
    temp_root = resolve_stored_path(str(temp_dir), workspace_root)
    assert output_root is not None
    assert temp_root is not None
    durable_sources_root = canonical_sources_dir(db_file).resolve()

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        row_counts = {
            "books": conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
            "chapters": conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0],
        }

        books = conn.execute(
            """
            SELECT id, title, source_path, playback_path, playback_status
            FROM books
            ORDER BY id
            """
        ).fetchall()
        chapters = conn.execute(
            "SELECT id, book_id, chapter_number, mp3_path FROM chapters ORDER BY id"
        ).fetchall()

        stale_chapter_rows: list[dict[str, Any]] = []
        referenced_output_dirs: set[str] = set()

        def add_output_reference(raw_path: Optional[str]) -> Path | None:
            resolved_path = resolve_stored_path(
                raw_path,
                workspace_root,
                output_root=output_root,
                temp_root=temp_root,
            )
            if resolved_path is None:
                return None
            try:
                relative = resolved_path.resolve().relative_to(output_root.resolve())
            except ValueError:
                return resolved_path
            if relative.parts:
                referenced_output_dirs.add(str((output_root / relative.parts[0]).resolve()))
            return resolved_path

        playable_book_ids: set[int] = set()
        for row in books:
            playback_path = add_output_reference(row["playback_path"])
            if playback_path and playback_path.exists() and playback_path.is_file():
                playable_book_ids.add(int(row["id"]))

        for row in chapters:
            raw_path = row["mp3_path"]
            if raw_path:
                mp3_path = add_output_reference(raw_path)
                if mp3_path is None:
                    continue
                if not mp3_path.exists() and int(row["book_id"]) not in playable_book_ids:
                    stale_chapter_rows.append(
                        {
                            "id": row["id"],
                            "book_id": row["book_id"],
                            "chapter_number": row["chapter_number"],
                            "mp3_path": raw_path,
                        }
                    )
            elif int(row["book_id"]) not in playable_book_ids:
                stale_chapter_rows.append(
                    {
                        "id": row["id"],
                        "book_id": row["book_id"],
                        "chapter_number": row["chapter_number"],
                        "mp3_path": None,
                    }
                )

        stale_book_sources: list[dict[str, Any]] = []
        temp_upload_refs: set[Path] = set()
        durable_source_refs: set[Path] = set()
        for row in books:
            source_path = resolve_stored_path(
                row["source_path"],
                workspace_root,
                output_root=output_root,
                temp_root=temp_root,
            )
            if source_path is None:
                continue
            if not source_path.exists():
                stale_book_sources.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "source_path": row["source_path"],
                    }
                )
            if "web_uploads" in source_path.parts:
                temp_upload_refs.add(source_path.resolve())
            if is_app_owned_durable_source(source_path, durable_sources_root):
                durable_source_refs.add(source_path.resolve())

        actual_output_dirs = (
            {
                str(p.resolve())
                for p in output_root.iterdir()
                if p.is_dir() and p.name not in RESERVED_OUTPUT_DIR_NAMES
            }
            if output_root.exists()
            else set()
        )

        orphan_output_dirs = _recency_filter(
            actual_output_dirs - referenced_output_dirs, recency_cutoff
        )

        temp_upload_dir = temp_root / "web_uploads"
        actual_temp_uploads = (
            {p.resolve() for p in temp_upload_dir.iterdir() if p.is_file()}
            if temp_upload_dir.exists()
            else set()
        )
        stale_temp_uploads = _recency_filter(
            actual_temp_uploads - temp_upload_refs, recency_cutoff
        )

        actual_durable_sources = (
            {p.resolve() for p in durable_sources_root.iterdir() if p.is_file()}
            if durable_sources_root.exists()
            else set()
        )
        orphan_durable_sources = _recency_filter(
            actual_durable_sources - durable_source_refs, recency_cutoff
        )

        stale_temp_stage_dirs = _recency_filter(
            (path.resolve() for path in temp_root.glob("ch_*") if path.is_dir()),
            recency_cutoff,
        )

        delete_staging_root = temp_root / "delete_staging"
        orphan_delete_staging_dirs = _recency_filter(
            (
                path.resolve()
                for path in delete_staging_root.iterdir()
                if path.is_dir()
            ),
            recency_cutoff,
        ) if delete_staging_root.exists() else []

        return {
            "database_path": str(db_file.resolve()),
            "workspace_root": str(workspace_root),
            "row_counts": row_counts,
            "stale_chapter_rows": stale_chapter_rows,
            "stale_book_sources": stale_book_sources,
            "orphan_output_dirs": orphan_output_dirs,
            "stale_temp_uploads": stale_temp_uploads,
            "stale_temp_stage_dirs": stale_temp_stage_dirs,
            "orphan_delete_staging_dirs": orphan_delete_staging_dirs,
            "referenced_temp_uploads": sorted(str(path) for path in temp_upload_refs),
            "referenced_durable_sources": sorted(str(path) for path in durable_source_refs),
            "orphan_durable_sources": orphan_durable_sources,
        }
    finally:
        conn.close()


def _move_referenced_upload_sources(
    db_file: Path,
    workspace_root: Path,
    output_root: Path,
    temp_root: Path,
    canonical_sources_dir: Path,
) -> list[dict[str, Any]]:
    moved_sources: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        books = conn.execute("SELECT id, source_path FROM books").fetchall()
        for row in books:
            raw_source_path = row["source_path"]
            source_path = resolve_stored_path(
                raw_source_path,
                workspace_root,
                output_root=output_root,
                temp_root=temp_root,
            )
            if source_path is None:
                continue
            if not source_path.exists():
                continue
            if not path_is_within_root(source_path, Path(temp_root) / "web_uploads"):
                continue

            destination = unique_destination_path(canonical_sources_dir / source_path.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Copy first and delete the upload only after the DB commit: a
            # crash between the file transfer and the commit must leave the
            # row's source_path pointing at a file that still exists.
            copy2(str(source_path), str(destination))
            try:
                cursor = conn.execute(
                    """
                    UPDATE books
                    SET source_path = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND source_path = ?
                    """,
                    (str(destination), row["id"], raw_source_path),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.OperationalError(
                        f"Source move update affected {cursor.rowcount} rows for book {row['id']}"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                destination.unlink(missing_ok=True)
                raise
            try:
                source_path.unlink()
            except OSError:
                # Best-effort: the committed copy is canonical; a leftover
                # upload is swept later as a stale temp upload.
                pass

            moved_sources.append(
                {"book_id": row["id"], "from": str(source_path), "to": str(destination)}
            )
    finally:
        conn.close()

    return moved_sources


# Durable, self-contained playback artifacts. When a book row loses its DB
# playback metadata (e.g. after a schema migration that intentionally does not
# probe disk) these files on disk still represent a fully converted audiobook and
# must never be pruned as if the book were orphaned.
DURABLE_PLAYBACK_SUFFIXES = (".m4b", ".m4a")


def _durable_audio_file_in_dir(directory: Path) -> Optional[Path]:
    """Return a durable playable file directly inside ``directory`` if present.

    ``.m4b`` (packaged single-file books) is preferred over ``.m4a`` and matches
    are sorted so the choice is deterministic across runs.
    """
    if not directory.is_dir():
        return None
    for suffix in DURABLE_PLAYBACK_SUFFIXES:
        matches = sorted(
            child
            for child in directory.iterdir()
            if child.is_file() and child.suffix.lower() == suffix
        )
        if matches:
            return matches[0]
    return None


def _candidate_book_output_dirs(
    *,
    title: Optional[str],
    playback_raw: Optional[str],
    chapter_raw_paths: list[Optional[str]],
    workspace_root: Path,
    output_root: Path,
    temp_root: Path,
) -> list[Path]:
    """Best-effort resolution of a book's on-disk output directory.

    Uses any resolvable playback/chapter paths first (their parent, minus a
    trailing ``chapters`` segment) and falls back to the title-derived directory
    name the conversion pipeline uses. Only directories inside the output root are
    returned, so a stale or traversing stored path can never redirect the scan
    outside managed storage.
    """
    output_root_resolved = output_root.resolve()
    candidates: list[Path] = []
    seen: set[str] = set()

    def _add_dir(directory: Path) -> None:
        resolved = directory.resolve()
        if resolved == output_root_resolved:
            return
        if not path_is_within_root(resolved, output_root_resolved):
            return
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            candidates.append(resolved)

    def _add_from_stored(raw: Optional[str]) -> None:
        resolved = resolve_stored_path(
            raw, workspace_root, output_root=output_root, temp_root=temp_root
        )
        if resolved is None:
            return
        directory = resolved if resolved.is_dir() else resolved.parent
        if directory.name == "chapters":
            directory = directory.parent
        _add_dir(directory)

    _add_from_stored(playback_raw)
    for raw in chapter_raw_paths:
        _add_from_stored(raw)

    safe_title = sanitize_book_title(title or "")
    if safe_title:
        _add_dir(output_root / safe_title)

    return candidates


def _find_ondisk_playback_artifact(
    *,
    title: Optional[str],
    playback_raw: Optional[str],
    chapter_raw_paths: list[Optional[str]],
    workspace_root: Path,
    output_root: Path,
    temp_root: Path,
) -> Optional[tuple[Path, str]]:
    """Locate a durable on-disk playback artifact for a book whose DB playback
    metadata is missing/stale.

    Returns ``(artifact_path, playback_status)`` — a top-level ``.m4b``/``.m4a``
    maps to ``packaged`` playback, per-chapter files under ``chapters/`` map to
    ``chapter_audio`` — or ``None`` when the book is genuinely orphaned on disk.
    """
    for directory in _candidate_book_output_dirs(
        title=title,
        playback_raw=playback_raw,
        chapter_raw_paths=chapter_raw_paths,
        workspace_root=workspace_root,
        output_root=output_root,
        temp_root=temp_root,
    ):
        packaged = _durable_audio_file_in_dir(directory)
        if packaged is not None:
            return packaged, PlaybackStatus.PACKAGED.value
        per_chapter = _durable_audio_file_in_dir(directory / "chapters")
        if per_chapter is not None:
            return per_chapter, PlaybackStatus.CHAPTER_AUDIO.value
    return None


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
    }

    conn = sqlite3.connect(db_file)
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
            ondisk_artifact = _find_ondisk_playback_artifact(
                title=book_row["title"],
                playback_raw=book_row["playback_path"],
                chapter_raw_paths=[row["mp3_path"] for row in chapter_rows],
                workspace_root=workspace_root,
                output_root=output_root,
                temp_root=temp_root,
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

            books_to_delete.append(int(book_row["id"]))

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
        if path.exists() and path.is_dir():
            _delete_directory_and_verify(path)
            actions["deleted_output_dirs"].append(str(path))

    for raw in audit_mid["stale_temp_uploads"]:
        path = Path(raw)
        if path.exists() and path.is_file():
            _delete_file_and_verify(path)
            actions["deleted_temp_uploads"].append(str(path))

    for raw in audit_mid["stale_temp_stage_dirs"]:
        path = Path(raw)
        if path.exists() and path.is_dir():
            _delete_directory_and_verify(path)
            actions["deleted_temp_stage_dirs"].append(str(path))

    for raw in audit_mid["orphan_durable_sources"]:
        path = Path(raw)
        if path.exists() and path.is_file():
            _delete_file_and_verify(path)
            actions["deleted_durable_sources"].append(str(path))

    for raw in audit_mid.get("orphan_delete_staging_dirs", []):
        path = Path(raw)
        if path.exists() and path.is_dir():
            _delete_directory_and_verify(path)
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
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"db_prune_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path.resolve())
    return report
