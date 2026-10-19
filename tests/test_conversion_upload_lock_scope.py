"""Hashing a recovered upload must not hold the conversion job lock."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from modules.app.controller import ConversionJob, JobStatus
from modules.app.services import conversion_service as conversion_module
from modules.pipeline.models import ConversionResult
from tests.test_backup_restore import controller as controller_fixture

controller = controller_fixture


def _registered_upload(controller):
    uploaded = controller.upload_text_source(text="Original words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    service = controller._conversion_service
    durable = service._durable_source_path_for_conversion(source)
    recovery = controller.config.output_dir / "Book" / ".orpheus-source" / source.name
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(source.read_bytes())
    job = ConversionJob(id="job", book_id=None, status=JobStatus.RUNNING)
    job.upload_source_path = source.resolve()
    command = SimpleNamespace(job=job, source_path=durable, conversion_mode="audiobook", runtime_options=None)
    return service, source, recovery, command


def _watch_hashing(monkeypatch, service):
    held: list[bool] = []
    real_digest = hashlib.file_digest

    def watched(stream, digest):
        held.append(service._job_lock.locked())
        return real_digest(stream, digest)

    monkeypatch.setattr(conversion_module.hashlib, "file_digest", watched)
    return held


def test_checkpoint_and_handle_resolution_hash_outside_the_job_lock(controller, monkeypatch) -> None:
    service, source, recovery, command = _registered_upload(controller)
    held = _watch_hashing(monkeypatch, service)

    service._checkpoint_conversion(command, recovery, ConversionResult(success=False, title="Book", author=None))
    assert not source.exists()
    assert held and not any(held), held

    held.clear()
    assert service.resolve_recovered_upload(source) == recovery
    assert held and not any(held), held


def test_recovery_source_rewritten_after_hashing_is_hashed_again_under_the_lock(controller, monkeypatch) -> None:
    service, source, recovery, command = _registered_upload(controller)
    real_prehash = conversion_module._prehash_recovery_source

    def prehash_then_rewrite(upload, recovery_source):
        prehashed = real_prehash(upload, recovery_source)
        recovery_source.write_bytes(b"Rewritten words.")
        return prehashed

    with monkeypatch.context() as patched:
        patched.setattr(conversion_module, "_prehash_recovery_source", prehash_then_rewrite)
        service._checkpoint_conversion(command, recovery, ConversionResult(success=False, title="Book", author=None))

    # The handle is registered for the bytes that were actually committed.
    assert recovery.read_bytes() == b"Rewritten words."
    assert service.resolve_recovered_upload(source) == recovery


def test_handle_is_refused_when_the_source_changes_while_it_is_hashed(controller, monkeypatch) -> None:
    service, source, recovery, command = _registered_upload(controller)
    service._checkpoint_conversion(command, recovery, ConversionResult(success=False, title="Book", author=None))
    real_prehash = conversion_module._prehash_recovery_source

    def prehash_then_replace(upload, recovery_source):
        prehashed = real_prehash(upload, recovery_source)
        recovery_source.write_bytes(b"Different words, same handle.")
        return prehashed

    monkeypatch.setattr(conversion_module, "_prehash_recovery_source", prehash_then_replace)

    assert service.resolve_recovered_upload(source) is None


def test_handle_is_refused_when_a_restore_lands_while_it_is_hashed(controller, monkeypatch) -> None:
    service, source, recovery, command = _registered_upload(controller)
    service._checkpoint_conversion(command, recovery, ConversionResult(success=False, title="Book", author=None))
    real_prehash = conversion_module._prehash_recovery_source

    def prehash_then_invalidate(upload, recovery_source):
        prehashed = real_prehash(upload, recovery_source)
        service.invalidate_recovered_uploads()
        return prehashed

    monkeypatch.setattr(conversion_module, "_prehash_recovery_source", prehash_then_invalidate)

    assert service.resolve_recovered_upload(source) is None
