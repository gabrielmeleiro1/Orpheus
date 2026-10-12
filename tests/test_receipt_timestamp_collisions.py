"""Metadata caches must not hide writes within a Windows filesystem tick."""

import os

import pytest

from modules.runtime_setup import receipts


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    weights = root / "model.bin"
    weights.write_bytes(b"original")
    receipts.write_download_receipt(root, model_id="test/model", revision="a" * 40)
    return root, weights


def _verify(verifier, root):
    return verifier.verify(root, model_id="test/model", revision="a" * 40)


def _colliding_windows_metadata(monkeypatch, *, age_ns):
    stamp = 1_000_000_000
    real_identity = receipts._stat_identity
    monkeypatch.setattr(receipts, "_WINDOWS", True)
    monkeypatch.setattr(receipts, "_stat_identity", lambda value: (*real_identity(value)[:4], stamp, stamp))
    monkeypatch.setattr(receipts, "_windows_change_time_ns", lambda _descriptor: stamp)
    monkeypatch.setattr(receipts, "_now_ns", lambda: stamp + age_ns)
    return stamp


@pytest.mark.parametrize("age_ns", [0, 1_000_000_000, -10_000_000_000])
def test_same_size_write_with_identical_recent_or_future_metadata_is_rehashed(snapshot, monkeypatch, age_ns):
    root, weights = snapshot
    _colliding_windows_metadata(monkeypatch, age_ns=age_ns)
    verifier = receipts.ModelReceiptVerifier()
    assert _verify(verifier, root)
    weights.write_bytes(b"tampered")
    assert not _verify(verifier, root)


def test_mature_windows_metadata_reuses_hashes(snapshot, monkeypatch):
    root, _weights = snapshot
    _colliding_windows_metadata(monkeypatch, age_ns=3_000_000_000)
    real_hash = receipts._file_size_and_sha256
    calls = []
    monkeypatch.setattr(receipts, "_file_size_and_sha256", lambda path: calls.append(path) or real_hash(path))
    verifier = receipts.ModelReceiptVerifier()
    assert _verify(verifier, root)
    assert _verify(verifier, root)
    assert len(calls) == 1


def test_snapshot_becoming_mature_during_hash_remains_valid_but_uncached(snapshot, monkeypatch):
    root, _weights = snapshot
    stamp = _colliding_windows_metadata(monkeypatch, age_ns=0)
    clock = iter((stamp, stamp + 3_000_000_000))
    monkeypatch.setattr(receipts, "_now_ns", lambda: next(clock))
    verifier = receipts.ModelReceiptVerifier()
    assert _verify(verifier, root)
    assert not verifier._entries


def test_windows_change_time_invalidates_cache_when_write_time_is_restored(snapshot, monkeypatch):
    root, weights = snapshot
    stamp = _colliding_windows_metadata(monkeypatch, age_ns=10_000_000_000)
    verifier = receipts.ModelReceiptVerifier()
    assert _verify(verifier, root)
    assert verifier._entries
    weights.write_bytes(b"tampered")
    # Size, inode, creation time and restored write time all remain identical;
    # the native mutation timestamp is the only changed fingerprint field.
    monkeypatch.setattr(receipts, "_windows_change_time_ns", lambda _descriptor: stamp + 1_000_000_000)
    assert not _verify(verifier, root)


def test_unavailable_windows_change_time_disables_only_metadata_cache(snapshot, monkeypatch):
    root, weights = snapshot
    _colliding_windows_metadata(monkeypatch, age_ns=10_000_000_000)
    monkeypatch.setattr(receipts, "_windows_change_time_ns", lambda _descriptor: None)
    verifier = receipts.ModelReceiptVerifier()
    assert _verify(verifier, root)
    assert not verifier._entries
    weights.write_bytes(b"tampered")
    assert not _verify(verifier, root)


def test_clock_rollback_rechecks_previously_cached_windows_files(snapshot, monkeypatch):
    root, weights = snapshot
    stamp = _colliding_windows_metadata(monkeypatch, age_ns=10_000_000_000)
    verifier = receipts.ModelReceiptVerifier()
    assert _verify(verifier, root)
    assert verifier._entries
    monkeypatch.setattr(receipts, "_now_ns", lambda: stamp - 1)
    weights.write_bytes(b"tampered")
    assert not _verify(verifier, root)


def test_fresh_windows_receipt_also_prevents_cache_admission(snapshot, monkeypatch):
    root, _weights = snapshot
    stamp = _colliding_windows_metadata(monkeypatch, age_ns=10_000_000_000)
    real_target = receipts._target_identity
    monkeypatch.setattr(receipts, "_target_identity", lambda path: (*real_target(path)[:6], stamp))
    monkeypatch.setattr(receipts, "_windows_change_time_ns", lambda _descriptor: stamp + 10_000_000_000)
    verifier = receipts.ModelReceiptVerifier()
    assert _verify(verifier, root)
    assert not verifier._entries


@pytest.mark.skipif(os.name != "nt", reason="Exercises native Windows mutation timestamps")
@pytest.mark.parametrize("restore_mtime", [False, True])
def test_native_windows_rapid_rewrites_are_never_accepted(tmp_path, restore_mtime):
    # Repetition exercises actual timestamp collisions without sleeping until
    # writes become distinguishable, which would hide the original bug.
    for index in range(20):
        root = tmp_path / str(index)
        root.mkdir()
        weights = root / "model.bin"
        weights.write_bytes(b"original")
        receipts.write_download_receipt(root, model_id="test/model", revision="a" * 40)
        with weights.open("rb") as handle:
            assert receipts._windows_change_time_ns(handle.fileno()) is not None
        verifier = receipts.ModelReceiptVerifier()
        assert _verify(verifier, root)
        previous = weights.stat()
        weights.write_bytes(b"tampered")
        if restore_mtime:
            os.utime(weights, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        assert not _verify(verifier, root), f"Accepted changed content on repetition {index}"
