from collections import Counter
from contextlib import closing
from pathlib import Path
import os
import threading
import time

import pytest

from modules.storage.deletion import (
    LOCAL_DELETE_STAGING,
    delete_staging_policy,
    mark_delete_transaction_committed,
    write_delete_transaction,
)
from modules.storage.maintenance import (
    backup_database,
    prune_stale_records_and_files,
    restore_database_backup,
)
from modules.storage.models import BookCreate, SourceType
from tests.test_library_service import _create_deletion_book


def _stage(service, book, directory, source):
    entry = service._delete_staging_root() / "pending-delete"
    entry.mkdir(parents=True)
    artifacts = [
        (path, path.parent / LOCAL_DELETE_STAGING / entry.name / path.name)
        for path in (directory, source)
    ]
    write_delete_transaction(entry, book.id, artifacts)
    for original, staged in artifacts:
        service._stage_artifact_for_deletion(original, staged)
    return entry, artifacts


@pytest.mark.parametrize("restore_blocked", [False, True])
def test_prune_keeps_staged_files_referenced_by_another_restored_book(tmp_path, monkeypatch, restore_blocked):
    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    with closing(repo):
        current = backup_database(repo.db_path)
        external = tmp_path / "external.txt"
        external.write_bytes(b"external source")
        other = repo.create_book(BookCreate(
            title="Other snapshot", source_path=str(external), source_type=SourceType.TXT,
            playback_path=str(audio),
        ))
        repo.delete_book(book.id)
        historic = backup_database(repo.db_path)
        restore_database_backup(repo, current.name)
        entry, artifacts = _stage(service, book, directory, source)
        repo.delete_book(book.id)
        mark_delete_transaction_committed(entry)
        restore_database_backup(repo, historic.name)
        with monkeypatch.context() as patch:
            patch.setattr(service, "_restore_staged_artifacts", lambda artifacts: None)
            service.sweep_delete_staging()
        assert repo.get_book(book.id) is None
        assert repo.get_book(other.id).playback_path == str(audio)
        assert (entry / ".preserve").exists()
        assert artifacts[0][1].exists()
        assert not artifacts[1][1].exists(), "Committed unreferenced source must be removed"

        replace = os.replace

        def replace_unless_restoring(src, dst):
            if restore_blocked and LOCAL_DELETE_STAGING in Path(src).parts:
                raise PermissionError("Restore destination is locked")
            return replace(src, dst)

        monkeypatch.setattr(os, "replace", replace_unless_restoring)
        result = prune_stale_records_and_files(
            repo.db_path, tmp_path / "output", tmp_path / "temp", aggressive=True, grace_seconds=0,
        )
        assert audio.exists() or artifacts[0][1].exists(), "Referenced staged audio must survive cleanup"
        assert repo.get_book(other.id) is not None
        assert (artifacts[0][1] / "chapters" / "01.mp3" if restore_blocked else audio).read_bytes() == b"chapter audio"
        assert not artifacts[1][1].exists()
        assert entry.exists() == restore_blocked
        assert result["actions"]["deleted_delete_staging_dirs"] == ([] if restore_blocked else [str(entry)])


def _reconcile(service, repo, tmp_path, reconcile):
    if reconcile == "sweep":
        return service.sweep_delete_staging()
    return prune_stale_records_and_files(
        repo.db_path, tmp_path / "output", tmp_path / "temp", aggressive=True, grace_seconds=0,
    )


@pytest.mark.parametrize("reconcile", ["sweep", "prune"])
def test_case_alias_reference_restores_staged_audio(tmp_path, reconcile):
    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    with closing(repo):
        alias_directory = directory.with_name(directory.name.swapcase())
        if alias_directory == directory or not alias_directory.exists():
            pytest.skip("Requires a case-insensitive filesystem")
        alias_audio = alias_directory / audio.relative_to(directory)
        assert alias_audio.read_bytes() == b"chapter audio"
        external = tmp_path / "external.txt"
        external.write_bytes(b"external source")
        other = repo.create_book(BookCreate(
            title="Shared audio", source_path=str(external), source_type=SourceType.TXT,
            playback_path=str(alias_audio),
        ))
        entry, _ = _stage(service, book, directory, source)
        repo.delete_book(book.id)
        mark_delete_transaction_committed(entry)
        _reconcile(service, repo, tmp_path, reconcile)
        assert alias_audio.exists(), "Live case-variant reference must keep its audio"
        assert alias_audio.read_bytes() == b"chapter audio"
        assert repo.get_book(other.id) is not None
        assert not entry.exists()


@pytest.mark.parametrize("reconcile", ["sweep", "prune"])
def test_case_variant_source_reference_restores_bytes_when_parent_variant_is_missing(tmp_path, monkeypatch, reconcile):
    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    with closing(repo):
        alias_source = source.with_name(source.name.swapcase())
        other = repo.create_book(BookCreate(
            title="Shared source", source_path=str(alias_source), source_type=SourceType.TXT,
            playback_path=str(audio),
        ))
        entry, artifacts = _stage(service, book, directory, source)
        repo.delete_book(book.id)
        mark_delete_transaction_committed(entry)
        samefile = os.path.samefile
        root, variant = source.parent, source.parent.with_name(source.parent.name.swapcase())

        def samefile_with_sensitive_parent(first, second):
            if {Path(first), Path(second)} == {root, variant}:
                raise FileNotFoundError("Parent has no case-variant directory entry")
            return samefile(first, second)

        monkeypatch.setattr(os.path, "samefile", samefile_with_sensitive_parent)
        if reconcile == "prune":
            # Reconcile the old inventory while retaining freshly restored sources.
            stale = time.time() - 3 * 86400
            for path in [*entry.iterdir(), entry]:
                os.utime(path, (stale, stale))
            prune_stale_records_and_files(
                repo.db_path, tmp_path / "output", tmp_path / "temp", aggressive=True, grace_seconds=86400,
            )
        else:
            _reconcile(service, repo, tmp_path, reconcile)
        assert source.exists(), "Live case-variant reference must restore its only source"
        assert source.read_bytes() == b"source bytes"
        assert audio.read_bytes() == b"chapter audio"
        assert repo.get_book(other.id).source_path == str(alias_source)
        assert all(not staged.exists() for _, staged in artifacts)
        assert not entry.exists()


@pytest.mark.parametrize("reconcile", ["sweep", "prune"])
def test_mixed_staged_source_and_original_audio_both_remain_usable(tmp_path, reconcile):
    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    with closing(repo):
        entry, artifacts = _stage(service, book, directory, source)
        unused = tmp_path / "output" / "unused.mp3"
        unused.write_bytes(b"unused audio")
        staged_unused = unused.parent / LOCAL_DELETE_STAGING / entry.name / unused.name
        artifacts.append((unused, staged_unused))
        write_delete_transaction(entry, book.id, artifacts)
        service._stage_artifact_for_deletion(unused, staged_unused)
        other = repo.create_book(BookCreate(
            title="Mixed paths", source_path=str(artifacts[1][1]), source_type=SourceType.TXT,
            playback_path=str(audio),
        ))
        repo.delete_book(book.id)
        mark_delete_transaction_committed(entry)
        for _ in range(3):
            _reconcile(service, repo, tmp_path, reconcile)
            assert audio.exists(), "Original audio reference must be restored alongside a staged source reference"
            assert audio.read_bytes() == b"chapter audio"
            assert artifacts[1][1].read_bytes() == b"source bytes"
            assert repo.get_book(other.id).playback_path == str(audio)
            assert not artifacts[0][1].exists()
            assert not staged_unused.exists(), "Committed unreferenced artifact must be removed"
            assert not unused.exists()
            assert entry.exists(), "Inventory must remain while a staged source is referenced"
            assert (entry / "transaction.json").is_file()


@pytest.mark.parametrize("reconcile", ["sweep", "prune"])
@pytest.mark.parametrize("reference_directory", [False, True])
def test_staged_path_reference_keeps_live_bytes(tmp_path, reconcile, reference_directory):
    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    with closing(repo):
        entry, artifacts = _stage(service, book, directory, source)
        staged_audio = artifacts[0][1] / audio.relative_to(directory)
        other = repo.create_book(BookCreate(
            title="Staged audio", source_path=str(artifacts[1][1]), source_type=SourceType.TXT,
            playback_path=str(artifacts[0][1] if reference_directory else staged_audio),
        ))
        repo.delete_book(book.id)
        mark_delete_transaction_committed(entry)
        _reconcile(service, repo, tmp_path, reconcile)
        assert staged_audio.exists(), "Live staged reference must keep its audio in place"
        assert staged_audio.read_bytes() == b"chapter audio"
        assert artifacts[1][1].read_bytes() == b"source bytes"
        assert repo.get_book(other.id) is not None
        assert entry.exists()


@pytest.mark.parametrize("reconcile", ["sweep", "prune"])
def test_partial_restore_removes_committed_unreferenced_source(tmp_path, reconcile):
    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    with closing(repo):
        external = tmp_path / "external.txt"
        external.write_bytes(b"external source")
        other = repo.create_book(BookCreate(
            title="Shared audio", source_path=str(external), source_type=SourceType.TXT,
            playback_path=str(audio),
        ))
        entry, artifacts = _stage(service, book, directory, source)
        repo.delete_book(book.id)
        mark_delete_transaction_committed(entry)
        _reconcile(service, repo, tmp_path, reconcile)
        assert audio.read_bytes() == b"chapter audio"
        assert repo.get_book(other.id) is not None
        assert not artifacts[1][1].exists(), "Committed unreferenced source must be removed"
        assert not entry.exists(), "Completed inventory must be removed"
        _reconcile(service, repo, tmp_path, reconcile)
        assert audio.read_bytes() == b"chapter audio"


@pytest.mark.parametrize("reconcile", ["sweep", "prune"])
def test_partial_restore_keeps_uncommitted_unreferenced_source(tmp_path, reconcile):
    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    with closing(repo):
        external = tmp_path / "external.txt"
        external.write_bytes(b"external source")
        repo.create_book(BookCreate(
            title="Shared audio", source_path=str(external), source_type=SourceType.TXT,
            playback_path=str(audio),
        ))
        entry, artifacts = _stage(service, book, directory, source)
        repo.delete_book(book.id)
        _reconcile(service, repo, tmp_path, reconcile)
        retained_audio = audio if reconcile == "sweep" else artifacts[0][1] / audio.relative_to(directory)
        assert retained_audio.read_bytes() == b"chapter audio"
        assert artifacts[1][1].read_bytes() == b"source bytes"
        assert entry.exists()


@pytest.mark.parametrize("sensitivity", ["sensitive", "insensitive", "unknown"])
def test_case_variant_reference_restores_artifacts_regardless_of_parent_semantics(tmp_path, monkeypatch, sensitivity):
    root = tmp_path / "output"
    root.mkdir()
    entry = tmp_path / "transaction"
    entry.mkdir()
    mark_delete_transaction_committed(entry)
    artifacts = [(root / "Book", root / ".staging" / "Book")]
    references = {root / "bOOK" / "01.mp3", root / "bOOK" / "02.mp3"}

    def samefile(first, second):
        if sensitivity == "unknown":
            raise PermissionError("Cannot inspect containing root")
        return sensitivity == "insensitive"

    monkeypatch.setattr(os.path, "samefile", samefile)
    decision, referenced, protected = delete_staging_policy(entry, artifacts, references)
    assert decision == "restore"
    assert referenced == artifacts
    assert protected == []


def test_uncommitted_staged_reference_keeps_whole_transaction(tmp_path):
    entry = tmp_path / "transaction"
    entry.mkdir()
    artifacts = [
        (tmp_path / "Book", tmp_path / "staging" / "Book"),
        (tmp_path / "source.txt", tmp_path / "staging" / "source.txt"),
    ]
    references = {artifacts[0][0] / "01.mp3", artifacts[1][1]}
    assert delete_staging_policy(entry, artifacts, references) == ("preserve", [], [])


def test_staged_reference_takes_priority_over_original_reference(tmp_path):
    entry = tmp_path / "transaction"
    entry.mkdir()
    mark_delete_transaction_committed(entry)
    artifacts = [(tmp_path / "source.txt", tmp_path / "staging" / "source.txt")]
    references = {artifacts[0][0], artifacts[0][1].with_name("SOURCE.TXT")}
    assert delete_staging_policy(entry, artifacts, references) == ("restore", [], artifacts)


def test_case_variant_sibling_path_does_not_reference_artifact(tmp_path):
    entry = tmp_path / "transaction"
    entry.mkdir()
    mark_delete_transaction_committed(entry)
    artifacts = [(tmp_path / "Book", tmp_path / "staging" / "Book")]
    references = {tmp_path / "bOOK-other" / "01.mp3"}
    assert delete_staging_policy(entry, artifacts, references) == ("remove", [], [])


def test_policy_resolves_each_live_reference_once(tmp_path, monkeypatch):
    entry = tmp_path / "transaction"
    entry.mkdir()
    mark_delete_transaction_committed(entry)
    artifacts = [(tmp_path / f"Book{index}", tmp_path / "staging" / f"Book{index}") for index in range(3)]
    references = {tmp_path / "Other" / f"{number:02}.mp3" for number in range(20)}
    calls = Counter()
    resolve = Path.resolve

    def counting_resolve(self, *args, **kwargs):
        calls[self] += 1
        return resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    assert delete_staging_policy(entry, artifacts, references) == ("remove", [], [])
    assert max(calls[path] for path in references) == 1


class _ObservedLock:
    def __init__(self, lock, attempted):
        self.lock = lock
        self.attempted = attempted

    def __enter__(self):
        if threading.current_thread().name == "restore-library":
            self.attempted.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


def test_prune_restores_shared_staging_before_pruning_fileless_rows(tmp_path):
    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    with closing(repo):
        other = repo.create_book(BookCreate(
            title="Shared audio", source_path=str(source), source_type=SourceType.TXT,
            playback_path=str(audio),
        ))
        entry, _ = _stage(service, book, directory, source)
        repo.delete_book(book.id)
        mark_delete_transaction_committed(entry)
        prune_stale_records_and_files(
            repo.db_path, tmp_path / "output", tmp_path / "temp", aggressive=True, grace_seconds=0,
        )
        assert repo.get_book(other.id) is not None, "A book with recoverable staged files must survive cleanup"
        assert audio.read_bytes() == b"chapter audio"
        assert source.read_bytes() == b"source bytes"
        assert not entry.exists()


def test_sweep_holds_snapshot_until_staged_artifacts_are_reconciled(tmp_path, monkeypatch):
    repo, service, book, directory, _, source = _create_deletion_book(tmp_path)
    with closing(repo):
        backup = backup_database(repo.db_path)
        entry, artifacts = _stage(service, book, directory, source)
        repo.delete_book(book.id)
        mark_delete_transaction_committed(entry)
        attempted = threading.Event()
        restored = threading.Event()
        errors = []

        def restore():
            try:
                restore_database_backup(repo, backup.name)
            except Exception as error:
                errors.append(error)
            finally:
                restored.set()

        thread = threading.Thread(target=restore, name="restore-library", daemon=True)
        monkeypatch.setattr(repo, "_lock", _ObservedLock(repo._lock, attempted))
        collect = service._other_book_artifact_paths

        def collect_then_restore(book_id):
            paths = collect(book_id)
            thread.start()
            assert attempted.wait(5), "Restore must reach the replacement boundary"
            assert not restored.wait(1), "Restore must wait until staged reconciliation finishes"
            return paths

        remove = service._commit_staged_artifacts

        def remove_before_restore(staged):
            assert not restored.is_set(), "Restore must remain blocked through artifact removal"
            remove(staged)

        monkeypatch.setattr(service, "_other_book_artifact_paths", collect_then_restore)
        monkeypatch.setattr(service, "_commit_staged_artifacts", remove_before_restore)
        try:
            service.sweep_delete_staging()
        finally:
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors == []
        assert repo.get_book(book.id) is not None
        assert not entry.exists()
        assert all(not staged.exists() for _, staged in artifacts)


@pytest.mark.parametrize("contents", ["preserved", "unknown"])
def test_prune_retains_ambiguous_legacy_staging(tmp_path, contents):
    repo, _, _, _, _, _ = _create_deletion_book(tmp_path)
    with closing(repo):
        entry = tmp_path / "temp" / "delete_staging" / "legacy"
        entry.mkdir(parents=True)
        (entry / ".committed").touch()
        payload = entry / ("chapter.mp3" if contents == "preserved" else "source.txt")
        payload.write_bytes(b"retained bytes")
        if contents == "preserved":
            (entry / ".preserve").touch()
        stale = time.time() - 3 * 86400
        for path in [*entry.iterdir(), entry]:
            os.utime(path, (stale, stale))
        prune_stale_records_and_files(
            repo.db_path, tmp_path / "output", tmp_path / "temp", aggressive=True, grace_seconds=0,
        )
        assert payload.exists(), "Ambiguous legacy staging must survive cleanup"
        assert payload.read_bytes() == b"retained bytes"
