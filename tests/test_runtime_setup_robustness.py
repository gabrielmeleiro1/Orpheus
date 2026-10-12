"""Runtime setup survives corrupt files, full disks, races and hostile names."""

from __future__ import annotations

import errno
import json
import os
import threading
from contextlib import nullcontext
from pathlib import Path

import pytest

from modules.app.services.model_download_service import ModelDownloadBusyError, ModelDownloadService
from modules.runtime_setup import receipts, state as setup_state
from modules.runtime_setup.catalog import MODEL_GROUPS, resolve_model_groups
from modules.runtime_setup.downloads import _local_dir_for_model
from modules.runtime_setup.errors import SetupExecutionError

REVISION = "a" * 40
MODEL_ID = "org/model"


windows_only = pytest.mark.skipif(os.name != "nt", reason="drive and stream names are Windows path syntax")


@windows_only
@pytest.mark.parametrize("path", ["Z:victim.bin", "C:victim.bin", "weights.bin:stream", "dir/Z:victim.bin"])
def test_receipt_entries_cannot_name_a_drive_or_alternate_stream(path) -> None:
    # On Windows ``snapshot / "Z:victim.bin"`` leaves the snapshot for the
    # current directory of drive Z:, and repair would delete that file.
    assert receipts._valid_receipt_relative_path(path) is None


def _snapshot_with_receipt(tmp_path: Path, payload) -> Path:
    snapshot = tmp_path / "models--org--model" / "snapshots" / REVISION
    snapshot.mkdir(parents=True)
    (snapshot / "weights.bin").write_bytes(b"weights")
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    (snapshot / receipts.DOWNLOAD_RECEIPT_FILENAME).write_bytes(raw)
    return snapshot


@windows_only
def test_receipt_listing_a_drive_relative_entry_is_untrusted(tmp_path) -> None:
    snapshot = _snapshot_with_receipt(tmp_path, {
        "schema_version": receipts.DOWNLOAD_RECEIPT_SCHEMA_VERSION, "model_id": MODEL_ID, "revision": REVISION,
        "files": [{"path": "Z:victim.bin", "size": 7, "sha256": "0" * 64}],
    })

    assert receipts._parse_download_receipt(snapshot, model_id=MODEL_ID, revision=REVISION) is None


@pytest.mark.parametrize("raw", [
    b"", b"{", b'{"schema_version": 1, "files": [', b"\xff\xfe\x00garbage", b"null", b"[]",
    b"[" * 200_000,
], ids=["empty", "brace", "truncated", "binary", "null", "list", "deeply-nested"])
def test_corrupt_or_truncated_receipt_is_untrusted_not_a_crash(tmp_path, raw) -> None:
    snapshot = _snapshot_with_receipt(tmp_path, raw)

    assert receipts._parse_download_receipt(snapshot, model_id=MODEL_ID, revision=REVISION) is None


@pytest.mark.parametrize("raw", [
    b"", b"{", b'{"schema_version": 1, "profile": "cuda", "model_groups": [', b"\xff\xfe\x00garbage",
    b"null", b"[]", b'{"schema_version": 1, "profile": [], "model_groups": 5}', b"[" * 200_000,
], ids=["empty", "brace", "truncated", "binary", "null", "list", "wrong-types", "deeply-nested"])
def test_corrupt_or_truncated_setup_state_reads_as_invalid(tmp_path, raw) -> None:
    path = tmp_path / "setup-state.json"
    path.write_bytes(raw)

    state, error = setup_state._read_setup_state(path)

    assert state is None and error
    assert setup_state.get_setup_state_cache_key(state_path=path)[0] == "invalid"


def test_full_disk_while_saving_setup_state_keeps_the_previous_state(tmp_path, monkeypatch) -> None:
    path = tmp_path / "setup-state.json"
    group = next(group for group in MODEL_GROUPS if group.startswith("tts-cpu"))
    profile = "cuda" if "cuda" in group else "base"
    saved = setup_state.write_setup_state(path, profile_id=profile, model_groups=(group,), accepted_risks=())
    before = path.read_bytes()

    def full_disk(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(setup_state, "atomic_write_json", full_disk)
    with pytest.raises(SetupExecutionError, match="Could not write runtime setup state"):
        setup_state.write_setup_state(path, profile_id=profile, model_groups=(group,), accepted_risks=())

    assert path.read_bytes() == before
    assert setup_state._read_setup_state(path)[0]["installation_id"] == saved["installation_id"]


@pytest.mark.parametrize("group_id", ["../../outside", "tts-cpu-cuda/../../x", "C:\\Windows", "con", ""])
def test_hostile_model_group_ids_are_refused_before_any_filesystem_work(group_id) -> None:
    with pytest.raises(ValueError, match="Unknown model group"):
        resolve_model_groups([group_id])


@pytest.mark.parametrize("model_id", ["../../outside", "org/../../x", "C:/Windows/System32", "..", "a\\..\\..\\b"])
def test_per_model_download_directories_stay_inside_the_requested_root(tmp_path, model_id) -> None:
    target = _local_dir_for_model(tmp_path / "models", model_id, multiple_models=True)

    assert target.parent == tmp_path / "models"
    assert target.resolve().is_relative_to((tmp_path / "models").resolve())


def _service(downloader) -> ModelDownloadService:
    return ModelDownloadService(
        downloader=downloader, group_resolver=lambda groups: tuple(f"org/{group}" for group in groups),
        lock_factory=nullcontext,
    )


def test_concurrent_download_requests_start_exactly_one_download(monkeypatch) -> None:
    monkeypatch.setattr(setup_state, "pending_model_deletions", lambda *_args, **_kwargs: [])
    release = threading.Event()
    started: list[str] = []

    def downloader(model_id):
        started.append(model_id)
        release.wait(5)

    service = _service(downloader)
    barrier = threading.Barrier(4)
    outcomes: list[object] = []

    def request():
        barrier.wait()
        try:
            outcomes.append(service.start(profile_id="base", model_groups=["one"]))
        except ModelDownloadBusyError as error:
            outcomes.append(error)

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    release.set()
    service.shutdown(timeout_seconds=5)

    assert sum(isinstance(outcome, ModelDownloadBusyError) for outcome in outcomes) == 3
    assert started == ["org/one"]


def test_cancel_mid_download_counts_nothing_as_installed_and_releases_the_lock(monkeypatch) -> None:
    monkeypatch.setattr(setup_state, "pending_model_deletions", lambda *_args, **_kwargs: [])
    in_download = threading.Event()
    released: list[bool] = []

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            released.append(True)

    def downloader(_model_id):
        in_download.set()
        while not service.cancel_requested:
            threading.Event().wait(0.01)

    service = ModelDownloadService(
        downloader=downloader, group_resolver=lambda groups: ("org/a", "org/b"), lock_factory=Lock,
    )
    service.start(profile_id="base", model_groups=["one"])
    assert in_download.wait(5)
    service.request_cancel()
    assert service.shutdown(timeout_seconds=5)

    status = service.status()
    assert status["lifecycle"] == "cancelled"
    assert status["completed_models"] == 0
    assert status["error"] is None
    assert released == [True]


def _standalone_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AUDIOBOOK_DATA_DIR", raising=False)
    for name, relative in (("APPDATA", "roaming"), ("LOCALAPPDATA", "local"), ("XDG_DATA_HOME", "xdg")):
        monkeypatch.setenv(name, str(tmp_path / relative))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


def _cpu_group() -> tuple[str, str]:
    group = next(group for group in MODEL_GROUPS if group.startswith("tts-cpu"))
    return ("cuda" if "cuda" in group else "base"), group


def test_standalone_setup_state_lives_in_the_desktop_app_data_directory(tmp_path, monkeypatch) -> None:
    _standalone_environment(monkeypatch, tmp_path)
    profile, group = _cpu_group()

    path = setup_state.default_state_path()
    setup_state.write_setup_state(path, profile_id=profile, model_groups=(group,), accepted_risks=())

    assert path.parent.name == setup_state.DESKTOP_APP_IDENTIFIER
    assert path.is_file()
    # Nothing lands in the legacy (Windows: installer) folder.
    assert not setup_state._legacy_default_state_path().exists()


def test_legacy_standalone_state_is_read_but_never_modified(tmp_path, monkeypatch) -> None:
    _standalone_environment(monkeypatch, tmp_path)
    profile, group = _cpu_group()
    legacy = setup_state._legacy_default_state_path()
    saved = setup_state.write_setup_state(legacy, profile_id=profile, model_groups=(group,), accepted_risks=())
    legacy_bytes = legacy.read_bytes()
    current = setup_state.default_state_path()
    assert not current.exists()

    state, error = setup_state._read_setup_state(current)
    assert error is None and state["installation_id"] == saved["installation_id"]
    assert setup_state.get_setup_state_cache_key()[0] == "configured"

    rewritten = setup_state.write_setup_state(current, profile_id=profile, model_groups=(group,), accepted_risks=())
    assert setup_state._read_setup_state(current)[0]["installation_id"] == rewritten["installation_id"]
    assert legacy.read_bytes() == legacy_bytes


def test_desktop_data_directory_never_falls_back_to_legacy_state(tmp_path, monkeypatch) -> None:
    _standalone_environment(monkeypatch, tmp_path)
    profile, group = _cpu_group()
    setup_state.write_setup_state(
        setup_state._legacy_default_state_path(), profile_id=profile, model_groups=(group,), accepted_risks=(),
    )
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "desktop" / "data"))

    assert setup_state._read_setup_state(setup_state.default_state_path()) == (None, None)
