"""Regression coverage for runtime setup receipts."""

from __future__ import annotations

import threading

import pytest

from modules.runtime_setup import receipts


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_receipt_verification_error_releases_waiting_retry(monkeypatch, tmp_path, failure_type):
    model_id = "hexgrad/Kokoro-82M"
    revision = "a" * 40
    (tmp_path / "weights.bin").write_bytes(b"model weights")
    receipts.write_download_receipt(tmp_path, model_id=model_id, revision=revision)
    verifier = receipts.ModelReceiptVerifier()
    hashing = threading.Event()
    release_hash = threading.Event()
    waiting = threading.Event()
    retried = threading.Event()
    errors = []
    results = []
    hash_file = receipts._file_size_and_sha256
    wait = verifier._condition.wait
    failure = failure_type("hashing interrupted")

    def interrupted_hash(path):
        if not hashing.is_set():
            hashing.set()
            assert release_hash.wait(3), "Interrupted verification was not released"
            raise failure
        return hash_file(path)

    def observed_wait(*args, **kwargs):
        waiting.set()
        return wait(*args, **kwargs)

    def verify(*, retry=False):
        try:
            results.append(verifier.verify(tmp_path, model_id=model_id, revision=revision))
        except BaseException as exc:
            errors.append(exc)
        finally:
            if retry:
                retried.set()

    monkeypatch.setattr(receipts, "_file_size_and_sha256", interrupted_hash)
    monkeypatch.setattr(verifier._condition, "wait", observed_wait)
    first = threading.Thread(target=verify, daemon=True)
    second = threading.Thread(target=verify, kwargs={"retry": True}, daemon=True)
    first.start()
    try:
        assert hashing.wait(3), "First verification did not start hashing"
        second.start()
        assert waiting.wait(3), "Second verification did not wait for the first"
        release_hash.set()
        assert retried.wait(3), "Verification failure left the retry blocked"
        assert results == [True]
        assert errors == [failure]
    finally:
        release_hash.set()
        # Release a waiter even on the broken implementation so the regression
        # never leaks a blocked thread into another test.
        with verifier._condition:
            verifier._inflight.clear()
            verifier._condition.notify_all()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()
