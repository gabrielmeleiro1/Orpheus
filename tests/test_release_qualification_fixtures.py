"""Tests for isolated qualification runtime fixtures and scenario copies."""

from __future__ import annotations

import hashlib
import stat
import time
import wave
from pathlib import Path

import pytest

from modules.storage.models import BookFilters
from modules.storage.sqlite_repo import SQLiteRepository
from tools.release_qualification.evidence import DEFAULT_EVIDENCE_ROOT, EvidenceRoot, SENTINEL_NAME
from tools.release_qualification.fixtures import (
    SCENARIO_NAMES,
    FixtureRoot,
    RuntimePaths,
    cleanup_fixture_paths,
    create_scenario_copy,
    generate_large_library,
    generate_playable_audio_fixture,
    runtime_env,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def evidence_root() -> EvidenceRoot:
    root = EvidenceRoot.create(
        release_id="1.0.0-fixtures",
        base=REPO_ROOT / DEFAULT_EVIDENCE_ROOT,
    )
    yield root
    if root.path.exists():
        from tools.release_qualification.fixtures import _make_tree_writable

        _make_tree_writable(root.path)
        sentinel = root.path / SENTINEL_NAME
        if not sentinel.exists():
            sentinel.write_text("1.0.0-fixtures\n", encoding="utf-8")
        root.cleanup()


def test_fixture_runtime_paths_live_below_sentinelled_qualification_root(
    evidence_root: EvidenceRoot,
) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    runtime = fixtures.create_runtime("clean")

    assert runtime.root.resolve().is_relative_to(evidence_root.path.resolve())
    assert (runtime.root / SENTINEL_NAME).is_file()
    assert "1.0.0-fixtures" in (runtime.root / SENTINEL_NAME).read_text(encoding="utf-8")
    for path in (
        runtime.data_dir,
        runtime.output_dir,
        runtime.temp_dir,
        runtime.db_path.parent,
    ):
        assert path.resolve().is_relative_to(evidence_root.path.resolve())


def test_runtime_env_uses_existing_audiobook_path_contract(evidence_root: EvidenceRoot) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    runtime = fixtures.create_runtime("clean")
    env = runtime_env(runtime)

    assert env == {
        "AUDIOBOOK_DATA_DIR": str(runtime.data_dir),
        "AUDIOBOOK_OUTPUT_DIR": str(runtime.output_dir),
        "AUDIOBOOK_TEMP_DIR": str(runtime.temp_dir),
        "AUDIOBOOK_DB_PATH": str(runtime.db_path),
    }
    assert set(env) == {
        "AUDIOBOOK_DATA_DIR",
        "AUDIOBOOK_OUTPUT_DIR",
        "AUDIOBOOK_TEMP_DIR",
        "AUDIOBOOK_DB_PATH",
    }


def test_large_library_generates_10000_valid_records_via_storage_api(
    evidence_root: EvidenceRoot,
) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    runtime = fixtures.create_runtime("large-library")

    started = time.perf_counter()
    count = generate_large_library(
        runtime,
        seed=42,
        count=10_000,
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
    )
    elapsed = time.perf_counter() - started
    assert count == 10_000
    # Practical upper bound for bounded public bulk inserts on CI hardware.
    assert elapsed < 45.0, f"10k fixture generation took {elapsed:.1f}s"

    repo = SQLiteRepository(
        runtime.db_path,
        output_dir=runtime.output_dir,
        temp_dir=runtime.temp_dir,
    )
    try:
        assert repo.count_books() == 10_000
        books = repo.list_books(BookFilters(limit=10_000, offset=0))
        assert len(books) == 10_000
        for book in books[:20]:
            assert book.title
            assert book.source_path
            assert book.total_chapters >= 0
            assert book.tags is not None
    finally:
        repo.close()


def test_fixture_generation_is_deterministic_from_seed(evidence_root: EvidenceRoot) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    first = fixtures.create_runtime("large-a")
    second = fixtures.create_runtime("large-b")

    generate_large_library(
        first,
        seed=7,
        count=50,
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
    )
    generate_large_library(
        second,
        seed=7,
        count=50,
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
    )

    filters = BookFilters(limit=100, offset=0)
    repo_a = SQLiteRepository(first.db_path, output_dir=first.output_dir, temp_dir=first.temp_dir)
    repo_b = SQLiteRepository(second.db_path, output_dir=second.output_dir, temp_dir=second.temp_dir)
    try:
        books_a = sorted(repo_a.list_books(filters), key=lambda item: item.id)
        books_b = sorted(repo_b.list_books(filters), key=lambda item: item.id)
        titles_a = [book.title for book in books_a]
        titles_b = [book.title for book in books_b]
        authors_a = [book.author for book in books_a]
        authors_b = [book.author for book in books_b]
        tags_a = [book.tags for book in books_a]
        tags_b = [book.tags for book in books_b]
    finally:
        repo_a.close()
        repo_b.close()

    assert titles_a == titles_b
    assert authors_a == authors_b
    assert tags_a == tags_b


def test_playable_fixture_uses_only_project_generated_audio(
    evidence_root: EvidenceRoot,
) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    runtime = fixtures.create_runtime("playable")

    audio_path = generate_playable_audio_fixture(
        runtime,
        seed=11,
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
    )
    assert audio_path.is_file()
    assert audio_path.resolve().is_relative_to(runtime.root.resolve())
    assert audio_path.suffix.lower() == ".wav"
    assert audio_path.stat().st_size > 44

    with wave.open(str(audio_path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 24_000
        assert handle.getnframes() > 0

    # Must not reference external downloads or live library media.
    assert "Library/Application Support" not in str(audio_path)
    assert "/Downloads/" not in str(audio_path)


def test_corruption_scenarios_operate_on_copies_with_hash_backup(
    evidence_root: EvidenceRoot,
) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    pristine = fixtures.create_runtime("pristine")
    generate_large_library(
        pristine,
        seed=3,
        count=25,
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
    )
    pristine_hash = hashlib.sha256(pristine.db_path.read_bytes()).hexdigest()
    pristine_bytes = pristine.db_path.read_bytes()

    corrupt = create_scenario_copy(
        pristine,
        fixtures.scenarios_root,
        "corrupt-db",
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
        backups_root=fixtures.backups_root,
    )
    assert corrupt.root.resolve() != pristine.root.resolve()
    assert corrupt.db_path.resolve() != pristine.db_path.resolve()

    backup = fixtures.pristine_backup_path(pristine_hash)
    assert backup.is_file()
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == pristine_hash
    assert backup.read_bytes() == pristine_bytes

    # Corruption mutates only the copy.
    corrupt.db_path.write_bytes(b"not-a-sqlite-database")
    assert pristine.db_path.read_bytes() == pristine_bytes
    assert backup.read_bytes() == pristine_bytes


def test_all_required_scenario_roots_are_independent(evidence_root: EvidenceRoot) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    pristine = fixtures.create_runtime("pristine")
    generate_large_library(
        pristine,
        seed=5,
        count=10,
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
    )
    generate_playable_audio_fixture(
        pristine,
        seed=5,
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
    )

    scenarios: dict[str, RuntimePaths] = {}
    for name in SCENARIO_NAMES:
        scenarios[name] = create_scenario_copy(
            pristine,
            fixtures.scenarios_root,
            name,
            expected_qualification_root=evidence_root.path,
            release_id=evidence_root.release_id,
            backups_root=fixtures.backups_root,
        )

    assert set(scenarios) == set(SCENARIO_NAMES)
    roots = [runtime.root.resolve() for runtime in scenarios.values()]
    assert len(set(roots)) == len(roots)

    missing = scenarios["missing-output"]
    assert missing.output_dir.exists()
    assert not any(missing.output_dir.iterdir())

    read_only = scenarios["read-only-copy"]
    mode = read_only.root.stat().st_mode
    assert not (mode & stat.S_IWUSR)
    # Parent evidence directory must remain writable.
    assert evidence_root.path.stat().st_mode & stat.S_IWUSR


def _forged_live_runtime(root: Path, release_id: str) -> RuntimePaths:
    root.mkdir(parents=True, exist_ok=True)
    (root / SENTINEL_NAME).write_text(f"{release_id}\n", encoding="utf-8")
    data_dir = root / "data"
    output_dir = root / "output"
    temp_dir = root / "temp"
    data_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    temp_dir.mkdir(exist_ok=True)
    return RuntimePaths(
        root=root,
        data_dir=data_dir,
        output_dir=output_dir,
        temp_dir=temp_dir,
        db_path=data_dir / "audiobooks.db",
    )


def test_mutating_ops_refuse_forged_sentinel_on_live_application_support(
    evidence_root: EvidenceRoot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    live_root = fake_home / "Library" / "Application Support" / "Orpheus"
    forged_app = _forged_live_runtime(live_root, evidence_root.release_id)
    with pytest.raises(ValueError, match="Application Support|refuse|live"):
        generate_large_library(
            forged_app,
            seed=1,
            count=2,
            expected_qualification_root=evidence_root.path,
            release_id=evidence_root.release_id,
        )

    forged_repo = _forged_live_runtime(
        REPO_ROOT / "data" / "forged-qual-runtime",
        evidence_root.release_id,
    )
    try:
        with pytest.raises(ValueError, match="refuse|live|repository"):
            generate_large_library(
                forged_repo,
                seed=1,
                count=2,
                expected_qualification_root=evidence_root.path,
                release_id=evidence_root.release_id,
            )
        with pytest.raises(ValueError, match="refuse|live|repository"):
            generate_playable_audio_fixture(
                forged_repo,
                seed=1,
                expected_qualification_root=evidence_root.path,
                release_id=evidence_root.release_id,
            )
        scenarios = evidence_root.path / "fixtures" / "scenarios"
        scenarios.mkdir(parents=True, exist_ok=True)
        (scenarios / SENTINEL_NAME).write_text(f"{evidence_root.release_id}\n", encoding="utf-8")
        backups = evidence_root.path / "fixtures" / "pristine-backups"
        backups.mkdir(parents=True, exist_ok=True)
        (backups / SENTINEL_NAME).write_text(f"{evidence_root.release_id}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="refuse|live|repository"):
            create_scenario_copy(
                forged_repo,
                scenarios,
                "clean",
                expected_qualification_root=evidence_root.path,
                release_id=evidence_root.release_id,
                backups_root=backups,
            )
    finally:
        if forged_repo.root.exists():
            import shutil

            shutil.rmtree(forged_repo.root, ignore_errors=True)


def test_mutating_ops_refuse_mismatched_sentinel_content(
    evidence_root: EvidenceRoot,
) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    runtime = fixtures.create_runtime("mismatch")
    (runtime.root / SENTINEL_NAME).write_text("forged-other-release\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sentinel"):
        generate_large_library(
            runtime,
            seed=1,
            count=2,
            expected_qualification_root=evidence_root.path,
            release_id=evidence_root.release_id,
        )


def test_scenario_copy_preserves_external_symlinks_and_refuses_unsafe_children(
    evidence_root: EvidenceRoot,
    tmp_path: Path,
) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    pristine = fixtures.create_runtime("symlink-src")
    outside = tmp_path / "outside-target"
    outside.write_text("secret", encoding="utf-8")
    link = pristine.data_dir / "external-link"
    link.symlink_to(outside)

    copied = create_scenario_copy(
        pristine,
        fixtures.scenarios_root,
        "clean",
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
        backups_root=fixtures.backups_root,
    )
    copied_link = copied.data_dir / "external-link"
    assert copied_link.is_symlink()
    assert copied_link.readlink() == outside
    assert outside.read_text(encoding="utf-8") == "secret"

    # Unsafe: directory symlink escaping the source tree must be refused.
    escape_dir = pristine.data_dir / "escape-dir"
    escape_dir.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink|unsafe|refuse"):
        create_scenario_copy(
            pristine,
            fixtures.scenarios_root,
            "large-library",
            expected_qualification_root=evidence_root.path,
            release_id=evidence_root.release_id,
            backups_root=fixtures.backups_root,
        )


def test_cleanup_refuses_live_application_support_and_repository_data(
    evidence_root: EvidenceRoot,
    tmp_path: Path,
) -> None:
    fixtures = FixtureRoot.create(evidence_root)
    runtime = fixtures.create_runtime("clean")

    live_app_support = Path.home() / "Library" / "Application Support" / "Orpheus"
    with pytest.raises(ValueError, match="Application Support|refuse|live"):
        cleanup_fixture_paths(
            RuntimePaths(
                root=live_app_support,
                data_dir=live_app_support / "data",
                output_dir=live_app_support / "output",
                temp_dir=live_app_support / "temp",
                db_path=live_app_support / "data" / "audiobooks.db",
            ),
            expected_qualification_root=evidence_root.path,
            release_id=evidence_root.release_id,
        )

    repo_data = REPO_ROOT / "data"
    with pytest.raises(ValueError, match="repository|refuse|live|data"):
        cleanup_fixture_paths(
            RuntimePaths(
                root=repo_data,
                data_dir=repo_data,
                output_dir=REPO_ROOT / "output",
                temp_dir=REPO_ROOT / "temp",
                db_path=repo_data / "audiobooks.db",
            ),
            expected_qualification_root=evidence_root.path,
            release_id=evidence_root.release_id,
        )

    # Outside the sentinelled qualification tree is refused even if temporary.
    foreign = tmp_path / "foreign-runtime"
    foreign.mkdir()
    (foreign / SENTINEL_NAME).write_text(f"{evidence_root.release_id}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="refuse|outside|qualification|sentinel"):
        cleanup_fixture_paths(
            RuntimePaths(
                root=foreign,
                data_dir=foreign / "data",
                output_dir=foreign / "output",
                temp_dir=foreign / "temp",
                db_path=foreign / "data" / "library.db",
            ),
            expected_qualification_root=evidence_root.path,
            release_id=evidence_root.release_id,
        )

    cleanup_fixture_paths(
        runtime,
        expected_qualification_root=evidence_root.path,
        release_id=evidence_root.release_id,
    )
    assert not runtime.root.exists()
