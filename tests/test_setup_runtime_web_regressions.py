"""Regression coverage for setup storage fencing, shutdown, voice and web plumbing."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from huggingface_hub import scan_cache_dir

from modules.app.config import AppConfig
from modules.app.services.runtime_capability_service import RuntimeCapabilityService
from modules.app.controller import AppController, ConversionCallbacks, ConversionJob, JobStatus
from modules.app.services import conversion_service as conversion_service_module
from modules.app.services import model_storage_service, voice_service
from modules.app.services.tts_session import TtsEngineSession
from modules.app.services.voice_service import VoiceService
from modules.runtime_setup import hub_cache
from modules.runtime_setup.catalog import resolve_model_groups
from modules.storage.models import ConversionResult
from modules.tts import model_cache
from modules.web.desktop_auth import _DesktopAuthMiddleware
from modules.web.events import STREAM_CLOSE_SENTINEL, EventBroker, async_event_stream
from modules.web.routes import setup as setup_route
from modules.web.server import create_app
from modules.web.setup_status_cache import RuntimeSetupStatusCache
from tests.test_conversion_service import _make_service
from tests.test_model_storage_service_regressions import _Engine, _paused_call


class _ConversionFence:
    """Conversion-service double for the storage-cleanup reservation."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.blocker: str | None = None
        self.active = False

    def begin_storage_cleanup(self) -> None:
        if self.blocker is not None or self.active:
            raise RuntimeError("Cannot clean up storage while a conversion is queued or running")
        self.active = True
        self.calls.append("begin")

    def end_storage_cleanup(self) -> None:
        self.active = False
        self.calls.append("end")

    def storage_cleanup_blocker(self) -> str | None:
        return self.blocker


@pytest.fixture
def storage(monkeypatch, tmp_path):
    cache = tmp_path / "models"
    model_id = resolve_model_groups(("tts-cpu-cuda",))[0]
    repo = cache / ("models--" + model_id.replace("/", "--"))
    snapshot = repo / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (repo / "refs").mkdir()
    (repo / "refs/main").write_text("a" * 40)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(hub_cache.hf_constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(hub_cache, "scan_cache_dir", lambda: scan_cache_dir(cache))
    monkeypatch.setattr(voice_service.sf, "write", lambda *args: None)

    config = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out", temp_dir=tmp_path / "temp")
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    service = VoiceService(config, tts_factory=SimpleNamespace(create=lambda *args, **kwargs: _Engine(snapshot)))
    calls: list[str] = []
    fence = _ConversionFence(calls)
    released: list[bool] = []

    def release_resident_models():
        # The reservation must already exclude conversions, and nothing may
        # have been staged yet.
        calls.append("release")
        assert fence.active
        assert repo.exists()
        return released.pop(0) if released else True

    controller = SimpleNamespace(
        generate_voice_preview=service.generate_voice_preview,
        _conversion_service=fence,
        release_resident_models=release_resident_models,
    )
    model_cache.clear_model_cache()
    with TestClient(create_app(controller=controller)) as client:
        try:
            yield SimpleNamespace(
                client=client, repo=repo, calls=calls, fence=fence, service=service, released=released,
            )
        finally:
            service.cleanup()
            model_cache.clear_model_cache()


def _write_committed_stage(repo: Path) -> Path:
    # A committed deletion of another managed model whose files remain staged.
    model_id = resolve_model_groups(("tts-mlx",))[0]
    stage = repo.with_name(".orpheus-delete-" + "b" * 32)
    (stage / "snapshots").mkdir(parents=True)
    stage.with_name(stage.name + ".json").write_text(json.dumps({
        "original": "models--" + model_id.replace("/", "--"), "staged": stage.name, "settings_committed": True,
    }))
    return stage


# ---- staged model recovery vs. running conversions ----

def test_model_listing_skips_staged_recovery_while_a_conversion_runs(storage):
    stage = _write_committed_stage(storage.repo)
    storage.fence.blocker = "conversion_in_progress"

    response = storage.client.get("/api/setup/models")

    assert response.status_code == 200, response.text
    assert stage.exists()
    assert storage.calls == []

    storage.fence.blocker = None
    assert storage.client.get("/api/setup/models").status_code == 200
    assert not stage.exists()
    assert storage.calls == ["begin", "end"]


def test_adopt_refuses_staged_recovery_while_a_conversion_runs(storage, monkeypatch):
    monkeypatch.setattr(setup_route, "build_setup_status_response", lambda _request: {})
    stage = _write_committed_stage(storage.repo)
    storage.fence.blocker = "conversion_in_progress"

    response = storage.client.post("/api/setup/adopt", json={"profile": "base", "model_groups": []})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "conversion_in_progress"
    assert stage.exists()

    storage.fence.blocker = None
    retry = storage.client.post("/api/setup/adopt", json={"profile": "base", "model_groups": []})
    assert retry.status_code == 200, retry.text
    assert not stage.exists()
    assert storage.calls == ["begin", "end"]


def test_adopt_without_staged_models_leaves_conversions_unfenced(storage, monkeypatch):
    monkeypatch.setattr(setup_route, "build_setup_status_response", lambda _request: {})
    storage.fence.blocker = "conversion_in_progress"

    response = storage.client.post("/api/setup/adopt", json={"profile": "base", "model_groups": []})

    assert response.status_code == 200, response.text
    assert storage.calls == []


# ---- deletion releases models a finished conversion keeps loaded ----

def test_model_deletion_releases_resident_conversion_models_under_the_fence(storage):
    response = storage.client.delete("/api/setup/models/tts-cpu-cuda")

    assert response.status_code == 200, response.text
    assert storage.calls == ["begin", "release", "end"]
    assert not storage.repo.exists()


def test_model_deletion_refuses_when_pool_workers_do_not_exit(storage):
    storage.released.append(False)

    response = storage.client.delete("/api/setup/models/tts-cpu-cuda")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "conversion_cleanup_pending"
    assert storage.repo.exists()
    assert storage.calls == ["begin", "release", "end"]


def test_runtime_uninstall_releases_resident_conversion_models_under_the_fence(storage):
    response = storage.client.delete("/api/setup/runtime")

    assert response.status_code == 200, response.text
    assert storage.calls == ["begin", "release", "end"]
    assert not storage.repo.exists()


def test_runtime_uninstall_reports_preview_and_invalidates_status_cache(storage, monkeypatch):
    invalidations: list[str] = []
    monkeypatch.setattr(RuntimeSetupStatusCache, "invalidate", lambda self: invalidations.append("status"))

    with _paused_call(monkeypatch, _Engine, "synthesize", lambda: storage.service.generate_voice_preview("am_adam")):
        response = storage.client.delete("/api/setup/runtime")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "preview_in_progress"
    assert invalidations == ["status"]
    assert storage.repo.exists()
    assert storage.calls == ["begin", "release", "end"]


def test_voice_preview_during_model_deletion_is_a_conflict(storage, monkeypatch):
    with _paused_call(
        monkeypatch, model_storage_service.shutil, "rmtree",
        lambda: storage.client.delete("/api/setup/models/tts-cpu-cuda"),
    ) as deletion:
        response = storage.client.post("/api/voices/preview", json={"voice": "am_adam"})
        assert response.status_code == 409
        assert "model deletion" in response.json()["detail"].lower()
    assert deletion.result().status_code == 200


def test_controller_release_unloads_the_warm_engine_and_stops_the_pool():
    controller = object.__new__(AppController)
    order: list[str] = []
    controller._conversion_service = Mock(cleanup=Mock(side_effect=lambda: order.append("engine")))
    controller._shared_worker_pool_manager = Mock(
        shutdown=Mock(side_effect=lambda **kwargs: order.append(f"pool:{kwargs['timeout_seconds']}") or True),
    )

    assert controller.release_resident_models() is True
    assert order == ["engine", f"pool:{AppController._CLEANUP_WORKER_SHUTDOWN_TIMEOUT_SECONDS}"]

    controller._shared_worker_pool_manager.shutdown = Mock(return_value=False)
    assert controller.release_resident_models() is False


# ---- shutdown always closes SQLite ----

def _quiesced_controller(voice: Any) -> AppController:
    controller = object.__new__(AppController)
    controller._conversion_service = Mock(cancel_active_job=Mock(return_value=True))
    controller._shared_worker_pool_manager = None
    controller._voice_service = voice
    controller._cleaner_api_limiter = Mock()
    controller._owns_repository = True
    controller.repository = Mock()
    controller._emit_desktop_shutdown_ack = Mock()
    return controller


def _run_bounded(action, timeout: float = 3.0):
    outcome: dict[str, Any] = {}

    def run():
        try:
            outcome["value"] = action()
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "operation did not finish within its bound"
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def test_shutdown_closes_repository_while_a_model_deletion_is_active(tmp_path):
    controller = _quiesced_controller(VoiceService(AppConfig(temp_dir=tmp_path), tts_factory=Mock()))

    with model_storage_service._model_deletion_reservation():
        assert controller.cleanup() is True

    controller.repository.close.assert_called_once()
    controller._emit_desktop_shutdown_ack.assert_called_once()
    controller._cleaner_api_limiter.shutdown.assert_called_once()


def test_shutdown_does_not_wait_out_a_running_voice_preview(tmp_path):
    voice = VoiceService(AppConfig(temp_dir=tmp_path), tts_factory=Mock())
    engine = Mock()
    voice._preview_engine = engine
    controller = _quiesced_controller(voice)
    held, release = threading.Event(), threading.Event()

    def synthesizing():
        with voice._runtime_validation_lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=synthesizing, daemon=True)
    holder.start()
    assert held.wait(2)
    try:
        assert _run_bounded(controller.cleanup) is True
    finally:
        release.set()
        holder.join(2)
    controller.repository.close.assert_called_once()
    controller._emit_desktop_shutdown_ack.assert_called_once()
    engine.unload.assert_not_called()


def test_shutdown_unloads_an_idle_preview_engine(tmp_path):
    voice = VoiceService(AppConfig(temp_dir=tmp_path), tts_factory=Mock())
    engine = Mock()
    voice._preview_engine = engine
    controller = _quiesced_controller(voice)

    assert controller.cleanup() is True

    engine.unload.assert_called_once_with(keep_in_cache=False)
    assert voice._preview_engine is None


def test_shutdown_closes_repository_when_model_cleanup_fails(tmp_path):
    controller = _quiesced_controller(VoiceService(AppConfig(temp_dir=tmp_path), tts_factory=Mock()))
    controller._conversion_service.cleanup = Mock(side_effect=RuntimeError("unload failed"))

    assert controller.cleanup() is True

    controller.repository.close.assert_called_once()
    controller._emit_desktop_shutdown_ack.assert_called_once()


# ---- warm TTS session ----

def _warm_pipeline(engine: Any) -> SimpleNamespace:
    config = SimpleNamespace(
        keep_tts_engine_warm=True, tts_engine="kokoro", tts_runtime=None, tts_quantization=None,
        tts_model_name=None, chunk_size=700, tts_idle_timeout_seconds=300.0,
    )
    return SimpleNamespace(config=config, _tts_engine=engine, _selected_parallel_chapters=1, stages=None)


def test_replacing_the_warm_engine_unloads_the_old_one_inside_the_session_fence():
    session = TtsEngineSession()
    observed: list[bool] = []

    class _Retired:
        def unload(self, *, keep_in_cache=True):
            def try_lock():
                acquired = session._lock.acquire(blocking=False)
                observed.append(acquired)
                if acquired:
                    session._lock.release()

            probe = threading.Thread(target=try_lock)
            probe.start()
            probe.join(2)

    session.adopt(_warm_pipeline(_Retired()))
    try:
        session.adopt(_warm_pipeline(Mock()))
        assert observed == [False]
    finally:
        session.cleanup()


# ---- conversion responses and results ----

def test_repair_response_reads_one_consistent_job_snapshot():
    job = ConversionJob(id="repair_3", book_id=3, status=JobStatus.IDLE)
    # A torn object: the attributes disagree with the synchronized snapshot.
    job.read_state = lambda: ("repair_3", 3, JobStatus.FAILED, "boom")
    controller = SimpleNamespace(repair_conversion=lambda **kwargs: job)

    with TestClient(create_app(controller=controller)) as client:
        response = client.post("/api/conversions/repair/3", json={})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "job_id": "repair_3", "client_request_id": None, "status": "failed", "book_id": 3, "error": "boom",
    }


def test_conversion_completion_result_names_the_registered_book(tmp_path, monkeypatch):
    service = _make_service(tmp_path, None)
    monkeypatch.setattr(service, "_update_conversion_library_records", lambda *args: None)
    job = ConversionJob(id="conv", book_id=7, status=JobStatus.RUNNING)
    job.set_result(ConversionResult(success=True, output_path="book.m4b"))
    delivered: list[ConversionResult] = []

    service._handle_conversion_success(
        job=job, source_path=tmp_path / "book.epub", pipeline_result=object(),
        callbacks=ConversionCallbacks(on_complete=delivered.append),
    )

    assert [result.book_id for result in delivered] == [7]
    assert delivered[0].output_path == "book.m4b"
    assert job.result.book_id == 7


def test_durable_sources_are_restricted_once_per_service(tmp_path, monkeypatch):
    service = _make_service(tmp_path, None)
    uploads = Path(service._config.temp_dir) / "web_uploads"
    uploads.mkdir(parents=True)
    sources = Path(service._config.db_path).parent / "sources"
    sources.mkdir(parents=True)
    legacy = sources / "legacy.epub"
    legacy.write_text("old")
    restricted: list[str] = []
    monkeypatch.setattr(conversion_service_module, "owner_only", lambda path: restricted.append(Path(path).name))

    for name in ("first.txt", "second.txt"):
        upload = uploads / name
        upload.write_text("text")
        service._durable_source_path_for_conversion(upload)

    assert restricted.count("legacy.epub") == 1
    assert {"first.txt", "second.txt"} <= set(restricted)


# ---- voice metrics readers ----

def test_metrics_readers_do_not_wait_for_a_model_load(tmp_path):
    voice = VoiceService(AppConfig(temp_dir=tmp_path), tts_factory=Mock())
    held, release = threading.Event(), threading.Event()

    def loading():
        with voice._runtime_validation_lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=loading, daemon=True)
    holder.start()
    assert held.wait(2)
    try:
        assert _run_bounded(voice.metrics_snapshot, timeout=1.0)["preview_generation_count"] == 0
        assert _run_bounded(lambda: voice.runtime_validation_cache_size, timeout=1.0) == 0
    finally:
        release.set()
        holder.join(2)


def test_validation_metrics_stay_consistent_with_the_cache(tmp_path):
    engine = Mock(supported_voices=[SimpleNamespace(id="am_adam")])
    voice = VoiceService(AppConfig(temp_dir=tmp_path), tts_factory=SimpleNamespace(create=lambda *a, **k: engine))

    voice.validate_tts_runtime("kokoro")
    voice.validate_tts_runtime("kokoro")

    metrics = voice.metrics_snapshot()
    assert (metrics["runtime_validation_misses"], metrics["runtime_validation_hits"]) == (1, 1)
    assert voice.runtime_validation_cache_size == 1
    voice.discard_preview_models()
    assert voice.runtime_validation_cache_size == 0


# ---- desktop auth as pure ASGI ----

def _desktop_app(**kwargs):
    return create_app(controller=object(), desktop_mode=True, desktop_api_token="secret-token", **kwargs)


def test_desktop_auth_is_pure_asgi_between_cors_and_the_upload_limit():
    app = _desktop_app(enforce_loopback_host=True)
    names = [middleware.cls.__name__ for middleware in app.user_middleware]

    assert "BaseHTTPMiddleware" not in names
    assert names.index("_LoopbackHostMiddleware") < names.index("CORSMiddleware")
    assert names.index("CORSMiddleware") < names.index("_DesktopAuthMiddleware")
    assert names.index("_DesktopAuthMiddleware") < names.index("_InternalErrorMiddleware")


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
def test_desktop_auth_passes_non_http_scopes_through(scope_type):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(_DesktopAuthMiddleware(inner)({"type": scope_type, "path": "/api/library"}, None, None))
    assert seen == [scope_type]


def test_desktop_auth_reports_a_missing_server_token():
    app = create_app(controller=object(), desktop_mode=True, desktop_api_token=None)
    with TestClient(app) as client:
        response = client.get("/api/library", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Desktop API token is not configured"}


def test_desktop_auth_rejects_and_exempts_exactly_as_before():
    with TestClient(_desktop_app()) as client:
        missing = client.get("/api/does-not-exist")
        wrong = client.get("/api/does-not-exist", headers={"Authorization": "Bearer wrong"})
        authorized = client.get("/api/does-not-exist", headers={"Authorization": "Bearer secret-token"})
        preflight = client.options(
            "/api/library",
            headers={"Origin": "http://tauri.localhost", "Access-Control-Request-Method": "GET"},
        )
        health = client.get("/health")

    assert (missing.status_code, missing.json()) == (401, {"detail": "Unauthorized"})
    assert (wrong.status_code, wrong.json()) == (401, {"detail": "Unauthorized"})
    assert authorized.status_code == 404
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert health.status_code != 401


def test_desktop_auth_exposes_the_audio_token_epoch_to_the_route():
    from modules.web.desktop_auth import mint_desktop_query_token

    token, _ = mint_desktop_query_token("secret-token", "audio:4:epoch-1")
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["state"]["audio_library_epoch"])

    scope = {
        "type": "http", "method": "GET", "path": "/api/library/4/audio", "headers": [],
        "query_string": f"token={token}".encode(), "app": SimpleNamespace(
            state=SimpleNamespace(desktop_mode=True, desktop_api_token="secret-token"),
        ),
    }
    asyncio.run(_DesktopAuthMiddleware(inner)(scope, None, None))
    assert seen == ["epoch-1"]


# ---- loop-driven SSE stream ----

def test_async_stream_delivers_thread_published_events_in_order():
    async def consume():
        broker = EventBroker()
        stream = async_event_stream(broker, heartbeat_seconds=30.0, announce_instance=True)
        assert json.loads((await anext(stream)).removeprefix("data: "))["event_type"] == "stream"
        publisher = threading.Thread(target=lambda: [broker.publish({"event_type": "log", "message": str(i)}) for i in range(5)])
        publisher.start()
        frames = [json.loads((await asyncio.wait_for(anext(stream), 2)).removeprefix("data: ")) for _ in range(5)]
        publisher.join()
        await stream.aclose()
        return broker, frames

    broker, frames = asyncio.run(consume())
    assert [frame["message"] for frame in frames] == ["0", "1", "2", "3", "4"]
    assert [frame["event_sequence"] for frame in frames] == [1, 2, 3, 4, 5]
    assert {frame["backend_instance_id"] for frame in frames} == {broker.instance_id}
    assert broker.subscriber_count == 0


def test_async_stream_keeps_the_bounded_overflow_policy():
    async def consume():
        broker = EventBroker(max_queue_size=2)
        stream = async_event_stream(broker, heartbeat_seconds=30.0, announce_instance=True)
        await anext(stream)
        for message in ("one", "two", "three"):
            broker.publish({"event_type": "log", "message": message})
        frames = [json.loads((await anext(stream)).removeprefix("data: "))["message"] for _ in range(2)]
        await stream.aclose()
        return frames

    assert asyncio.run(consume()) == ["two", "three"]


def test_async_stream_pings_when_idle_and_ends_on_shutdown():
    async def consume():
        broker = EventBroker()
        stream = async_event_stream(broker, heartbeat_seconds=0.01)
        ping = await anext(stream)
        broker.shutdown()
        rest = [frame async for frame in stream]
        return broker, ping, rest

    broker, ping, rest = asyncio.run(consume())
    assert ping == "event: ping\ndata: {}\n\n"
    assert rest == []
    assert broker.subscriber_count == 0


def test_async_stream_unsubscribes_as_soon_as_the_client_task_is_cancelled():
    async def consume():
        broker = EventBroker()
        stream = async_event_stream(broker, heartbeat_seconds=30.0)
        task = asyncio.ensure_future(anext(stream))
        while broker.subscriber_count == 0:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return broker

    assert asyncio.run(consume()).subscriber_count == 0


def test_async_stream_tolerates_publishes_after_its_loop_closed():
    broker = EventBroker()

    async def subscribe_only():
        stream = async_event_stream(broker, heartbeat_seconds=30.0, announce_instance=True)
        await anext(stream)
        return stream

    asyncio.run(subscribe_only())
    broker.publish({"event_type": "log", "message": "late"})
    broker.shutdown()
    assert broker.subscriber_count in {0, 1}


def test_broker_subscribers_without_a_loop_keep_the_queue_contract():
    broker = EventBroker()
    subscriber = broker.subscribe()
    broker.publish({"event_type": "log", "message": "x"})
    broker.shutdown()
    assert subscriber.get_nowait()["message"] == "x"
    assert subscriber.get_nowait() is STREAM_CLOSE_SENTINEL


# ---- setup status reuses the selected profile's checks ----

def test_setup_status_checks_selected_packages_and_assets_once(monkeypatch):
    from tests.test_setup_onboarding import _all_profiles_compatible

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(setup_route.setup_status, "get_setup_status", lambda **kwargs: {
        "configured": True, "profile": "mlx", "model_groups": ["tts-mlx"], "ready": True,
        "restart_required": False, "cleaner_cuda_quantization": "4bit",
        "probe": {"available": True, "checks": [{"id": "tts-mlx", "available": True}]},
    })
    monkeypatch.setattr(setup_route.setup_status, "_probe_profile", lambda groups, service, **kwargs: {
        "available": True, "checks": [{"id": group, "available": True} for group in groups],
    })
    package_checks: list[str] = []
    asset_checks: list[list[str]] = []
    monkeypatch.setattr(setup_route, "_check_selected_profile_packages", lambda profile: package_checks.append(profile) or {
        "ready": True, "missing": [], "incompatible": [],
    })

    def assets(groups, **kwargs):
        asset_checks.append(list(groups))
        return {group: {"ready": group == "tts-mlx", "reason": None if group == "tts-mlx" else "missing"} for group in groups}

    monkeypatch.setattr(hub_cache, "_check_model_group_assets", assets)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(backend_instance_id="b" * 32)))

    status = setup_route._build_uncached_setup_status_response(request, Mock(), Mock())

    assert package_checks == ["mlx"]
    assert asset_checks == [["tts-mlx"], ["cleaner-mlx", "cleaner-mlx-small"]]
    installed = {group["id"]: group for group in status["installed_runtime"]["model_groups"]}
    assert [installed[group]["assets_ready"] for group in ("tts-mlx", "cleaner-mlx", "cleaner-mlx-small")] == [True, False, False]
    assert status["state"] == "ready"


def test_installed_runtime_detection_without_a_selection_checks_everything(monkeypatch):
    from tests.test_setup_onboarding import _all_profiles_compatible

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(setup_route.setup_status, "_probe_profile", lambda groups, service, **kwargs: {
        "available": True, "checks": [{"id": group, "available": True} for group in groups],
    })
    package_checks: list[str] = []
    monkeypatch.setattr(setup_route, "_check_selected_profile_packages", lambda profile: package_checks.append(profile) or {"ready": profile == "mlx"})
    monkeypatch.setattr(hub_cache, "_check_model_group_assets", lambda groups, **kwargs: {
        group: {"ready": True, "reason": None} for group in groups
    })

    detected = setup_route._detect_installed_runtime(
        Mock(), Mock(), known_packages={"ready": True}, known_assets={"tts-mlx": {"ready": False, "reason": "x"}},
    )

    # Without a preferred profile, nothing computed for one selection applies.
    assert package_checks[-1] == "mlx"
    assert detected["profile"] == "mlx"
    assert all(group["assets_ready"] for group in detected["model_groups"])



# ---- setup status runs each runtime probe once per build ----

class _ScriptCountingProbes(RuntimeCapabilityService):
    """Real probe service whose subprocess runs are recorded instead of spawned."""

    def __init__(self, *, timed_out: bool = False) -> None:
        super().__init__(cache_ttl_seconds=60.0)
        self.runs: list[str] = []
        self.timed_out = timed_out

    def _run_probe(self, script: str, timeout_seconds: float) -> dict[str, Any]:
        kind = (
            "combined" if "results = {}" in script
            else "cleaner-cuda" if "CLEANER_CUDA_QUANTIZATION" in script
            else "tts-package" if script == self._TTS_PACKAGE_PROBE
            else "tts-mlx" if script == self._TTS_PROBE
            else "cleaner-mlx"
        )
        self.runs.append(kind)
        if kind == "combined" and not self.timed_out:
            return {"available": True, "latency_ms": 1.0, "error": None, "cache_hit": False,
                    "results": {"tts": {"available": True}, "cleaner": {"available": True}}}
        if self.timed_out:
            return {"available": False, "latency_ms": 1.0, "error": "slow", "timed_out": True, "cache_hit": False}
        return {"available": True, "latency_ms": 1.0, "error": None, "cache_hit": False}


class _SeparateProbes(_ScriptCountingProbes):
    """An adapter that keeps its own TTS probe and so never shares a subprocess."""

    def probe_package_tts_runtime(self) -> dict[str, Any]:
        return super().probe_package_tts_runtime()


def _status_build(monkeypatch, service, *, profile, groups, packages_ready=True, legacy=False):
    from modules.runtime_setup import state as setup_state
    from tests.test_setup_onboarding import _all_profiles_compatible

    _all_profiles_compatible(monkeypatch)
    setup_state.write_setup_state(
        setup_state.default_state_path(), profile_id=profile, model_groups=groups,
        restart_required=False, cleaner_cuda_quantization="8bit",
    )
    package_checks: list[str] = []
    monkeypatch.setattr(setup_route, "_check_selected_profile_packages", lambda profile_id: package_checks.append(profile_id) or {
        "ready": packages_ready, "missing": [], "incompatible": [],
    })
    monkeypatch.setattr(hub_cache, "_check_model_group_assets", lambda ids, **kwargs: {
        group: {"ready": True, "reason": None} for group in ids
    })
    if legacy:
        # Probe exactly as before: each consumer straight against the service.
        monkeypatch.setattr(setup_route, "_StatusBuildProbes", lambda probes: probes)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(backend_instance_id="b" * 32)))
    return setup_route._build_uncached_setup_status_response(request, service, object()), package_checks


_STATUS_CASES = [
    ("cuda", ["tts-cpu-cuda"], True, False),
    ("cuda", ["tts-cpu-cuda"], False, False),
    ("cuda", ["tts-cpu-cuda", "cleaner-cuda"], True, False),
    ("cuda", ["tts-cpu-cuda"], True, True),
    ("mlx", ["tts-mlx"], True, False),
    ("mlx", ["tts-mlx", "cleaner-mlx"], True, False),
    ("mlx", ["tts-mlx"], True, True),
]


@pytest.mark.parametrize(("profile", "groups", "packages_ready", "timed_out"), _STATUS_CASES)
@pytest.mark.parametrize("probes", [_ScriptCountingProbes, _SeparateProbes])
def test_setup_status_runs_each_probe_once_with_an_unchanged_payload(
    monkeypatch, probes, profile, groups, packages_ready, timed_out,
):
    with monkeypatch.context() as legacy_patch:
        before_service = probes(timed_out=timed_out)
        before, _ = _status_build(
            legacy_patch, before_service, profile=profile, groups=groups, packages_ready=packages_ready, legacy=True,
        )
    service = probes(timed_out=timed_out)
    after, package_checks = _status_build(
        monkeypatch, service, profile=profile, groups=groups, packages_ready=packages_ready,
    )

    assert after == before
    assert package_checks == [profile]
    # No probe executes twice, and none runs that did not run before.
    assert len(service.runs) == len(set(service.runs))
    assert set(service.runs) <= set(before_service.runs)


def test_setup_status_reuses_the_selected_probe_for_installed_runtime_detection(monkeypatch):
    service = _ScriptCountingProbes()

    status, _ = _status_build(monkeypatch, service, profile="cuda", groups=["tts-cpu-cuda", "cleaner-cuda"])

    assert service.runs == ["combined"]
    assert status["state"] == "ready"
    assert [group["runtime_ready"] for group in status["installed_runtime"]["model_groups"]] == [True, True, True]


@pytest.mark.parametrize(("profile", "groups", "runs"), [
    ("mlx", ["tts-mlx"], ["tts-mlx", "cleaner-mlx"]),
    ("cuda", ["tts-cpu-cuda"], ["tts-package", "cleaner-cuda"]),
])
def test_setup_status_never_reruns_a_timed_out_probe_in_one_build(monkeypatch, profile, groups, runs):
    service = _ScriptCountingProbes(timed_out=True)

    status, _ = _status_build(monkeypatch, service, profile=profile, groups=groups)

    assert service.runs == runs
    assert status["state"] == "checking"


def test_setup_status_keeps_separate_probes_for_custom_services(monkeypatch):
    service = _SeparateProbes()

    _status_build(monkeypatch, service, profile="cuda", groups=["tts-cpu-cuda"])

    assert service.runs == ["tts-package", "cleaner-cuda"]


# ---- staged model recovery evicts only the recovered groups ----

def _write_stage(cache: Path, group: str, name: str, **extra: Any) -> Path:
    model_id = resolve_model_groups((group,))[0]
    stage = cache / (".orpheus-delete-" + name * 32)
    (stage / "snapshots").mkdir(parents=True)
    stage.with_name(stage.name + ".json").write_text(json.dumps({
        "original": "models--" + model_id.replace("/", "--"), "staged": stage.name, "settings_committed": True, **extra,
    }))
    return stage


@pytest.fixture
def cache_clears(monkeypatch):
    from modules.tts import cleaner_cuda, cleaner_mlx

    cleared: list[str] = []
    for name, module, attribute in (
        ("cuda-cleaner", cleaner_cuda, "clear_cleaner_cache"),
        ("mlx-cleaner", cleaner_mlx, "clear_cleaner_cache"),
        ("tts", model_cache, "clear_model_cache"),
    ):
        original = getattr(module, attribute)
        monkeypatch.setattr(module, attribute, lambda name=name, original=original: cleared.append(name) or original())
    return cleared


def test_recovering_a_cleaner_deletion_keeps_the_voice_preview_loaded(storage, cache_clears):
    storage.service.generate_voice_preview("am_adam")
    engine = storage.service._preview_engine
    stage = _write_stage(storage.repo.parent, "cleaner-mlx", "c")

    response = storage.client.get("/api/setup/models")

    assert response.status_code == 200, response.text
    assert not stage.exists()
    assert storage.service._preview_engine is engine
    assert sorted(set(cache_clears)) == ["cuda-cleaner", "mlx-cleaner"]


def test_recovering_a_voice_deletion_keeps_cleaner_caches(storage, cache_clears):
    storage.service.generate_voice_preview("am_adam")
    stage = _write_stage(storage.repo.parent, "tts-mlx", "d")

    response = storage.client.get("/api/setup/models")

    assert response.status_code == 200, response.text
    assert not stage.exists()
    assert storage.service._preview_engine is None
    assert set(cache_clears) == {"tts"}


def test_recovering_a_runtime_uninstall_still_clears_every_model(storage, cache_clears):
    storage.service.generate_voice_preview("am_adam")
    stage = _write_stage(
        storage.repo.parent, "cleaner-mlx", "e",
        uninstall_runtime=True, installation_id="f" * 32, previous_installation_id=None,
    )

    response = storage.client.get("/api/setup/models")

    assert response.status_code == 200, response.text
    assert not stage.exists()
    assert storage.service._preview_engine is None
    assert set(cache_clears) == {"cuda-cleaner", "mlx-cleaner", "tts"}
    assert storage.client.app.state.setup_reset_id == "f" * 32


# ---- idle event streams release their subscription on disconnect ----

def test_idle_event_stream_disconnect_unsubscribes_without_worker_threads():
    import socket
    import time

    uvicorn = pytest.importorskip("uvicorn")
    token = "t" * 32
    broker = EventBroker()
    try:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        probe.close()
    except OSError:
        pytest.skip("cannot bind a loopback port")
    server = uvicorn.Server(uvicorn.Config(
        create_app(controller=object(), broker=broker, desktop_mode=True, desktop_api_token=token),
        host="127.0.0.1", port=0, log_level="warning", access_log=False, lifespan="off",
    ))
    thread = threading.Thread(target=server.run, name="sse-test-server", daemon=True)
    thread.start()
    clients: list[socket.socket] = []
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                pytest.skip("loopback server did not start")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        baseline_threads = threading.active_count()
        for _ in range(3):
            client = socket.create_connection(("127.0.0.1", port), timeout=5)
            clients.append(client)
            client.sendall(
                f"GET /api/events/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {token}\r\n\r\n".encode()
            )
            received = b""
            while b"backend_instance_id" not in received:
                chunk = client.recv(1024)
                assert chunk, received
                received += chunk
        assert broker.subscriber_count == 3
        # Streams run on the event loop, not one pinned worker thread each.
        assert threading.active_count() <= baseline_threads + 1

        started = time.monotonic()
        for client in clients:
            client.close()
        # Nothing is published: the disconnect alone must end each stream.
        while broker.subscriber_count and time.monotonic() - started < 2:
            time.sleep(0.01)
        assert broker.subscriber_count == 0
        assert time.monotonic() - started < 1.5
    finally:
        for client in clients:
            client.close()
        broker.shutdown()
        server.should_exit = True
        thread.join(10)
    assert not thread.is_alive()


def test_runtime_uninstall_refusal_invalidates_model_receipts(storage, monkeypatch):
    from modules.runtime_setup.receipts import ModelReceiptVerifier

    invalidations: list[str] = []
    monkeypatch.setattr(RuntimeSetupStatusCache, "invalidate", lambda self: invalidations.append("status"))
    monkeypatch.setattr(ModelReceiptVerifier, "invalidate", lambda self: invalidations.append("receipts"))

    with _paused_call(monkeypatch, _Engine, "synthesize", lambda: storage.service.generate_voice_preview("am_adam")):
        response = storage.client.delete("/api/setup/runtime")

    assert response.status_code == 409, response.text
    assert invalidations == ["receipts", "status"]
    assert storage.repo.exists()
