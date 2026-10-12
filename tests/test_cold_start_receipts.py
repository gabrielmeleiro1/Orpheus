"""Persist only settled, unchanged content-integrity fingerprints."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from modules.runtime_setup import receipts


def snapshot(tmp_path: Path, monkeypatch):
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "weights.bin").write_bytes(b"original model")
    receipts.write_download_receipt(root, model_id="example/model", revision="a" * 40)
    # Unit tests can advance the cache cutoff without a wall-clock sleep.
    now = receipts._now_ns()
    monkeypatch.setattr(receipts, "_now_ns", lambda: now + 10_000_000_000)
    return root, dict(model_id="example/model", revision="a" * 40)


def test_restart_reuses_exact_settled_fingerprint(tmp_path, monkeypatch):
    root, identity = snapshot(tmp_path, monkeypatch)
    cache_path = tmp_path / "integrity.json"
    hashed = Mock(wraps=receipts._validate_parsed_receipt)
    monkeypatch.setattr(receipts, "_validate_parsed_receipt", hashed)
    assert receipts.ModelReceiptVerifier(cache_path=cache_path).verify(root, **identity)
    assert cache_path.is_file()
    assert receipts.ModelReceiptVerifier(cache_path=cache_path).verify(root, **identity)
    assert hashed.call_count == 1


@pytest.mark.parametrize("change", ["weights", "receipt", "extra", "missing", "replacement"])
def test_persisted_fingerprint_invalidates_on_file_changes(tmp_path, monkeypatch, change):
    root, identity = snapshot(tmp_path, monkeypatch)
    cache_path = tmp_path / "integrity.json"
    assert receipts.ModelReceiptVerifier(cache_path=cache_path).verify(root, **identity)
    target = root / "weights.bin"
    if change == "weights":
        before = target.stat()
        target.write_bytes(b"modified model")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    elif change == "receipt":
        (root / receipts.DOWNLOAD_RECEIPT_FILENAME).write_text("{}", encoding="utf-8")
    elif change == "extra":
        (root / "extra.bin").write_bytes(b"unexpected")
    elif change == "missing":
        target.unlink()
    else:
        replacement = tmp_path / "replacement.bin"
        replacement.write_bytes(b"modified model")
        replacement.replace(target)
    assert not receipts.ModelReceiptVerifier(cache_path=cache_path).verify(root, **identity)


def test_recent_or_unreliable_metadata_always_rehashes(tmp_path, monkeypatch):
    root, identity = snapshot(tmp_path, monkeypatch)
    cache_path = tmp_path / "integrity.json"
    assert receipts.ModelReceiptVerifier(cache_path=cache_path).verify(root, **identity)
    monkeypatch.setattr(receipts, "_stable_identity", lambda *_: False)
    hashed = Mock(wraps=receipts._validate_parsed_receipt)
    monkeypatch.setattr(receipts, "_validate_parsed_receipt", hashed)
    verifier = receipts.ModelReceiptVerifier(cache_path=cache_path)
    assert verifier.verify(root, **identity)
    assert verifier.verify(root, **identity)
    assert hashed.call_count == 2


def test_invalidation_clears_persisted_success(tmp_path, monkeypatch):
    root, identity = snapshot(tmp_path, monkeypatch)
    cache_path = tmp_path / "integrity.json"
    verifier = receipts.ModelReceiptVerifier(cache_path=cache_path)
    assert verifier.verify(root, **identity)
    verifier.invalidate()
    hashed = Mock(wraps=receipts._validate_parsed_receipt)
    monkeypatch.setattr(receipts, "_validate_parsed_receipt", hashed)
    assert receipts.ModelReceiptVerifier(cache_path=cache_path).verify(root, **identity)
    assert hashed.call_count == 1


def test_corrupt_or_unwritable_cache_falls_back_to_hashing(tmp_path, monkeypatch):
    root, identity = snapshot(tmp_path, monkeypatch)
    cache_path = tmp_path / "integrity.json"
    cache_path.write_text("broken JSON", encoding="utf-8")
    hashed = Mock(wraps=receipts._validate_parsed_receipt)
    monkeypatch.setattr(receipts, "_validate_parsed_receipt", hashed)

    def unwritable(*_args, **_kwargs):
        raise PermissionError("cache unavailable")

    monkeypatch.setattr(receipts, "atomic_write_json", unwritable)
    assert receipts.ModelReceiptVerifier(cache_path=cache_path).verify(root, **identity)
    assert hashed.call_count == 1
