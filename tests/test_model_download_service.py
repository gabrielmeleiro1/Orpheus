import importlib
import sys
from types import ModuleType
from unittest.mock import Mock

import httpx
import huggingface_hub
import pytest
from huggingface_hub.errors import LocalEntryNotFoundError, OfflineModeIsEnabled

from modules.app.services import model_download_service as downloads
from scripts.download_models import MODEL_GROUPS


def test_service_reports_byte_updates_before_the_worker_finishes(monkeypatch):
    service = downloads.ModelDownloadService()
    process = Mock(exitcode=0)
    process.is_alive.side_effect = [True, False, False, False]
    messages = [
        {"bytes_total": 1800, "bytes_done": 0},
        {"bytes_total": 1800, "bytes_done": 1200},
        None,
    ]
    reader, writer = Mock(), Mock()
    reader.poll.side_effect = lambda: bool(messages)
    def receive():
        if messages[0] is None:
            assert service.status()["bytes_total"] == 1800
            assert service.status()["bytes_done"] == 1200
        return messages.pop(0)
    reader.recv.side_effect = receive
    context = Mock()
    context.Pipe.return_value = reader, writer
    context.Process.return_value = process
    monkeypatch.setattr(downloads.multiprocessing, "get_context", lambda _: context)

    service._download_interruptibly("org/model")

    assert service.status()["bytes_done"] == 1200


def test_worker_reports_pinned_file_bytes_and_throttles_updates(monkeypatch, tmp_path):
    from types import SimpleNamespace

    model_id = MODEL_GROUPS["tts-mlx"][0]
    revision = downloads.pinned_revision_for(model_id)
    monkeypatch.setattr(downloads.hf_constants, "HF_HUB_CACHE", str(tmp_path))
    snapshot = tmp_path / ("models--" + model_id.replace("/", "--")) / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_bytes(b"{}")
    info = SimpleNamespace(siblings=[
        SimpleNamespace(rfilename="config.json", size=2),
        SimpleNamespace(rfilename="model.safetensors", size=1798),
    ])
    api = Mock()
    api.model_info.return_value = info
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: api)
    clock = [0.0]
    monkeypatch.setattr(downloads, "monotonic", lambda: clock[0], raising=False)
    result = Mock()
    def download(model, *, tqdm_class):
        assert model == model_id
        with tqdm_class(total=1798, unit="B", name="huggingface_hub.snapshot_download", disable=True) as progress:
            progress.update(100)
            progress.update(100)
            clock[0] = 1.1
            progress.update(998)
        with tqdm_class(total=1800, unit="B", name="huggingface_hub.snapshot_download.transfer") as transfer:
            transfer.update(900)
        with tqdm_class(total=2, unit="it") as files:
            files.update(2)
        updates = [call.args[0] for call in result.send.call_args_list]
        assert updates == [
            {"bytes_total": 1800, "bytes_done": 2},
            {"bytes_total": 1800, "bytes_done": 1200},
        ]
    monkeypatch.setattr(downloads, "download_model_snapshot", download)

    downloads._download_worker(model_id, result)

    api.model_info.assert_called_once_with(model_id, revision=revision, files_metadata=True, timeout=10)
    assert [call.args[0] for call in result.send.call_args_list][-2:] == [
        {"bytes_total": 1800, "bytes_done": 1800}, None,
    ]
    result.close.assert_called_once()


@pytest.mark.parametrize("damage", [None, "receipt", "weights"])
def test_worker_checks_verified_local_snapshot_before_metadata_http(monkeypatch, tmp_path, damage):
    from modules.runtime_setup.receipts import DOWNLOAD_RECEIPT_FILENAME, write_download_receipt

    model_id = MODEL_GROUPS["tts-mlx"][0]
    revision = downloads.pinned_revision_for(model_id)
    monkeypatch.setattr(downloads.hf_constants, "HF_HUB_CACHE", str(tmp_path))
    snapshot = tmp_path / ("models--" + model_id.replace("/", "--")) / "snapshots" / revision
    snapshot.mkdir(parents=True)
    weights = snapshot / "model.safetensors"
    weights.write_bytes(b"weights")
    write_download_receipt(snapshot, model_id=model_id, revision=revision)
    if damage == "receipt":
        (snapshot / DOWNLOAD_RECEIPT_FILENAME).write_text("broken")
    elif damage == "weights":
        weights.write_bytes(b"changed")
    metadata = Mock(return_value=(7, 0))
    download = Mock()
    monkeypatch.setattr(downloads, "_model_byte_metadata", metadata)
    monkeypatch.setattr(downloads, "download_model_snapshot", download)
    monkeypatch.setattr(httpx.Client, "send", lambda *args, **kwargs: pytest.fail("Snapshot verification attempted HTTP"))
    result = Mock()

    downloads._download_worker(model_id, result)

    assert metadata.call_count == download.call_count == (1 if damage else 0)
    assert result.send.call_args_list[-1].args == (None,)
    result.close.assert_called_once()


@pytest.mark.parametrize("transport", ["http", "xet"])
def test_snapshot_hook_counts_file_bytes_for_each_transport(monkeypatch, tmp_path, transport):
    from contextlib import contextmanager
    from io import BytesIO
    from itertools import count
    from types import SimpleNamespace

    from huggingface_hub import ModelInfo, RepoFile, _snapshot_download, file_download, snapshot_download
    from huggingface_hub.utils import _xet
    from huggingface_hub.utils._xet import XetFileData

    clock = count()
    monkeypatch.setattr(downloads, "monotonic", lambda: next(clock) * 1.1)
    api = Mock()
    api.repo_info.return_value = ModelInfo(id="org/model", sha="a" * 40, siblings=[{"rfilename": "config.json"}])
    api.list_repo_tree.return_value = [RepoFile(path="config.json", size=1000, oid="b" * 40)]
    monkeypatch.setattr(_snapshot_download, "HfApi", lambda **kwargs: api)
    result = Mock()
    progress_class = downloads._byte_progress_class(result, 1000, 0)

    @contextmanager
    def stream(*args, **kwargs):
        yield httpx.Response(206, headers={"content-length": "800", "content-range": "bytes 200-999/1000"}, content=b"x" * 800,
                             request=httpx.Request("GET", "https://huggingface.co/org/model/config.json"))
    monkeypatch.setattr(file_download, "http_stream_backoff", stream)

    def download(repo_id, *, tqdm_class, **kwargs):
        if transport == "http":
            partial = BytesIO(b"x" * 200)
            partial.seek(0, 2)
            file_download.http_get(
                "https://huggingface.co/org/model/config.json", partial,
                resume_size=200, expected_size=1000, tqdm_class=tqdm_class,
            )
            assert partial.tell() == 1000
            assert partial.getvalue() == b"x" * 1000
        else:
            monkeypatch.setitem(sys.modules, "hf_xet", SimpleNamespace(XetFileInfo=lambda *args: args))
            @contextmanager
            def group(**options):
                def start(*args):
                    options["progress_callback"](SimpleNamespace(
                        total_bytes=1000,
                        total_bytes_completed=400, total_transfer_bytes_completed=150,
                        total_bytes_completion_rate=400, total_transfer_bytes_completion_rate=150,
                    ), {})
                    options["progress_callback"](SimpleNamespace(
                        total_bytes=1000,
                        total_bytes_completed=1000, total_transfer_bytes_completed=300,
                        total_bytes_completion_rate=600, total_transfer_bytes_completion_rate=150,
                    ), {})
                yield SimpleNamespace(start_download_file=start)
            monkeypatch.setattr(_xet, "get_xet_session", lambda: SimpleNamespace(new_file_download_group=group))
            monkeypatch.setattr(_xet, "refresh_xet_connection_info", lambda **kwargs: SimpleNamespace(
                endpoint="https://cas.example", access_token="token", expiration_unix_epoch=0,
            ))
            file_download.xet_get(
                incomplete_path=tmp_path / "model.incomplete",
                xet_file_data=XetFileData(file_hash="a" * 64, refresh_route="https://huggingface.co/token"),
                headers={}, expected_size=1000, tqdm_class=tqdm_class,
            )
        return str(tmp_path / "config.json")
    monkeypatch.setattr(_snapshot_download, "hf_hub_download", download)

    snapshot_download("org/model", revision="a" * 40, cache_dir=tmp_path, tqdm_class=progress_class)

    updates = [call.args[0] for call in result.send.call_args_list]
    assert updates[0] == {"bytes_total": 1000, "bytes_done": 200 if transport == "http" else 400}
    assert updates[-1] == {"bytes_total": 1000, "bytes_done": 1000}


def test_worker_downloads_without_byte_metadata_when_the_hub_is_unreachable(monkeypatch):
    api = Mock()
    api.model_info.side_effect = httpx.ConnectError("offline")
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: api)
    download = Mock()
    monkeypatch.setattr(downloads, "download_model_snapshot", download)
    result = Mock()

    downloads._download_worker(MODEL_GROUPS["tts-mlx"][0], result)

    download.assert_called_once()
    assert [call.args[0] for call in result.send.call_args_list] == [
        {"bytes_total": None, "bytes_done": None}, None,
    ]


@pytest.mark.parametrize("metadata_times_out", [False, True])
def test_worker_bounds_metadata_requests_and_downloads_after_a_timeout(monkeypatch, tmp_path, metadata_times_out):
    from huggingface_hub import hf_api

    model_id = MODEL_GROUPS["tts-mlx"][0]
    requests = []
    def respond(request):
        requests.append(request)
        if metadata_times_out:
            raise httpx.ReadTimeout("metadata timed out", request=request)
        return httpx.Response(200, json={
            "id": model_id,
            "siblings": [{"rfilename": "config.json", "size": 7}],
        })

    api = huggingface_hub.HfApi(token=False)
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: api)
    monkeypatch.setattr(downloads.hf_constants, "HF_HUB_CACHE", str(tmp_path))
    download = Mock()
    monkeypatch.setattr(downloads, "download_model_snapshot", download)
    result = Mock()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(hf_api, "get_session", lambda: client)
        downloads._download_worker(model_id, result)

    assert len(requests) == 1
    assert all(timeout is not None and 0 < timeout <= 10 for timeout in requests[0].extensions["timeout"].values())
    download.assert_called_once()
    updates = [call.args[0] for call in result.send.call_args_list]
    assert updates == (
        [{"bytes_total": None, "bytes_done": None}, None] if metadata_times_out else
        [{"bytes_total": 7, "bytes_done": 0}, {"bytes_total": 7, "bytes_done": 7}, None]
    )
    result.close.assert_called_once()


def test_service_clears_byte_counts_when_the_next_model_starts():
    service = downloads.ModelDownloadService(group_resolver=lambda _: ("org/first", "org/second"))
    seen = []
    def download(model):
        seen.append((model, service.status()["bytes_total"], service.status()["bytes_done"]))
        with service._mutex:
            service._state.update(bytes_total=1800, bytes_done=1800)
    service._downloader = download

    service._run([("tts-mlx", ("org/first", "org/second"))], Mock())

    assert seen == [("org/first", None, None), ("org/second", None, None)]


def test_service_imports_and_maps_connection_failures_without_requests(monkeypatch):
    monkeypatch.setitem(sys.modules, "requests", None)
    service_module = ModuleType(downloads.__name__)
    monkeypatch.setitem(sys.modules, downloads.__name__, service_module)
    importlib.reload(service_module)
    assert service_module._user_facing_error(httpx.ConnectError("dns"), "org/model") == (
        "Could not reach huggingface.co. "
        "Check your internet connection and try again."
    )


@pytest.mark.parametrize("error", [
    httpx.ConnectError("dns"),
    httpx.ReadError("connection lost"),
    httpx.ReadTimeout("timed out"),
    LocalEntryNotFoundError("not cached"),
    OfflineModeIsEnabled("offline"),
])
def test_network_failures_explain_how_to_retry(error):
    assert "Could not reach huggingface.co" in downloads._user_facing_error(error, "org/model")


@pytest.mark.parametrize("outcome", ["cancelled", "failed", "abrupt", "completed"])
def test_worker_exit_cleans_only_failed_models_partial_blobs(monkeypatch, tmp_path, outcome):
    from huggingface_hub import constants

    cache = tmp_path / "hub"
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))
    model_id = MODEL_GROUPS["tts-mlx"][0]
    blobs = cache / ("models--" + model_id.replace("/", "--")) / "blobs"
    blobs.mkdir(parents=True)
    partials = [blobs / "etag.a1b2c3d4.incomplete", blobs / "other.incomplete"]
    preserved = [blobs / "complete", blobs / "not.incomplete.backup", blobs / "foreign.incomplete",
                 cache / "models--other--model" / "blobs" / "etag.random.incomplete"]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep")
    external = tmp_path / "external.incomplete"
    external.write_bytes(b"keep")
    link = blobs / "linked.incomplete"
    link.symlink_to(external)
    directory = blobs / "directory.incomplete"
    directory.mkdir()
    (directory / "nested.incomplete").write_bytes(b"keep")

    service = downloads.ModelDownloadService()
    process = Mock(exitcode=1 if outcome == "abrupt" else 0)
    process.is_alive.side_effect = [outcome == "cancelled", False, False]
    def start():
        for path in partials:
            path.write_bytes(b"partial")
        if outcome == "cancelled":
            service._cancel_event.set()
    process.start.side_effect = start
    def join(**kwargs):
        assert all(path.exists() for path in partials)
    process.join.side_effect = join
    reader, writer = Mock(), Mock()
    reader.poll.return_value = outcome != "abrupt"
    reader.recv.return_value = "download failed" if outcome == "failed" else None
    context = Mock()
    context.Pipe.return_value = reader, writer
    context.Process.return_value = process
    monkeypatch.setattr(downloads.multiprocessing, "get_context", lambda _: context)

    if outcome in {"failed", "abrupt"}:
        with pytest.raises(downloads.DownloadWorkerError):
            service._download_interruptibly(model_id)
    else:
        service._download_interruptibly(model_id)

    process.join.assert_called_once()
    if outcome == "cancelled":
        process.terminate.assert_called_once()
    assert all(path.exists() == (outcome == "completed") for path in partials)
    assert all(path.read_bytes() == b"keep" for path in preserved)
    assert link.is_symlink()
    assert external.read_bytes() == b"keep"
    assert (directory / "nested.incomplete").read_bytes() == b"keep"


def test_partial_cleanup_continues_after_a_locked_file(monkeypatch, tmp_path):
    from pathlib import Path

    model_id = MODEL_GROUPS["tts-mlx"][0]
    cache = tmp_path / "hub"
    blobs = cache / ("models--" + model_id.replace("/", "--")) / "blobs"
    blobs.mkdir(parents=True)
    monkeypatch.setattr(downloads.hf_constants, "HF_HUB_CACHE", str(cache))
    locked = blobs / "locked.incomplete"
    owned = blobs / "owned.incomplete"
    foreign = blobs / "foreign.incomplete"
    for path in (locked, owned, foreign):
        path.write_bytes(b"partial")
    unlink = Path.unlink
    def remove(path, **kwargs):
        if path == locked:
            raise PermissionError("locked")
        return unlink(path, **kwargs)
    monkeypatch.setattr(Path, "unlink", remove)

    downloads._remove_incomplete_blobs(model_id, {foreign.name})

    assert locked.read_bytes() == b"partial"
    assert foreign.read_bytes() == b"partial"
    assert not owned.exists()


@pytest.mark.parametrize("linked_directory", ["hub", "repo", "blobs"])
def test_partial_cleanup_does_not_follow_cache_directory_symlinks(monkeypatch, tmp_path, linked_directory):
    from huggingface_hub import constants

    model_id = MODEL_GROUPS["tts-mlx"][0]
    cache = tmp_path / "hub"
    repo = cache / ("models--" + model_id.replace("/", "--"))
    blobs = repo / "blobs"
    linked = {"hub": cache, "repo": repo, "blobs": blobs}[linked_directory]
    external = tmp_path / "external"
    external.mkdir()
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(external, target_is_directory=True)
    blobs.mkdir(parents=True, exist_ok=True)
    partial = blobs / "etag.random.incomplete"
    partial.write_bytes(b"keep")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))

    downloads._remove_incomplete_blobs(model_id, set())

    assert partial.read_bytes() == b"keep"
