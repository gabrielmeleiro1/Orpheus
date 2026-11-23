"""
Storage audit: report rows, stale pointers and orphan artifacts.

Read-only counterpart to the prune half of
:mod:`modules.storage.maintenance`, which re-exports everything here.
"""

from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import time
from typing import Any, Optional

from modules.storage.deletion import DELETE_MANIFEST, read_delete_transaction
from modules.storage.maintenance_paths import (
    DEFAULT_CLEANUP_GRACE_SECONDS,
    RESERVED_OUTPUT_DIR_NAMES,
    _connect,
    canonical_sources_dir,
    resolve_stored_path,
    workspace_root_for_db,
)


def _path_comparison_key(path: Path | str) -> str:
    return os.path.normcase(str(path)).casefold()


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
    return audit_storage_state_with_unowned(db_path, output_dir, temp_dir, grace_seconds)[0]


def audit_storage_state_with_unowned(
    db_path: Path | str,
    output_dir: Path | str = "output",
    temp_dir: Path | str = "temp",
    grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
) -> tuple[dict[str, Any], list[str]]:
    """:func:`audit_storage_state` plus every unreferenced output entry.

    The second value is ``orphan_output_dirs`` without the recency exemption,
    as ``grace_seconds=0`` reports it, from the same pass.
    """
    recency_cutoff = time.time() - grace_seconds if grace_seconds > 0 else None
    db_file = Path(db_path)
    workspace_root = workspace_root_for_db(db_file)
    output_root = resolve_stored_path(str(output_dir), workspace_root)
    temp_root = resolve_stored_path(str(temp_dir), workspace_root)
    assert output_root is not None
    assert temp_root is not None
    # Keep each audit's canonical comparison roots fixed. Resolving them for
    # every row repeats the same filesystem work on large libraries.
    canonical_output_root = output_root.resolve()
    durable_sources_root = canonical_sources_dir(db_file).resolve()
    output_root_key = Path(_path_comparison_key(canonical_output_root))
    durable_sources_root_key = Path(_path_comparison_key(durable_sources_root))

    conn = _connect(db_file, read_only=True)
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
                relative = Path(_path_comparison_key(resolved_path.resolve())).relative_to(
                    output_root_key
                )
            except ValueError:
                return resolved_path
            if relative.parts:
                # Key on the resolved root plus the entry name. Resolving the
                # whole entry would dereference a symlinked book directory and
                # stop it matching the listing below.
                referenced_output_dirs.add(
                    _path_comparison_key(canonical_output_root / relative.parts[0])
                )
            return resolved_path

        playable_book_ids: set[int] = set()
        recoverable_book_ids: set[int] = set()
        delete_staging_root = temp_root / "delete_staging"
        if delete_staging_root.is_dir() and not delete_staging_root.is_symlink():
            for entry in delete_staging_root.iterdir():
                try:
                    book_id, _artifacts = read_delete_transaction(
                        entry, allowed_roots=(output_root, temp_root, durable_sources_root),
                    )
                    recoverable_book_ids.add(book_id)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    continue
        for row in books:
            # Interrupted conversions can own only their durable source/inventory.
            source_path = add_output_reference(row["source_path"])
            if source_path is not None:
                from modules.contracts.recovery_inventory import recovery_audio_paths

                if source_path.is_file() or recovery_audio_paths(source_path):
                    recoverable_book_ids.add(int(row["id"]))
            playback_path = add_output_reference(row["playback_path"])
            if playback_path and playback_path.exists() and playback_path.is_file():
                playable_book_ids.add(int(row["id"]))

        for row in chapters:
            raw_path = row["mp3_path"]
            if int(row["book_id"]) in recoverable_book_ids:
                add_output_reference(raw_path)
                continue
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
            canonical_source = source_path.resolve()
            if "web_uploads" in Path(_path_comparison_key(source_path)).parts:
                temp_upload_refs.add(canonical_source)
            if Path(_path_comparison_key(canonical_source)).is_relative_to(
                durable_sources_root_key
            ):
                durable_source_refs.add(canonical_source)

        # Classify by each entry's own (non-dereferenced) path. A symlink is
        # reported by its link path — never resolved to its target — so the
        # delete phase unlinks the link instead of rmtree-ing whatever it
        # points at outside the managed roots.
        # Map a canonical key (resolved root + entry name) to the entry's own
        # path, so the comparison survives a symlinked output root while the
        # delete phase still acts on the undereferenced path.
        actual_output_dirs = (
            {
                str(canonical_output_root / p.name): str(p)
                for p in output_root.iterdir()
                if (p.is_symlink() or p.is_dir())
                and p.name not in RESERVED_OUTPUT_DIR_NAMES
            }
            if output_root.exists()
            else {}
        )

        unreferenced_output_dirs = [
            entry_path
            for key, entry_path in actual_output_dirs.items()
            if _path_comparison_key(key) not in referenced_output_dirs
        ]
        orphan_output_dirs = _recency_filter(unreferenced_output_dirs, recency_cutoff)

        temp_upload_dir = temp_root / "web_uploads"
        actual_temp_uploads = (
            {p for p in temp_upload_dir.iterdir() if p.is_symlink() or p.is_file()}
            if temp_upload_dir.exists()
            else set()
        )
        temp_upload_ref_keys = {_path_comparison_key(path) for path in temp_upload_refs}
        canonical_temp_upload_dir = temp_upload_dir.resolve()
        stale_temp_uploads = _recency_filter(
            (
                path for path in actual_temp_uploads
                if _path_comparison_key(canonical_temp_upload_dir / path.name)
                not in temp_upload_ref_keys
            ),
            recency_cutoff,
        )

        actual_durable_sources = (
            {p for p in durable_sources_root.iterdir()
             if not p.name.startswith(".orpheus-") and (p.is_symlink() or p.is_file())}
            if durable_sources_root.exists()
            else set()
        )
        durable_source_ref_keys = {_path_comparison_key(path) for path in durable_source_refs}
        orphan_durable_sources = _recency_filter(
            (
                path for path in actual_durable_sources
                if _path_comparison_key(path) not in durable_source_ref_keys
            ),
            recency_cutoff,
        )

        stale_temp_stage_dirs = _recency_filter(
            (
                path
                for path in temp_root.glob("ch_*")
                if path.is_symlink() or path.is_dir()
            ),
            recency_cutoff,
        )

        delete_staging_root = temp_root / "delete_staging"
        # A deletion whose row is gone may still lack its commit marker when
        # making it durable failed; cleanup settles it with the same policy.
        orphan_delete_staging_dirs = _recency_filter(
            (
                path
                for path in delete_staging_root.iterdir()
                if path.is_dir() and not path.is_symlink() and not path.is_junction()
                and any(
                    (path / name).is_file() and not (path / name).is_symlink()
                    for name in (".committed", DELETE_MANIFEST)
                )
            ),
            recency_cutoff,
        ) if delete_staging_root.exists() else []

        return {
            "database_path": str(db_file.resolve()),
            "workspace_root": str(workspace_root),
            "row_counts": row_counts,
            "stale_chapter_rows": stale_chapter_rows,
            "recoverable_book_ids": sorted(recoverable_book_ids),
            "stale_book_sources": stale_book_sources,
            "orphan_output_dirs": orphan_output_dirs,
            "stale_temp_uploads": stale_temp_uploads,
            "stale_temp_stage_dirs": stale_temp_stage_dirs,
            "orphan_delete_staging_dirs": orphan_delete_staging_dirs,
            "referenced_temp_uploads": sorted(str(path) for path in temp_upload_refs),
            "referenced_durable_sources": sorted(str(path) for path in durable_source_refs),
            "orphan_durable_sources": orphan_durable_sources,
        }, _recency_filter(unreferenced_output_dirs, None)
    finally:
        conn.close()
