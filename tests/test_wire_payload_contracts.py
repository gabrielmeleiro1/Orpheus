"""Compare real desktop wire payloads with the shared, committed JSON fixtures.

Regenerate intentionally with::

    ORPHEUS_REGENERATE_WIRE_FIXTURES=1 python -m pytest tests/test_wire_payload_contracts.py --no-cov

Then review the JSON diff and run desktop typecheck and wireFixtures.test.ts.
Only volatile scalar values are normalized; keys, nulls, and JSON types survive.
The real app/controller/repository and serializers run against isolated storage.
Native runtime probes and conversion execution are replaced at their boundaries.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).resolve().parents[1] / "desktop/src/test/wire-fixtures"


def _normalize(value, root: Path, key: str = ""):
    if isinstance(value, dict):
        return {name: _normalize(item, root, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, root, key) for item in value]
    if isinstance(value, str):
        if key in {"timestamp", "created_at"}:
            return "2026-01-01T00:00:00+00:00"
        if key == "label" and value.startswith(("Backup from ", "Safety copy from ")):
            # Backup labels embed the local creation time.
            return "<backup label>"
        if key in {"library_id", "library_epoch", "backend_instance_id", "instance_id", "setup_reset_id"}:
            return f"<{key}>"
        return value.replace(str(root), "<workspace>").replace("\\", "/")
    # SQLite file sizes differ by SQLite version and page allocation.
    if key == "size" and type(value) in (int, float):
        return 4096
    return value


def _json_shape(value):
    if isinstance(value, dict):
        return {key: _json_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_shape(item) for item in value]
    if type(value) in (int, float):
        return "number"
    return type(value).__name__


def _assert_fixture(name, payload, root):
    actual = _normalize(payload, root)
    path = FIXTURES / f"{name}.json"
    if os.environ.get("ORPHEUS_REGENERATE_WIRE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert path.exists(), f"Missing {path.name}; see the regeneration command in this module."
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert _json_shape(actual) == _json_shape(expected), f"{name}: JSON keys or types changed"
    assert actual == expected, f"{name}: normalized response changed"


@pytest.fixture
def wire_client(tmp_path, monkeypatch, registry_snapshot):
    from modules.app.config import AppConfig
    from modules.app.controller import AppController
    from modules.app.services.runtime_capability_service import RuntimeCapabilityService
    from modules.pipeline import worker_recommendation
    from modules.pipeline.memory_policy import MemorySnapshot
    from modules.runtime_profiles import RuntimeProfile
    from modules.storage.models import BookCreate, ChapterCreate, PlaybackStatus, SourceType
    from modules.tts.factory import TTSEngineFactory
    from modules.tts.runtime_spec import TTSRuntimeSpec
    from modules.web.routes import setup
    from modules.web.server import create_app

    monkeypatch.chdir(tmp_path)
    # Ignore host configuration, including model choices and storage overrides.
    for name in tuple(os.environ):
        if name.startswith(("AUDIOBOOK_", "TTS_", "CLEANER_", "ORPHEUS_DESKTOP_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    monkeypatch.delenv("WORKER_POOL_REUSE_ENABLED", raising=False)
    monkeypatch.setattr(TTSRuntimeSpec, "mlx_supported_on_current_platform", staticmethod(lambda: False))
    monkeypatch.setattr(TTSRuntimeSpec, "cuda_available_on_current_platform", staticmethod(lambda: False))
    compatible = RuntimeProfile.is_compatible
    reason = RuntimeProfile.incompatibility_reason
    monkeypatch.setattr(RuntimeProfile, "is_compatible", lambda self, **kw: compatible(
        self, system="Linux", machine="x86_64", os_version="",
    ))
    monkeypatch.setattr(RuntimeProfile, "incompatibility_reason", lambda self, **kw: reason(
        self, system="Linux", machine="x86_64", os_version="",
    ))
    monkeypatch.setattr(setup, "recommended_profile_id", lambda: "cuda")
    monkeypatch.setattr(setup, "_check_selected_profile_packages", lambda profile: {
        "ready": False, "missing": [{"name": "kokoro", "expected": "==0.9.4", "installed": None}],
        "incompatible": [],
    })
    monkeypatch.setattr(RuntimeCapabilityService, "_run_probe", lambda *args: {
        "available": False, "latency_ms": 1.0, "error": "Runtime unavailable", "cache_hit": False,
    })
    monkeypatch.setattr(worker_recommendation, "sample_memory_snapshot", lambda device_memory_policy=None: MemorySnapshot(
        total_bytes=32 * 1024**3, available_bytes=16 * 1024**3,
    ))
    # Keep factory metadata independent of earlier tests registering engines.
    monkeypatch.setattr(TTSEngineFactory, "_strategies", {})
    monkeypatch.setattr(TTSEngineFactory, "_config_types", {})
    monkeypatch.setattr(TTSEngineFactory, "_optional_loaded", False)
    TTSEngineFactory._load_optional_strategies()
    controller = AppController(AppConfig(
        data_dir=tmp_path / "data", db_path=tmp_path / "data/library.db",
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        tts_engine="kokoro", tts_quantization="bf16", cleaner_backend="api",
        worker_pool_reuse_enabled=False,
    ))
    probe = controller.runtime_capability_service
    # Seed both the MLX and the PyTorch-runtime checks: which one the snapshot
    # reports depends on this machine's primary runtime.
    for kind in {*probe._snapshot_probe_kinds("mlx"), *probe._snapshot_probe_kinds("cpu")}:
        probe._store_result(kind, probe._run_probe("", 0))
    source = tmp_path / "source.txt"
    source.write_text("A small book.", encoding="utf-8")
    audio = controller.config.output_dir / "book.m4b"
    audio.write_bytes(b"audio")
    book, _ = controller.repository.create_book_with_chapters(
        BookCreate(title="A small book", author="An author", source_path=str(source),
                   source_type=SourceType.TXT, total_chapters=3, playback_path=str(audio),
                   playback_status=PlaybackStatus.PACKAGED, tags="fiction", favorite=True),
        [ChapterCreate(book_id=0, chapter_number=1, title="Opening", duration_ms=1250,
                       mp3_path=str(audio), packaged_start_ms=0),
         ChapterCreate(book_id=0, chapter_number=2, title="Ending", duration_ms=2500,
                       mp3_path=None, packaged_start_ms=1280),
         ChapterCreate(book_id=0, chapter_number=3, title="Unfinished")],
    )
    controller.repository.create_book(BookCreate(
        title="Unconverted", source_path=str(tmp_path / "missing.txt"), source_type=SourceType.TXT,
    ))
    # Equal timestamps keep pagination on the stable ID tie-breaker.
    with controller.repository._connection() as conn:
        conn.execute(
            "UPDATE books SET created_at = ?, updated_at = ?",
            ("2026-01-01 00:00:00", "2026-01-01 00:00:00"),
        )
    app = create_app(controller=controller)
    try:
        with TestClient(app) as client:
            yield client, controller, book.id
    finally:
        controller.cleanup()
        controller.repository.close()


def test_http_responses_match_committed_wire_payloads(wire_client, tmp_path, monkeypatch):
    from modules.app.controller import ConversionJob, JobStatus
    from modules.runtime_setup import state

    client, controller, book_id = wire_client

    def capture(name, url, *, method="get", **kwargs):
        response = getattr(client, method)(url, **kwargs)
        assert response.status_code == 200, response.text
        _assert_fixture(name, response.json(), tmp_path)
        return response.json()

    capture("health", "/health")
    capture("library-list", "/api/library?limit=1&offset=1")
    capture("book-detail", f"/api/library/{book_id}")
    capture("book-detail-empty", f"/api/library/{book_id + 1}")
    capture("active-idle", "/api/conversions/active")
    capture("system-capabilities", "/api/system/capabilities")
    capture("options-models", "/api/options/models")
    capture("voices", "/api/options/voices?engine=kokoro")
    capture("setup-status", "/api/setup/status")
    capture("setup-models", "/api/setup/models")
    capture("model-download-idle", "/api/setup/models/download")
    response = client.post("/api/maintenance/backup")
    assert response.status_code == 200, response.text
    backup = Path(response.json()["backup_path"])
    backup.rename(backup.with_name("library_20260101_000000.db"))
    capture("maintenance-backups", "/api/maintenance/backups")

    state.write_setup_state(state.default_state_path(), profile_id="cuda",
                            model_groups=("tts-cpu-cuda",),
                            restart_required=False, cleaner_cuda_quantization="4bit")
    client.app.state.runtime_setup_status_cache.invalidate()
    capture("setup-status-configured", "/api/setup/status")
    response = client.delete("/api/setup/runtime")
    assert response.status_code == 200, response.text
    capture("setup-status-reset", "/api/setup/status")

    job = ConversionJob(id="job-1", book_id=None, status=JobStatus.RUNNING,
                        title="A small book", author=None, client_request_id="desktop-request_123")
    # Starting a worker is outside the wire contract; keep the real job serializer.
    monkeypatch.setattr(controller._conversion_service, "start_conversion", lambda **kwargs: job)
    monkeypatch.setattr(controller._conversion_service, "get_active_job", lambda: job)
    capture("conversion-start", "/api/conversions", method="post", json={
        "source_path": str(tmp_path / "source.txt"), "conversion_mode": "audiobook",
        "client_request_id": "desktop-request_123",
    })
    capture("active-running", "/api/conversions/active")
    job.complete(book_id)
    capture("active-completed", "/api/conversions/active")
    job.mark_cancelled("Conversion cancelled")
    capture("active-cancelled", "/api/conversions/active")


def test_download_status_matches_running_and_complete_wire_payloads(wire_client, tmp_path, monkeypatch):
    client, _, _ = wire_client
    assert client.get("/api/setup/models/download").status_code == 200
    service = client.app.state.model_download_service
    entered = threading.Event()
    release = threading.Event()

    def download(model_id):
        entered.set()
        assert release.wait(5), f"Download was not released: {model_id}"

    monkeypatch.setattr(service, "_downloader", download)
    try:
        response = client.post("/api/setup/models/download", json={
            "profile": "cuda", "model_groups": ["tts-cpu-cuda"], "configure_runtime": False,
        })
        assert response.status_code == 200, response.text
        assert entered.wait(2)
        worker = service._thread
        response = client.get("/api/setup/models/download")
        assert response.status_code == 200, response.text
        _assert_fixture("model-download-running", response.json(), tmp_path)
        release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        response = client.get("/api/setup/models/download")
        assert response.status_code == 200, response.text
        _assert_fixture("model-download-complete", response.json(), tmp_path)
    finally:
        release.set()
        assert service.shutdown(timeout_seconds=2)


def test_sse_serializers_match_committed_wire_payloads(tmp_path):
    from modules.app.events import JobState, make_log_event, make_progress_event, make_state_event
    from modules.web.events import EventBroker, event_stream

    broker = EventBroker()
    stream = event_stream(broker, announce_instance=True)

    def capture(name):
        frame = next(stream)
        assert frame.startswith("data: ") and frame.endswith("\n\n")
        _assert_fixture(name, json.loads(frame[6:]), tmp_path)

    try:
        capture("sse-stream")
        for name, event in [
            ("sse-progress", make_progress_event("synthesis", 0.5, "Reading", eta=12,
                chapter_current=1, chapter_total=2, chunk_current=2, chunk_total=4,
                performance={"pipeline_mode": "parallel", "active_workers": 1, "max_workers": 2},
                job_id="job-1")),
            ("sse-log", make_log_event("Starting")),
            ("sse-state", make_state_event(JobState.COMPLETED, "job-1", "Complete")),
        ]:
            broker.publish(event)
            capture(name)
    finally:
        broker.shutdown()
        stream.close()


def test_submission_id_is_reserved_before_conversion_execution(wire_client, tmp_path, monkeypatch):
    from modules.app.events import JobState, make_state_event

    client, controller, _ = wire_client
    service = controller._conversion_service
    subscriber = client.app.state.event_broker.subscribe()

    def start_reserved_job(**kwargs):
        job = kwargs["job"]
        kwargs["callbacks"].on_event(make_state_event(JobState.RUNNING, job.id, "Started"))
        return job

    monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: None)
    monkeypatch.setattr(service, "_start_pipeline_job", start_reserved_job)
    try:
        response = client.post("/api/conversions", json={
            "source_path": str(tmp_path / "source.txt"), "client_request_id": "desktop-request_123",
        })
        assert response.status_code == 200, response.text
        assert getattr(controller.get_active_job(), "client_request_id", None) == "desktop-request_123"
        assert response.json()["client_request_id"] == "desktop-request_123"
        assert client.get("/api/conversions/active").json()["job"]["client_request_id"] == "desktop-request_123"
        assert subscriber.get_nowait()["client_request_id"] == "desktop-request_123"
    finally:
        client.app.state.event_broker.unsubscribe(subscriber)
        job = controller.get_active_job()
        if job is not None:
            service._job_coordinator.release_reservation(job)


@pytest.mark.parametrize("request_id", ["", "space id", "../id", "a" * 65, "ümlaut", 123])
def test_start_rejects_invalid_submission_id_before_reserving_a_job(wire_client, tmp_path, request_id):
    client, controller, _ = wire_client
    response = client.post("/api/conversions", json={
        "source_path": str(tmp_path / "source.txt"), "client_request_id": request_id,
    })
    assert response.status_code == 422, response.text
    assert controller.get_active_job() is None
