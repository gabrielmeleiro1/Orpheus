"""
Database/storage maintenance helpers for auditing and aggressive pruning.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from shutil import move, rmtree
import json
import sqlite3
from typing import Any, Optional


def workspace_root_for_db(db_path: Path) -> Path:
    if db_path.parent.name in {"data", "state"}:
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


def _unique_destination(base_path: Path) -> Path:
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
    target = _unique_destination(target_dir / f"{source.stem}_{stamp}{source.suffix}")
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    return target


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
) -> dict[str, Any]:
    """
    Build a storage audit report (rows, stale pointers, orphan artifacts).
    """
    db_file = Path(db_path)
    workspace_root = workspace_root_for_db(db_file)
    output_root = resolve_stored_path(str(output_dir), workspace_root)
    temp_root = resolve_stored_path(str(temp_dir), workspace_root)
    assert output_root is not None
    assert temp_root is not None

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

        actual_output_dirs = (
            {str(p.resolve()) for p in output_root.iterdir() if p.is_dir()}
            if output_root.exists()
            else set()
        )

        orphan_output_dirs = sorted(actual_output_dirs - referenced_output_dirs)

        temp_upload_dir = temp_root / "web_uploads"
        actual_temp_uploads = (
            {p.resolve() for p in temp_upload_dir.iterdir() if p.is_file()}
            if temp_upload_dir.exists()
            else set()
        )
        stale_temp_uploads = sorted(
            str(path) for path in (actual_temp_uploads - temp_upload_refs)
        )

        stale_temp_stage_dirs = sorted(
            str(path.resolve())
            for path in temp_root.glob("ch_*")
            if path.is_dir()
        )

        return {
            "database_path": str(db_file.resolve()),
            "workspace_root": str(workspace_root),
            "row_counts": row_counts,
            "stale_chapter_rows": stale_chapter_rows,
            "stale_book_sources": stale_book_sources,
            "orphan_output_dirs": orphan_output_dirs,
            "stale_temp_uploads": stale_temp_uploads,
            "stale_temp_stage_dirs": stale_temp_stage_dirs,
            "referenced_temp_uploads": sorted(str(path) for path in temp_upload_refs),
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
            if "web_uploads" not in source_path.parts:
                continue

            destination = _unique_destination(canonical_sources_dir / source_path.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            move(str(source_path), str(destination))
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
                if destination.exists() and not source_path.exists():
                    source_path.parent.mkdir(parents=True, exist_ok=True)
                    move(str(destination), str(source_path))
                raise

            moved_sources.append(
                {"book_id": row["id"], "from": str(source_path), "to": str(destination)}
            )
    finally:
        conn.close()

    return moved_sources


def prune_stale_records_and_files(
    db_path: Path | str,
    output_dir: Path | str = "output",
    temp_dir: Path | str = "temp",
    aggressive: bool = False,
) -> dict[str, Any]:
    """
    Aggressively prune stale storage rows/artifacts and emit a JSON report.
    """
    if not aggressive:
        raise ValueError("Refusing prune operation without aggressive=True")

    db_file = Path(db_path)
    workspace_root = workspace_root_for_db(db_file)
    output_root = resolve_stored_path(str(output_dir), workspace_root)
    temp_root = resolve_stored_path(str(temp_dir), workspace_root)
    assert output_root is not None
    assert temp_root is not None
    canonical_sources_dir = (db_file.parent / "sources").resolve()
    canonical_sources_dir.mkdir(parents=True, exist_ok=True)

    audit_before = audit_storage_state(db_file, output_dir=output_root, temp_dir=temp_root)
    backup_path = backup_database(db_file)

    actions: dict[str, Any] = {
        "deleted_chapter_rows": [],
        "deleted_book_rows": [],
        "deleted_output_dirs": [],
        "deleted_temp_uploads": [],
        "deleted_temp_stage_dirs": [],
        "moved_sources": [],
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
        book_rows = conn.execute("SELECT id, playback_path FROM books").fetchall()
        books_to_delete: list[int] = []
        for book_row in book_rows:
            playback_path = resolve_stored_path(
                book_row["playback_path"],
                workspace_root,
                output_root=output_root,
                temp_root=temp_root,
            )
            has_playable = bool(
                playback_path and playback_path.exists() and playback_path.is_file()
            )
            if has_playable:
                continue

            chapter_rows = conn.execute(
                """
                SELECT mp3_path FROM chapters
                WHERE book_id = ? AND mp3_path IS NOT NULL AND mp3_path != ''
                """,
                (book_row["id"],),
            ).fetchall()
            has_playable = False
            for chapter_row in chapter_rows:
                chapter_path = resolve_stored_path(
                    chapter_row["mp3_path"],
                    workspace_root,
                    output_root=output_root,
                    temp_root=temp_root,
                )
                if chapter_path and chapter_path.exists():
                    has_playable = True
                    break
            if not has_playable:
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
        canonical_sources_dir=canonical_sources_dir,
    )

    # Delete orphan output directories and stale temp artifacts.
    audit_mid = audit_storage_state(db_file, output_dir=output_root, temp_dir=temp_root)
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

    report = {
        "timestamp": datetime.now().isoformat(),
        "database_path": str(db_file.resolve()),
        "backup_path": str(backup_path.resolve()),
        "aggressive": aggressive,
        "audit_before": audit_before,
        "audit_after": audit_storage_state(db_file, output_dir=output_root, temp_dir=temp_root),
        "actions": actions,
    }

    reports_dir = output_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"db_prune_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path.resolve())
    return report
