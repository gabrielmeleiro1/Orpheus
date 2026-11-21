"""Isolated desktop runtime fixtures for release qualification."""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from modules.storage.db_ownership import DIRECTORY_GATE_NAME, acquire_database
from modules.storage.managed_roots import STORAGE_OWNER_NAME
from modules.storage.models import BookCreate, ConversionMode, SourceType
from modules.storage.sqlite_repo import SQLiteRepository

from .evidence import SENTINEL_NAME

SCENARIO_NAMES: tuple[str, ...] = (
    "clean",
    "large-library",
    "corrupt-db",
    "missing-output",
    "read-only-copy",
)

_BATCH_SIZE = 500
_SOURCE_TYPES = (SourceType.EPUB, SourceType.PDF, SourceType.TXT, SourceType.MD)
_TAG_POOL = (
    "fiction",
    "history",
    "science",
    "biography",
    "fantasy",
    "mystery",
    "poetry",
    "reference",
    "drama",
    "travel",
)
_AUTHOR_POOL = (
    "Ada Lovelace",
    "Grace Hopper",
    "Alan Turing",
    "Katherine Johnson",
    "Donald Knuth",
    "Barbara Liskov",
    "Edsger Dijkstra",
    "Margaret Hamilton",
)


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    data_dir: Path
    output_dir: Path
    temp_dir: Path
    db_path: Path


@dataclass
class FixtureRoot:
    evidence_path: Path
    release_id: str
    path: Path
    scenarios_root: Path
    backups_root: Path

    @classmethod
    def create(cls, evidence: object) -> FixtureRoot:
        evidence_path = Path(getattr(evidence, "path"))
        release_id = str(getattr(evidence, "release_id"))
        path = (evidence_path / "fixtures").resolve()
        if not path.is_relative_to(evidence_path.resolve()):
            raise ValueError("unsafe fixture path: outside qualification evidence root")
        path.mkdir(parents=True, exist_ok=True)
        _write_sentinel(path, release_id)
        scenarios_root = path / "scenarios"
        backups_root = path / "pristine-backups"
        scenarios_root.mkdir(parents=True, exist_ok=True)
        backups_root.mkdir(parents=True, exist_ok=True)
        _write_sentinel(scenarios_root, release_id)
        _write_sentinel(backups_root, release_id)
        return cls(
            evidence_path=evidence_path.resolve(),
            release_id=release_id,
            path=path,
            scenarios_root=scenarios_root,
            backups_root=backups_root,
        )

    def create_runtime(self, name: str) -> RuntimePaths:
        runtimes_root = self.path / "runtimes"
        runtimes_root.mkdir(parents=True, exist_ok=True)
        _write_sentinel(runtimes_root, self.release_id)
        runtime_root = (runtimes_root / name).resolve()
        if not runtime_root.is_relative_to(self.path.resolve()):
            raise ValueError("unsafe runtime path: outside fixture root")
        if runtime_root.exists():
            raise ValueError(f"runtime already exists: {name}")
        return _materialize_runtime(runtime_root, self.release_id)

    def discard(self, relative: str) -> None:
        """Remove a fixture subtree left by an earlier attempt so a phase can re-run."""
        _assert_sentinel_matches(self.path, self.release_id)
        target = self.path / relative
        if not target.exists() and not _is_link(target):
            return
        _assert_confined_path(target, self.path.resolve())
        _make_tree_writable(target)
        shutil.rmtree(target)


def generate_large_library(
    runtime: RuntimePaths,
    *,
    seed: int,
    count: int = 10_000,
    expected_qualification_root: Path,
    release_id: str,
) -> int:
    if count <= 0:
        raise ValueError("count must be positive")
    _assert_mutable_runtime(
        runtime,
        expected_qualification_root=expected_qualification_root,
        release_id=release_id,
    )

    repo = SQLiteRepository(
        runtime.db_path,
        output_dir=runtime.output_dir,
        temp_dir=runtime.temp_dir,
    )
    try:
        rng = random.Random(seed)
        books = [_deterministic_book(index, rng, runtime) for index in range(count)]
        inserted = repo.create_books(books, batch_size=_BATCH_SIZE)
        counted = repo.count_books()
    finally:
        repo.close()
    if inserted != count or counted != count:
        raise RuntimeError(f"expected {count} books, inserted {inserted}, found {counted}")
    return counted


def create_scenario_copy(
    source: RuntimePaths,
    scenarios_root: Path,
    scenario: str,
    *,
    expected_qualification_root: Path,
    release_id: str,
    backups_root: Path,
) -> RuntimePaths:
    if scenario not in SCENARIO_NAMES:
        raise ValueError(f"unknown scenario: {scenario}")
    _assert_mutable_runtime(
        source,
        expected_qualification_root=expected_qualification_root,
        release_id=release_id,
    )
    expected = expected_qualification_root.resolve()
    scenarios_root = scenarios_root.resolve()
    backups_root = backups_root.resolve()
    _reject_live_paths(scenarios_root)
    _reject_live_paths(backups_root)
    if not scenarios_root.is_relative_to(expected):
        raise ValueError("refuse: scenarios root outside qualification evidence root")
    if not backups_root.is_relative_to(expected):
        raise ValueError("refuse: backups root outside qualification evidence root")
    _assert_sentinel_matches(scenarios_root, release_id)
    _assert_sentinel_matches(backups_root, release_id)

    target_root = (scenarios_root / scenario).resolve()
    if not target_root.is_relative_to(scenarios_root):
        raise ValueError("unsafe scenario path")
    if target_root.exists():
        raise ValueError(f"scenario already exists: {scenario}")

    with acquire_database(source.db_path) as source_owner, source_owner.lock:
        _assert_safe_tree_for_copy(source.root)
        runtime = _runtime_from_root(target_root)
        with acquire_database(runtime.db_path) as target_owner, target_owner.lock:
            shutil.copytree(
                source.root, target_root, symlinks=True, dirs_exist_ok=True,
                ignore=lambda directory, names: [
                    name for name in names
                    if name in {STORAGE_OWNER_NAME, DIRECTORY_GATE_NAME}
                    or (Path(directory) == source.data_dir and name == source.db_path.name + ".lock")
                ],
            )
            _write_sentinel(target_root, release_id)

            if scenario == "corrupt-db":
                _preserve_pristine_backup(
                    source,
                    backups_root,
                    expected_qualification_root=expected,
                    release_id=release_id,
                )
                runtime.db_path.write_bytes(b"CORRUPTED-RELEASE-QUALIFICATION-DB\n")
            elif scenario == "missing-output":
                if runtime.output_dir.exists():
                    for child in list(runtime.output_dir.iterdir()):
                        if child.name == SENTINEL_NAME:
                            continue
                        if child.is_symlink():
                            child.unlink()
                        elif child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                runtime.output_dir.mkdir(parents=True, exist_ok=True)
            elif scenario == "read-only-copy":
                _make_tree_read_only(runtime.root)
            elif scenario in {"clean", "large-library"}:
                pass

            return runtime


def _reject_live_paths(path: Path) -> None:
    resolved = path.resolve()
    home = Path.home().resolve()
    app_support = (home / "Library" / "Application Support").resolve()
    if resolved == app_support or resolved.is_relative_to(app_support):
        raise ValueError("refuse: live Application Support path")

    repo_root = Path(__file__).resolve().parents[2]
    for live_name in ("data", "output", "temp"):
        live = (repo_root / live_name).resolve()
        if resolved == live or resolved.is_relative_to(live):
            raise ValueError(f"refuse: repository {live_name} path")


def _assert_sentinel_matches(path: Path, release_id: str) -> None:
    sentinel = path / SENTINEL_NAME
    if not sentinel.is_file() or sentinel.is_symlink():
        raise ValueError("refuse: sentinel missing or unsafe")
    if sentinel.read_text(encoding="utf-8").strip() != release_id:
        raise ValueError("refuse: sentinel mismatched")


def _assert_mutable_runtime(
    runtime: RuntimePaths,
    *,
    expected_qualification_root: Path,
    release_id: str,
) -> None:
    root = runtime.root.resolve()
    expected = expected_qualification_root.resolve()
    _reject_live_paths(root)
    if not root.is_relative_to(expected):
        raise ValueError("refuse: path outside qualification evidence root")
    if root == expected:
        raise ValueError("refuse: refusing qualification evidence root itself")
    if _is_link(runtime.root):
        raise ValueError("refuse: runtime root must not be a symlink")
    _assert_sentinel_matches(runtime.root, release_id)
    for child in (runtime.data_dir, runtime.output_dir, runtime.temp_dir, runtime.db_path):
        _assert_confined_path(child, root)
    for suffix in (".lock", "-wal", "-shm"):
        _assert_confined_path(Path(str(runtime.db_path) + suffix), root)
    _assert_confined_path(runtime.db_path.parent / DIRECTORY_GATE_NAME, root)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _assert_confined_path(path: Path, root: Path) -> None:
    """Reject linked managed paths before opening, chmodding or removing them."""
    resolved = path.resolve()
    _reject_live_paths(resolved)
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("refuse: runtime child escapes its fixture root")
    current = path
    while current.resolve() != root:
        if _is_link(current):
            raise ValueError("refuse: managed fixture paths must not contain symlinks or junctions")
        if current == current.parent:
            raise ValueError("refuse: runtime child escapes its fixture root")
        current = current.parent


def _assert_safe_tree_for_copy(root: Path) -> None:
    """Refuse directory links; file symlinks are copied as links, never followed."""
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in dirnames:
            child = base / name
            if _is_link(child):
                raise ValueError(f"refuse: unsafe directory symlink child {child}")


def _materialize_runtime(runtime_root: Path, release_id: str) -> RuntimePaths:
    runtime_root.mkdir(parents=False, exist_ok=False)
    _write_sentinel(runtime_root, release_id)
    data_dir = runtime_root / "data"
    output_dir = runtime_root / "output"
    temp_dir = runtime_root / "temp"
    data_dir.mkdir()
    output_dir.mkdir()
    temp_dir.mkdir()
    db_path = data_dir / "audiobooks.db"
    return RuntimePaths(
        root=runtime_root,
        data_dir=data_dir,
        output_dir=output_dir,
        temp_dir=temp_dir,
        db_path=db_path,
    )


def _runtime_from_root(runtime_root: Path) -> RuntimePaths:
    data_dir = runtime_root / "data"
    output_dir = runtime_root / "output"
    temp_dir = runtime_root / "temp"
    db_path = data_dir / "audiobooks.db"
    return RuntimePaths(
        root=runtime_root,
        data_dir=data_dir,
        output_dir=output_dir,
        temp_dir=temp_dir,
        db_path=db_path,
    )


def _write_sentinel(path: Path, release_id: str) -> None:
    (path / SENTINEL_NAME).write_text(f"{release_id}\n", encoding="utf-8")


def _deterministic_book(index: int, rng: random.Random, runtime: RuntimePaths) -> BookCreate:
    source_type = _SOURCE_TYPES[index % len(_SOURCE_TYPES)]
    author = _AUTHOR_POOL[index % len(_AUTHOR_POOL)]
    tag_a = _TAG_POOL[index % len(_TAG_POOL)]
    tag_b = _TAG_POOL[(index // 3) % len(_TAG_POOL)]
    noise = rng.randint(0, 10_000_000)
    title = f"Qualification Book {index:05d} [{noise}]"
    extension = source_type.value
    source_path = str(runtime.data_dir / "sources" / f"book-{index:05d}.{extension}")
    return BookCreate(
        title=title,
        author=author,
        source_path=source_path,
        source_type=source_type,
        conversion_mode=ConversionMode.AUDIOBOOK,
        total_chapters=(index % 24) + 1,
        tags=f"{tag_a},{tag_b}",
        favorite=(index % 17 == 0),
    )


def _preserve_pristine_backup(
    source: RuntimePaths,
    backups_root: Path,
    *,
    expected_qualification_root: Path,
    release_id: str,
) -> Path:
    expected = expected_qualification_root.resolve()
    backups_root = backups_root.resolve()
    _reject_live_paths(backups_root)
    if not backups_root.is_relative_to(expected):
        raise ValueError("refuse: backups root outside qualification evidence root")
    _assert_sentinel_matches(backups_root, release_id)
    backups_root.mkdir(parents=True, exist_ok=True)
    with acquire_database(source.db_path) as ownership, ownership.lock:
        # The shared repository lock excludes commits while WAL pages are copied.
        with closing(sqlite3.connect(source.db_path.resolve().as_uri() + "?mode=rw", uri=True)) as connection:
            busy, _, _ = connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            if busy:
                raise ValueError("cannot checkpoint pristine database while readers are active")
        digest = hashlib.sha256(source.db_path.read_bytes()).hexdigest()
        backup_path = backups_root / f"{digest}.db"
        if backup_path.is_symlink():
            raise ValueError("refuse: pristine backup path is a symlink")
        with acquire_database(backup_path) as backup_owner, backup_owner.lock:
            if not backup_path.exists():
                shutil.copy2(source.db_path, backup_path)
            return backup_path


def _iter_tree_entries(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        # Never descend through directory symlinks.
        children = list(dirnames)
        dirnames[:] = [name for name in dirnames if not _is_link(base / name)]
        for name in children:
            yield base / name
        for name in filenames:
            yield base / name


def _chmod_regular_path(path: Path, mode: int) -> None:
    if _is_link(path):
        return
    # Python 3.12 on Windows cannot pass follow_symlinks=False to chmod.
    # Links and junctions have already been excluded from this fixture walk.
    if os.chmod in os.supports_follow_symlinks:
        path.chmod(mode, follow_symlinks=False)
    else:
        path.chmod(mode)


def _make_tree_read_only(root: Path) -> None:
    entries = sorted(_iter_tree_entries(root), reverse=True)
    for path in entries:
        if _is_link(path):
            continue
        mode = path.lstat().st_mode
        _chmod_regular_path(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    if not _is_link(root):
        mode = root.lstat().st_mode
        _chmod_regular_path(root, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    if not _is_link(root):
        mode = root.lstat().st_mode
        _chmod_regular_path(root, mode | stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
    for path in _iter_tree_entries(root):
        if _is_link(path):
            continue
        mode = path.lstat().st_mode
        _chmod_regular_path(path, mode | stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
