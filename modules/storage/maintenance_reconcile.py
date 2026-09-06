"""
Reconciliation between database pointers and on-disk artifacts.

Relocates referenced uploads into app-owned durable storage and locates the
durable playback artifacts that keep prune from deleting finished audiobooks.
:mod:`modules.storage.maintenance` re-exports everything here.
"""

from __future__ import annotations

from pathlib import Path
from shutil import copy2
import sqlite3
from typing import Any, Optional

from modules.contracts.book_identity import sanitize_book_title
from modules.storage.maintenance_paths import (
    RESERVED_OUTPUT_DIR_NAMES,
    _connect,
    path_is_within_root,
    resolve_stored_path,
    unique_destination_path,
)
from modules.storage.models import PlaybackStatus


def _move_referenced_upload_sources(
    db_file: Path,
    workspace_root: Path,
    output_root: Path,
    temp_root: Path,
    canonical_sources_dir: Path,
) -> list[dict[str, Any]]:
    moved_sources: list[dict[str, Any]] = []
    conn = _connect(db_file)
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


# Any recognized audio artifact — packaged single-file books *or* per-chapter
# renders — marks a directory as holding finished conversion work. The orphan
# sweep must never rmtree such a directory (it may be a moved/renamed book whose
# row we could not match), even though only ``.m4b``/``.m4a`` are reconciled as a
# playback pointer. Includes ``.mp3`` so a chapter-audio library is protected too.
AUDIO_ARTIFACT_SUFFIXES = (".m4b", ".m4a", ".mp3")


def _dir_has_direct_audio(directory: Path) -> bool:
    """True when ``directory`` directly contains a recognized audio file.

    Missing paths yield ``False``. Any other inspection failure yields ``True``
    so destructive cleanup fails closed instead of deleting a directory whose
    contents could not be verified.
    """
    try:
        children = directory.iterdir()
        for child in children:
            try:
                is_file = child.is_file()
            except FileNotFoundError:
                continue
            except OSError:
                return True
            if is_file and child.suffix.lower() in AUDIO_ARTIFACT_SUFFIXES:
                return True
        return False
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True


def _dir_contains_finished_audio(directory: Path) -> bool:
    """True when a *real* directory holds finished audio (packaged or per-chapter).

    Symlinks are never followed (returns ``False``) so the orphan sweep still
    unlinks a symlinked entry rather than inspecting whatever it points at outside
    the managed roots. This is the guard that keeps prune from destroying a
    finished audiobook whose owning book row could not be matched.
    """
    if directory.is_symlink() or not directory.is_dir():
        return False
    return _dir_has_direct_audio(directory) or _dir_has_direct_audio(directory / "chapters")


def _referenced_output_dir_keys(
    conn: sqlite3.Connection,
    workspace_root: Path,
    output_root: Path,
    temp_root: Path,
) -> set[str]:
    """Return the canonical keys of output directories any book currently claims.

    A key is ``str(output_root.resolve() / <top-level entry name>)`` — the same
    keying the audit uses — computed from every book's playback path and every
    chapter's mp3 path (reflecting reconciliations already applied on ``conn``).
    """
    output_root_resolved = output_root.resolve()
    keys: set[str] = set()

    def _add(raw: Optional[str]) -> None:
        resolved = resolve_stored_path(
            raw, workspace_root, output_root=output_root, temp_root=temp_root
        )
        if resolved is None:
            return
        try:
            relative = resolved.resolve().relative_to(output_root_resolved)
        except ValueError:
            return
        if relative.parts:
            keys.add(str(output_root_resolved / relative.parts[0]))

    for row in conn.execute("SELECT playback_path FROM books").fetchall():
        _add(row["playback_path"])
    for row in conn.execute(
        "SELECT mp3_path FROM chapters WHERE mp3_path IS NOT NULL AND mp3_path != ''"
    ).fetchall():
        _add(row["mp3_path"])
    return keys


def _unclaimed_finished_audio_dirs(
    output_root: Path, referenced_keys: set[str]
) -> list[str]:
    """List real output subdirectories that hold finished audio yet no book claims.

    Such a directory may be a moved/renamed book's audio that lost its DB linkage;
    its existence means we are *not* confident any fileless book row is truly
    orphaned, so the caller keeps those rows instead of deleting them.
    """
    if not output_root.exists():
        return []
    output_root_resolved = output_root.resolve()
    unclaimed: list[str] = []
    for entry in output_root.iterdir():
        if entry.name in RESERVED_OUTPUT_DIR_NAMES:
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        key = str(output_root_resolved / entry.name)
        if key in referenced_keys:
            continue
        if _dir_contains_finished_audio(entry):
            unclaimed.append(str(entry.resolve()))
    return sorted(unclaimed)


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
    allow_title_fallback: bool = True,
) -> list[Path]:
    """Best-effort resolution of a book's *own* on-disk output directory.

    Uses only directories derived from the book's own resolvable playback/chapter
    paths (their parent, minus a trailing ``chapters`` segment). Only directories
    inside the output root are returned, so a stale or traversing stored path can
    never redirect the scan outside managed storage.

    The title-derived directory name is a *guess* (``sanitize_book_title(title)``)
    that ignores the pipeline's dedup suffixes (``Title (2)``) and library renames,
    so it can resolve into a directory another book owns. It is therefore only
    appended when ``allow_title_fallback`` is true — which the caller restricts to
    books that carry no stored playback/chapter pointer at all (the genuine
    post-migration case), never to a book whose stored pointer merely failed to
    resolve. This is what prevents reconciling a moved/renamed book onto a
    different book's audio.
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

    if allow_title_fallback:
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
    allow_title_fallback: bool = True,
) -> Optional[tuple[Path, str]]:
    """Locate a durable on-disk playback artifact for a book whose DB playback
    metadata is missing/stale.

    Returns ``(artifact_path, playback_status)`` — a top-level ``.m4b``/``.m4a``
    maps to ``packaged`` playback, per-chapter files under ``chapters/`` map to
    ``chapter_audio`` — or ``None`` when the book cannot be safely located on disk.

    Only the book's own directories are searched (see
    :func:`_candidate_book_output_dirs`); ``allow_title_fallback`` widens the
    search to the title-derived directory guess and must stay off for books that
    carry a stored pointer, so a rename/move never cross-wires the book onto
    another book's audio.
    """
    for directory in _candidate_book_output_dirs(
        title=title,
        playback_raw=playback_raw,
        chapter_raw_paths=chapter_raw_paths,
        workspace_root=workspace_root,
        output_root=output_root,
        temp_root=temp_root,
        allow_title_fallback=allow_title_fallback,
    ):
        packaged = _durable_audio_file_in_dir(directory)
        if packaged is not None:
            return packaged, PlaybackStatus.PACKAGED.value
        per_chapter = _durable_audio_file_in_dir(directory / "chapters")
        if per_chapter is not None:
            return per_chapter, PlaybackStatus.CHAPTER_AUDIO.value
    return None
