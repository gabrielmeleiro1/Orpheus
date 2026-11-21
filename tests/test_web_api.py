"""Script-style tests for the desktop backend API surface."""

import sys
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _make_client(tmp_path):
    from fastapi.testclient import TestClient
    from modules.app.config import AppConfig
    from modules.app.controller import AppController
    from modules.web.server import create_app

    config = AppConfig(
        data_dir=tmp_path / "data", output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp", db_path=tmp_path / "data" / "audiobooks.db",
    )
    return TestClient(create_app(controller_factory=lambda: AppController(config)))


def _make_client_with_controller(controller):
    from fastapi.testclient import TestClient
    from modules.web.server import create_app

    return TestClient(create_app(controller=controller))


def test_active_conversion_route_serializes_job_metadata():
    job = SimpleNamespace(
        id="job-1",
        book_id=None,
        status=SimpleNamespace(value="running"),
        error=None,
        title="Reloaded title",
        author="Reloaded author",
        is_active=lambda: True,
    )
    controller = SimpleNamespace(
        config=SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output")),
        get_active_job=lambda: job,
    )

    response = _make_client_with_controller(controller).get("/api/conversions/active")

    assert response.status_code == 200
    assert response.json()["job"] == {
        "id": "job-1",
        "client_request_id": None,
        "book_id": None,
        "status": "running",
        "error": None,
        "title": "Reloaded title",
        "author": "Reloaded author",
        "repair": False,
    }


def test_active_conversion_route_says_whether_the_job_repairs_a_book(tmp_path):
    from modules.app.controller import ConversionJob, JobStatus
    from modules.app.services.job_coordinator import JobCoordinator

    coordinator = JobCoordinator(job_cls=ConversionJob, status_enum=JobStatus)
    controller = SimpleNamespace(
        config=SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output")),
        get_active_job=lambda: coordinator._active_job,
    )
    client = _make_client_with_controller(controller)

    conversion = coordinator.reserve_conversion(tmp_path / "book.md", None, None)
    assert client.get("/api/conversions/active").json()["job"]["repair"] is False
    coordinator.release_reservation(conversion)
    coordinator.reserve_repair(7, SimpleNamespace(title="Harbor Lights", author=None))
    assert client.get("/api/conversions/active").json()["job"]["repair"] is True



def test_openai_model_list_route_fetches_models_with_pasted_key(monkeypatch):
    from modules.tts import cleaner_api as cleaner_api_module

    captured = {"url": None, "authorization": None}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            _ = (exc_type, exc_val, exc_tb)
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {"id": "gpt-5-nano"},
                        {"id": "gpt-4o-mini"},
                        {"id": ""},
                        {"object": "model"},
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request_obj, timeout=0, context=None):
        _ = (timeout, context)
        captured["url"] = request_obj.full_url
        captured["authorization"] = request_obj.get_header("Authorization")
        return _FakeResponse()

    monkeypatch.setattr(cleaner_api_module.urlrequest, "urlopen", fake_urlopen)

    from modules.app.controller import AppController

    controller = SimpleNamespace(
        list_openai_models=lambda *, api_key: AppController.list_openai_models(None, api_key)
    )
    client = _make_client_with_controller(controller)
    response = client.post("/api/options/openai/models", json={"api_key": "sk-test"})

    assert response.status_code == 200
    assert response.json()["provider"] == "openai"
    assert response.json()["base_url"] == "https://api.openai.com/v1"
    assert response.json()["models"] == ["gpt-5-nano", "gpt-4o-mini"]
    assert captured["url"] == "https://api.openai.com/v1/models"
    assert captured["authorization"] == "Bearer sk-test"


def test_repair_route_forwards_empty_payload_for_backend_runtime_resolution():
    class _Status:
        value = "running"

    class _Job:
        id = "job-1"
        book_id = None
        status = _Status()
        error = None

    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))
            self.repair_kwargs = None

        def repair_conversion(self, **kwargs):  # noqa: ANN003
            self.repair_kwargs = kwargs
            job = _Job()
            job.book_id = kwargs["book_id"]
            return job

    controller = _Controller()
    client = _make_client_with_controller(controller)

    response = client.post("/api/conversions/repair/42", json={})

    assert response.status_code == 200
    assert controller.repair_kwargs["book_id"] == 42
    # Empty payload must forward an empty fields_set so the service can resolve
    # persisted / app-config narration settings (not request-model defaults).
    assert controller.repair_kwargs["runtime_fields_set"] == set()
    assert controller.repair_kwargs["runtime_options"].parallel_worker_mode is None


def test_start_conversion_rejects_overlong_title_before_controller_call():
    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))
            self.called = False

        def start_conversion(self, **kwargs):  # noqa: ANN003
            self.called = True
            return SimpleNamespace(
                id="job-1", book_id=None, status=SimpleNamespace(value="running"), error=None
            )

    controller = _Controller()
    client = _make_client_with_controller(controller)

    response = client.post(
        "/api/conversions",
        json={
            "source_path": "/tmp/book.pdf",
            "title": "A" * 300,
        },
    )

    assert response.status_code == 422
    assert controller.called is False


@pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_api_numbers_return_json_safe_422(non_finite):
    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(
                temp_dir=Path("temp"), output_dir=Path("output")
            )
            self.called = False

        def start_conversion(self, **kwargs):  # noqa: ANN003
            self.called = True
            raise AssertionError("validation must reject before controller call")

        def update_book_playback_position(self, book_id, position_sec):
            _ = (book_id, position_sec)
            self.called = True
            raise AssertionError("validation must reject before controller call")

    controller = _Controller()
    client = _make_client_with_controller(controller)

    conversion = client.post(
        "/api/conversions",
        content=(
            '{"source_path":"/tmp/book.pdf","runtime_options":'
            f'{{"speed":{non_finite}}}}}'
        ),
        headers={"content-type": "application/json"},
    )
    playback = client.put(
        "/api/library/1/playback-position",
        content=f'{{"position_sec":{non_finite}}}',
        headers={"content-type": "application/json"},
    )

    assert conversion.status_code == 422
    assert playback.status_code == 422
    assert controller.called is False
    json.dumps(conversion.json(), allow_nan=False)
    json.dumps(playback.json(), allow_nan=False)


@pytest.mark.parametrize("speed", [0.49, 3.01, -1.0, 1e300])
def test_conversion_speed_bounds_reject_extremes_before_controller_call(speed):
    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(
                temp_dir=Path("temp"), output_dir=Path("output")
            )
            self.called = False

        def start_conversion(self, **kwargs):  # noqa: ANN003
            self.called = True
            raise AssertionError("validation must reject before controller call")

    controller = _Controller()
    response = _make_client_with_controller(controller).post(
        "/api/conversions",
        json={
            "source_path": "/tmp/book.pdf",
            "runtime_options": {"speed": speed},
        },
    )

    assert response.status_code == 422
    assert controller.called is False


def test_options_routes_translate_controller_errors():
    class _Controller:
        def list_openai_models(self, *, api_key):
            _ = api_key
            raise RuntimeError("provider rejected key")

    client = _make_client_with_controller(_Controller())

    openai = client.post(
        "/api/options/openai/models", json={"api_key": "sk-invalid"}
    )

    assert openai.status_code == 502
    assert openai.json() == {"detail": "provider rejected key"}


def test_conversion_routes_forward_full_frontend_selection_payloads_on_start_and_repair():
    class _Status:
        value = "running"

    class _Job:
        id = "job-1"
        book_id = None
        status = _Status()
        error = None

    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))
            self.start_kwargs = None
            self.repair_kwargs = None

        def start_conversion(self, **kwargs):  # noqa: ANN003
            self.start_kwargs = kwargs
            return _Job()

        def repair_conversion(self, **kwargs):  # noqa: ANN003
            self.repair_kwargs = kwargs
            job = _Job()
            job.book_id = kwargs["book_id"]
            return job

    controller = _Controller()
    client = _make_client_with_controller(controller)
    frontend_payload = {
        "source_path": "/tmp/frontend-contract.epub",
        "conversion_mode": "document_audio",
        "title": "Frontend Contract Book",
        "author": "Contract Author",
        "voice": "af_bella",
        "speed": 1.35,
        "tts_engine": "kokoro",
        "tts_quantization": "bf16",
        "cleaner_backend": "api",
        "cleaner_model": None,
        "cleaner_api_model": "gpt-5-nano",
        "cleaner_api_key": "sk-contract",
        "tts_options": {"quantization": "bf16"},
        "parallel_worker_mode": "manual",
        "enable_parallel": True,
        "max_parallel_chapters": 3,
    }

    start = client.post("/api/conversions", json=frontend_payload)
    repair = client.post(
        "/api/conversions/repair/42",
        json={k: v for k, v in frontend_payload.items() if k != "source_path"},
    )

    assert start.status_code == 200
    assert repair.status_code == 200
    assert controller.start_kwargs["source_path"] == Path(frontend_payload["source_path"])
    assert controller.start_kwargs["conversion_mode"] == frontend_payload["conversion_mode"]
    assert controller.start_kwargs["title"] == frontend_payload["title"]
    assert controller.start_kwargs["author"] == frontend_payload["author"]
    for key, value in frontend_payload.items():
        if key in {"source_path", "conversion_mode", "title", "author"}:
            continue
        assert getattr(controller.start_kwargs["runtime_options"], key) == value
        assert getattr(controller.repair_kwargs["runtime_options"], key) == value


def test_conversion_routes_forward_grouped_runtime_options_on_start_and_repair():
    class _Status:
        value = "running"

    class _Job:
        id = "job-1"
        book_id = None
        status = _Status()
        error = None

    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))
            self.start_kwargs = None
            self.repair_kwargs = None

        def start_conversion(self, **kwargs):  # noqa: ANN003
            self.start_kwargs = kwargs
            return _Job()

        def repair_conversion(self, **kwargs):  # noqa: ANN003
            self.repair_kwargs = kwargs
            job = _Job()
            job.book_id = kwargs["book_id"]
            return job

    controller = _Controller()
    client = _make_client_with_controller(controller)
    runtime_options = {
        "tts_engine": "kokoro",
        "tts_quantization": "bf16",
        "cleaner_backend": "api",
        "cleaner_model": None,
        "cleaner_api_model": "gpt-5-nano",
        "cleaner_api_key": "sk-contract",
        "tts_options": {"quantization": "bf16"},
        "parallel_worker_mode": "manual",
        "enable_parallel": True,
        "max_parallel_chapters": 3,
    }

    start = client.post(
        "/api/conversions",
        json={
            "source_path": "/tmp/frontend-contract.epub",
            "conversion_mode": "document_audio",
            "title": "Frontend Contract Book",
            "author": "Contract Author",
            "voice": "af_bella",
            "speed": 1.35,
            "runtime_options": runtime_options,
        },
    )
    repair = client.post(
        "/api/conversions/repair/42",
        json={
            "voice": "af_bella",
            "speed": 1.35,
            "runtime_options": runtime_options,
        },
    )

    assert start.status_code == 200
    assert repair.status_code == 200
    assert controller.start_kwargs["runtime_options"].tts_engine == "kokoro"
    assert controller.start_kwargs["runtime_options"].tts_options == {"quantization": "bf16"}
    assert controller.start_kwargs["runtime_options"].parallel_worker_mode == "manual"
    assert controller.start_kwargs["runtime_options"].max_parallel_chapters == 3
    assert controller.repair_kwargs["runtime_options"].tts_engine == "kokoro"
    assert controller.repair_kwargs["runtime_options"].enable_parallel is True


def test_conversion_routes_do_not_require_flat_voice_and_speed_with_grouped_runtime_options():
    class _Status:
        value = "running"

    class _Job:
        id = "job-1"
        book_id = None
        status = _Status()
        error = None

    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))

        def start_conversion(
            self,
            *,
            source_path,
            voice="am_adam",
            speed=1.0,
            runtime_options,
            **kwargs,
        ):  # noqa: ANN001, ANN003
            _ = (source_path, voice, speed, runtime_options, kwargs)
            return _Job()

        def repair_conversion(
            self,
            *,
            book_id,
            voice="am_adam",
            speed=1.0,
            runtime_options,
            **kwargs,
        ):  # noqa: ANN001, ANN003
            _ = (voice, speed, runtime_options, kwargs)
            job = _Job()
            job.book_id = book_id
            return job

    client = _make_client_with_controller(_Controller())

    start = client.post(
        "/api/conversions",
        json={
            "source_path": "/tmp/book.pdf",
            "runtime_options": {"voice": "af_bella", "speed": 1.25, "tts_engine": "kokoro"},
        },
    )
    repair = client.post(
        "/api/conversions/repair/42",
        json={"runtime_options": {"voice": "af_bella", "speed": 1.25, "tts_engine": "kokoro"}},
    )

    assert start.status_code == 200
    assert repair.status_code == 200


_FRONTEND_CANONICAL_RUNTIME_OPTIONS = {
    "voice": "af_bella",
    "speed": 1.35,
    "tts_engine": "kokoro",
    "tts_quantization": "bf16",
    "cleaner_backend": "api",
    "cleaner_model": None,
    "cleaner_api_model": "gpt-5-nano",
    "cleaner_api_key": "sk-contract",
    "tts_options": {"quantization": "bf16"},
    "parallel_worker_mode": "manual",
    "enable_parallel": True,
    "max_parallel_chapters": 3,
}


def _capture_conversion_controller():
    class _Status:
        value = "running"

    class _Job:
        id = "job-1"
        book_id = None
        status = _Status()
        error = None

    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))
            self.start_kwargs = None

        def start_conversion(self, **kwargs):  # noqa: ANN003
            self.start_kwargs = kwargs
            return _Job()

    return _Controller()


def _pipeline_config_for_payload(tmp_path: Path, api_payload: dict):
    from modules.app.config import AppConfig
    from modules.app.services.conversion_runtime import RuntimeOptionsResolver
    from modules.web.schemas.conversions import StartConversionRequest

    runtime_options = StartConversionRequest(**api_payload).to_runtime_options()
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
        # Test payload mapping without host-dependent CPU worker normalization.
        # Runtime validation is stubbed below; no MLX inference is performed.
        tts_provider="mlx",
    )
    resolver = RuntimeOptionsResolver(
        config=config,
        cleaner_api_limiter=SimpleNamespace(semaphore=None),
        runtime_validator=lambda *args, **kwargs: None,
    )
    pipeline_config = resolver.create_pipeline_config(
        conversion_mode=api_payload.get("conversion_mode", "audiobook"),
        runtime_options=runtime_options,
    )
    return runtime_options, pipeline_config


@pytest.mark.parametrize(
    ("api_payload", "expected_runtime", "expected_pipeline"),
    [
        pytest.param(
            {
                "source_path": "/tmp/frontend-contract.epub",
                "conversion_mode": "document_audio",
                "title": "Frontend Contract Book",
                "author": "Contract Author",
                "runtime_options": _FRONTEND_CANONICAL_RUNTIME_OPTIONS,
            },
            _FRONTEND_CANONICAL_RUNTIME_OPTIONS,
            {
                "conversion_mode": "document_audio",
                "voice": "af_bella",
                "speed": 1.35,
                "tts_engine": "kokoro",
                "tts_quantization": "bf16",
                "cleaner_backend": "api",
                "cleaner_api_model": "gpt-5-nano",
                "enable_parallel": True,
                "max_parallel_chapters": 3,
                "parallel_worker_mode": "manual",
            },
            id="frontend-canonical-nested",
        ),
        pytest.param(
            {
                "source_path": "/tmp/frontend-contract.epub",
                "conversion_mode": "document_audio",
                "title": "Frontend Contract Book",
                "author": "Contract Author",
                **_FRONTEND_CANONICAL_RUNTIME_OPTIONS,
            },
            _FRONTEND_CANONICAL_RUNTIME_OPTIONS,
            {
                "conversion_mode": "document_audio",
                "voice": "af_bella",
                "speed": 1.35,
                "tts_engine": "kokoro",
                "tts_quantization": "bf16",
                "cleaner_backend": "api",
                "cleaner_api_model": "gpt-5-nano",
                "enable_parallel": True,
                "max_parallel_chapters": 3,
                "parallel_worker_mode": "manual",
            },
            id="flat-legacy",
        ),
        pytest.param(
            {
                "source_path": "/tmp/book.pdf",
                "voice": "af_bella",
                "speed": 1.35,
                "parallel_worker_mode": "manual",
                "max_parallel_chapters": 3,
                "runtime_options": {
                    "tts_engine": "kokoro",
                    "tts_quantization": "bf16",
                    "cleaner_backend": "api",
                    "cleaner_api_model": "gpt-5-nano",
                    "enable_parallel": True,
                },
            },
            {
                "voice": "af_bella",
                "speed": 1.35,
                "tts_engine": "kokoro",
                "tts_quantization": "bf16",
                "cleaner_backend": "api",
                "cleaner_api_model": "gpt-5-nano",
                "parallel_worker_mode": "manual",
                "enable_parallel": True,
                "max_parallel_chapters": 3,
            },
            {
                "voice": "af_bella",
                "speed": 1.35,
                "tts_engine": "kokoro",
                "tts_quantization": "bf16",
                "cleaner_backend": "api",
                "cleaner_api_model": "gpt-5-nano",
                "enable_parallel": True,
                "max_parallel_chapters": 3,
                "parallel_worker_mode": "manual",
            },
            id="partial-flat-and-nested-merge",
        ),
    ],
)
def test_conversion_api_payload_contract_maps_to_controller_and_pipeline_config(
    tmp_path,
    api_payload,
    expected_runtime,
    expected_pipeline,
):
    controller = _capture_conversion_controller()
    client = _make_client_with_controller(controller)
    response = client.post("/api/conversions", json=api_payload)

    assert response.status_code == 200
    controller_runtime = controller.start_kwargs["runtime_options"]

    for field, expected_value in expected_runtime.items():
        assert getattr(controller_runtime, field) == expected_value

    _, pipeline_config = _pipeline_config_for_payload(tmp_path, api_payload)
    for field, expected_value in expected_pipeline.items():
        assert getattr(pipeline_config, field) == expected_value


def test_start_conversion_rejects_invalid_conversion_mode():
    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))
            self.start_called = False

        def start_conversion(self, **kwargs):  # noqa: ANN003
            self.start_called = True
            return kwargs

    controller = _Controller()
    client = _make_client_with_controller(controller)

    response = client.post(
        "/api/conversions",
        json={
            "source_path": "/tmp/book.pdf",
            "conversion_mode": "document",
        },
    )

    assert response.status_code == 422
    assert controller.start_called is False


@pytest.mark.parametrize("missing_asset", ["snapshot", "receipt", "phonemizer", "voice"])
def test_voice_preview_returns_installation_guidance_after_model_load(
    monkeypatch, tmp_path, missing_asset,
):
    import importlib.metadata
    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError
    from unittest.mock import Mock
    from fastapi.testclient import TestClient
    from modules.app.config import AppConfig
    from modules.app.services.voice_service import VoiceService
    from modules.tts import mlx_compat, model_snapshots
    from modules.tts.engine import TTSEngine
    from modules.tts.runtime_spec import KOKORO_MLX_DEFAULT_MODEL_REPO
    from modules.web.server import create_app

    model_name = KOKORO_MLX_DEFAULT_MODEL_REPO
    messages = {
        "snapshot": f"Model {model_name} is not installed. Reinstall the model from Setup.",
        "receipt": f"Incomplete or damaged cache for {model_name}. Reinstall the model from Setup.",
        "phonemizer": "The English phonemizer model is missing. Reinstall the runtime from Setup.",
        "voice": f"Voice af_heart is missing from the installed snapshot for {model_name}. Reinstall the model from Setup.",
    }

    def snapshot_download(**kwargs):
        assert kwargs["local_files_only"] is True
        if missing_asset == "snapshot":
            raise LocalEntryNotFoundError("Snapshot absent")
        return str(tmp_path)

    def distribution(name):
        if missing_asset == "phonemizer":
            raise importlib.metadata.PackageNotFoundError(name)
        return object()

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(model_snapshots._receipt_verifier, "verify", lambda *a, **kw: missing_asset != "receipt")
    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    monkeypatch.setitem(sys.modules, "mlx_audio.tts.models.kokoro.pipeline", SimpleNamespace(
        KokoroPipeline=type("Pipeline", (), {"load_single_voice": Mock()}),
    ))

    def synthesize_once(**kwargs):
        with mlx_compat.use_installed_voices(model_name):
            model_snapshots.installed_voice_path(model_name, "af_heart")
        pytest.fail("Synthesis must require installed assets")

    engine = TTSEngine.__new__(TTSEngine)
    engine._model_path = model_name
    engine._model_loaded = True
    engine._synthesize_once = Mock(side_effect=synthesize_once)
    engine._retry_or_exhausted = Mock(return_value=False)
    engine.validate_voice = lambda voice: True
    service = VoiceService(AppConfig(
        data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path,
    ), tts_factory=Mock())
    monkeypatch.setattr(service, "ensure_preview_engine", lambda **kwargs: engine)
    monkeypatch.setattr(service, "_schedule_preview_idle_unload", lambda: None)
    controller = SimpleNamespace(config=service._config, generate_voice_preview=service.generate_voice_preview)
    client = TestClient(create_app(controller=controller), raise_server_exceptions=False)

    response = client.post("/api/voices/preview", json={
        "voice": "af_heart", "text": "Hello again.", "tts_provider": "mlx",
    })

    assert response.status_code == 409
    assert response.json() == {"detail": messages[missing_asset]}
    engine._synthesize_once.assert_called_once()
    engine._retry_or_exhausted.assert_not_called()
    assert service.metrics_snapshot()["preview_generation_count"] == 0


@pytest.mark.parametrize("speed", [0.5, 1.35, 2.5, 3.0])
def test_voice_preview_route_forwards_runtime_selection_payload(tmp_path, speed):
    preview_path = tmp_path / "preview.wav"
    preview_path.write_bytes(b"RIFF$\x00\x00\x00WAVEfmt ")

    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))
            self.preview_kwargs = None

        def generate_voice_preview(self, **kwargs):  # noqa: ANN003
            self.preview_kwargs = kwargs
            return preview_path

    controller = _Controller()
    client = _make_client_with_controller(controller)

    response = client.post(
        "/api/voices/preview",
        json={
            "voice": "af_bella",
            "speed": speed,
            "text": "Preview the selected voice.",
            "tts_engine": "kokoro",
            "tts_quantization": "bf16",
            "tts_provider": "cuda",
            "tts_device": "cuda:0",
            "tts_options": {"quantization": "bf16", "dtype": "fp16"},
        },
    )

    assert response.status_code == 200
    assert controller.preview_kwargs == {
        "voice": "af_bella",
        "speed": speed,
        "text": "Preview the selected voice.",
        "engine_name": "kokoro",
        "tts_quantization": "bf16",
        "tts_provider": "cuda",
        "tts_device": "cuda:0",
        "tts_options": {"quantization": "bf16", "dtype": "fp16"},
    }
    assert not preview_path.exists()


def test_voice_preview_file_is_removed_when_the_client_disconnects(tmp_path):
    import anyio

    from modules.web.routes.voices import _EphemeralFileResponse

    preview_path = tmp_path / "preview.wav"
    preview_path.write_bytes(b"RIFF$\x00\x00\x00WAVEfmt " * 4096)
    response = _EphemeralFileResponse(path=preview_path, media_type="audio/wav")

    async def receive():  # noqa: ANN202
        # The disconnect surfaces through the failed send, not through receive.
        await anyio.sleep_forever()

    async def disconnected_send(message):  # noqa: ANN001, ANN202
        if message["type"] == "http.response.body":
            raise OSError("client went away")

    async def serve() -> None:
        await response(
            {"type": "http", "method": "GET", "path": "/", "headers": [], "extensions": {}},
            receive, disconnected_send,
        )

    with pytest.raises(Exception):
        anyio.run(serve)
    assert not preview_path.exists()


def test_system_capabilities_include_parallel_worker_recommendation(monkeypatch):
    from modules.pipeline.memory_policy import MemorySnapshot
    from modules.pipeline import worker_recommendation

    monkeypatch.setattr(
        worker_recommendation,
        "sample_memory_snapshot",
        lambda device_memory_policy=None: MemorySnapshot(
            total_bytes=16 * 1024**3,
            available_bytes=12 * 1024**3,
            process_rss_bytes=1024**3,
        ),
    )

    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(
                temp_dir=Path("temp"),
                output_dir=Path("output"),
                enable_parallel=False,
                max_parallel_chapters=1,
            )

        def get_system_capabilities(self):
            return {
                "parallel_worker_recommendation": worker_recommendation.build_parallel_worker_recommendation(
                    requested_max=4
                )
            }

    client = _make_client_with_controller(_Controller())
    response = client.get("/api/system/capabilities")

    assert response.status_code == 200
    recommendation = response.json()["parallel_worker_recommendation"]
    assert recommendation["total_memory_bytes"] == 16 * 1024**3
    assert recommendation["available_memory_bytes"] == 12 * 1024**3
    assert recommendation["reserved_memory_bytes"] == 4 * 1024**3
    assert recommendation["static_estimated_worker_bytes"] == 4 * 1024**3
    assert recommendation["max_safe_parallel_chapters"] == 3
    assert recommendation["recommended_parallel_chapters"] == 3
    assert "available - reserved" in recommendation["calculation"]


def test_model_options_expose_kokoro_conversion_capabilities():
    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))

        def get_available_tts_engines(self):
            return [
                {
                    "id": "kokoro",
                    "display_name": "Kokoro",
                    "available_for_conversion": True,
                    "capabilities": {
                        "provider": "mlx-audio",
                        "worker_auto_mode": "sequential",
                        "process_safe": False,
                        "supports_conversion": True,
                    },
                    "supported_quantizations": ["bf16"],
                    "options_schema": {"quantization": {"type": "string"}},
                    "voices": [],
                },
            ]

        def get_available_cleaner_models(self):
            return ["qwen"]

        def get_available_api_cleaner_models(self):
            return ["gpt-5-nano"]

        def get_cuda_cleaner_models(self):
            from modules.tts.cleaner_config import list_cuda_cleaner_models

            return list_cuda_cleaner_models()

        def get_cleaner_backend_options(self):
            from modules.tts.cleaner_backends.metadata import list_cleaner_backend_options

            return list_cleaner_backend_options()

    client = _make_client_with_controller(_Controller())
    response = client.get("/api/options/models")

    assert response.status_code == 200
    engines = {engine["id"]: engine for engine in response.json()["tts_engines"]}
    assert list(engines) == ["kokoro"]
    assert engines["kokoro"]["available_for_conversion"] is True
    assert engines["kokoro"]["capabilities"]["worker_auto_mode"] == "sequential"
    assert engines["kokoro"]["capabilities"]["process_safe"] is False
    payload = response.json()
    assert isinstance(payload.get("cuda_cleaner_models"), list)
    assert payload["cuda_cleaner_models"]
    backend_options = payload.get("cleaner_backend_options")
    assert isinstance(backend_options, list)
    option_names = [option["name"] for option in backend_options]
    assert option_names == ["api", "basic", "cuda", "mlx"]
    cuda_option = next(option for option in backend_options if option["name"] == "cuda")
    assert cuda_option["supported_quantizations"] == ["4bit", "8bit", "fp16"]
    assert cuda_option["default_device"] == "cuda"


def test_start_conversion_nested_runtime_options_forwards_cuda_fields():
    captured: dict[str, object] = {}

    class _Controller:
        def __init__(self):
            self.config = SimpleNamespace(temp_dir=Path("temp"), output_dir=Path("output"))

        def start_conversion(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                id="job-1",
                status="queued",
                book_id=None,
                error=None,
            )

    client = _make_client_with_controller(_Controller())
    response = client.post(
        "/api/conversions",
        json={
            "source_path": "/tmp/sample.md",
            "runtime_options": {
                "cleaner_backend": "cuda",
                "cleaner_cuda_device": "cuda:1",
                "cleaner_cuda_quantization": "fp16",
            },
        },
    )

    assert response.status_code == 200
    runtime_options = captured["runtime_options"]
    assert runtime_options.cleaner_backend == "cuda"
    assert runtime_options.cleaner_cuda_device == "cuda:1"
    assert runtime_options.cleaner_cuda_quantization == "fp16"


def test_web_api(tmp_path) -> bool:
    print("\n" + "=" * 50)
    print("WEB API TEST SUITE")
    print("=" * 50 + "\n")

    client = _make_client(tmp_path)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json().get("status") == "ok"
    print("✓ /health")

    from fastapi.testclient import TestClient
    import modules.web.server as web_server
    from modules.app.events import make_progress_event
    from modules.web.events import normalize_event

    created_controllers = []

    class _LifecycleController:
        def __init__(self):
            self.cleaned = False
            self.config = type(
                "Config",
                (),
                {"output_dir": Path("output"), "temp_dir": Path("temp")},
            )()
            created_controllers.append(self)

        def cleanup(self):
            self.cleaned = True

    with patch.object(web_server, "AppController", _LifecycleController):
        lifecycle_app = web_server.create_app()
        assert created_controllers == []

        with TestClient(lifecycle_app) as lifecycle_client:
            lifecycle_health = lifecycle_client.get("/health")
            assert lifecycle_health.status_code == 200
            assert len(created_controllers) == 1
            assert created_controllers[0].cleaned is False

        assert created_controllers[0].cleaned is True
    print("✓ create_app defers controller construction to lifespan and cleans it up")

    progress_payload = normalize_event(
        make_progress_event(
            stage="synthesizing",
            progress=0.42,
            message="chunk done",
        )
    )
    assert progress_payload["event_type"] == "progress"
    assert progress_payload["stage"] == "synthesizing"
    assert progress_payload["progress"] == progress_payload["overall_progress"]
    assert progress_payload["stage_progress"] == 0.42
    assert "eta" in progress_payload
    print("✓ progress payload exposes explicit dashboard progress fields")

    bad_upload = client.post(
        "/api/uploads/source",
        files={
            "file": (
                "notes.docx",
                b"word",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert bad_upload.status_code == 400
    print("✓ /api/uploads/source rejects unsupported types")

    library = client.get("/api/library")
    assert library.status_code == 200
    payload = library.json()
    assert isinstance(payload, dict)
    assert "items" in payload
    assert isinstance(payload["items"], list)
    print("✓ /api/library")

    library_count = client.get("/api/library/count")
    assert library_count.status_code == 200
    assert library_count.json()["count"] == 0
    print("✓ /api/library/count")

    models = client.get("/api/options/models")
    assert models.status_code == 200
    data = models.json()
    assert isinstance(data.get("tts_engines"), list)
    if data.get("tts_engines"):
        first_engine = data["tts_engines"][0]
        assert "capabilities" in first_engine
        assert "batching_mode" in first_engine["capabilities"]
        assert "supports_batching" in first_engine["capabilities"]
        assert "supported_quantizations" in first_engine
    assert isinstance(data.get("cleaner_models"), list)
    assert isinstance(data.get("api_cleaner_models"), list)
    assert isinstance(data.get("cuda_cleaner_models"), list)
    assert isinstance(data.get("cleaner_backend_options"), list)
    print("✓ /api/options/models")

    class _Status:
        def __init__(self, value: str):
            self.value = value

    class _FakeJob:
        def __init__(self, job_id: str):
            self.id = job_id
            self.book_id = None
            self.status = _Status("running")
            self.error = None

        def is_active(self) -> bool:
            return self.status.value == "running"

    class _FakeController:
        def __init__(self, *, temp_dir: Path | None = None, output_dir: Path | None = None):
            self.job = None
            self.start_kwargs = None
            self.library_searches = []
            self.audio_path = "/tmp/fake-audio.m4b"
            self.preview_path = None
            self.voice_suggestion_kwargs = None
            self.playback_position_updates = []
            self.open_folder_calls = []
            self.deleted_book_ids = []
            self.cleanup_requests = []
            self.config = SimpleNamespace(
                temp_dir=temp_dir or Path("temp"),
                output_dir=output_dir or Path("output"),
                db_path=(temp_dir or Path("temp")).parent / "data" / "audiobooks.db",
            )
            from modules.app.services.library_service import LibraryService

            self._library_service = LibraryService(
                object(),
                output_dir=self.config.output_dir,
                temp_dir=self.config.temp_dir,
                storage_root=Path(self.config.db_path).parent.parent,
            )

        def start_conversion(self, **kwargs):  # noqa: ANN003
            self.start_kwargs = kwargs
            self.job = _FakeJob("conv_test_1")
            return self.job

        def get_active_job(self):
            return self.job

        def cancel_conversion(self, expected_job_id=None) -> bool:
            if not self.job:
                return False
            self.job.status = _Status("cancelled")
            return True

        def get_library_books(
            self, search=None, limit=200, offset=0, conversion_mode=None, favorite=None
        ):
            self.library_searches.append((search, limit, offset, conversion_mode, favorite))

            class _BookSummary:
                def __init__(self):
                    self.id = 1
                    self.title = "Wind-Up Bird"
                    self.author = "Murakami"
                    self.total_chapters = 3
                    self.completed_chapters = 3
                    self.created_at = None

            return [_BookSummary()]

        def get_library_count(self):
            return 1

        def get_book(self, book_id):
            if book_id != 1:
                return None
            playback_status = (
                "packaged"
                if Path(self.audio_path).suffix.lower() in {".m4b", ".m4a"}
                else "chapter_audio"
            )
            return {
                "id": 1,
                "title": "Wind-Up Bird",
                "author": "Murakami",
                "source_path": "/tmp/sample.pdf",
                "source_type": "pdf",
                "conversion_mode": "document_audio",
                "total_chapters": 3,
                "playback_path": self.audio_path,
                "playback_status": playback_status,
                "playback_position_sec": 91.5,
                "created_at": None,
                "chapters": [
                    {
                        "id": 11,
                        "number": 1,
                        "title": "Chapter 1",
                        "duration_ms": 1000,
                        "mp3_path": None,
                    }
                ],
            }

        def upload_source(self, *, filename, file_obj, max_bytes):  # noqa: ANN001
            return self._library_service.upload_source(
                filename=filename,
                file_obj=file_obj,
                max_bytes=max_bytes,
            )

        def upload_text_source(self, *, text, title, max_bytes):  # noqa: ANN001
            return self._library_service.upload_text_source(
                text=text,
                title=title,
                max_bytes=max_bytes,
            )

        def preflight_source(self, source_path):
            return self._library_service.preflight_source(source_path)

        def get_book_playback_file(self, book_id):
            if book_id != 1:
                return None
            audio_path = Path(self.audio_path)
            if not audio_path.exists() or not audio_path.is_file():
                return None
            return audio_path

        def update_book_metadata(self, book_id, title=None, author=None, tags=None, favorite=None):
            if book_id != 1:
                return None
            return {
                **self.get_book(book_id),
                "title": title or "Wind-Up Bird",
                "author": author,
                "tags": tags or "",
                "favorite": bool(favorite),
                "source_available": True,
            }

        def get_chapter_cleaning_preview(self, book_id, chapter_number):
            if book_id != 1 or chapter_number != 1:
                return None
            return {
                "book_id": book_id,
                "chapter_number": chapter_number,
                "title": "Chapter 1",
                "available": True,
                "before_text": "raw",
                "llm_input_text": "rule",
                "cleaned_text": "clean",
                "trace_rows": [{"chunk_index": 1}],
            }

        def update_book_playback_position(self, book_id, position_sec):
            self.playback_position_updates.append((book_id, position_sec))
            if book_id != 1:
                return None
            return position_sec

        def open_book_folder(self, book_id):
            self.open_folder_calls.append(book_id)
            if book_id != 1:
                return None
            return Path("/tmp")

        def delete_book(self, book_id):
            self.deleted_book_ids.append(book_id)
            return book_id == 1

        def repair_conversion(self, **kwargs):  # noqa: ANN003
            self.job = _FakeJob("repair_test_1")
            self.job.book_id = kwargs["book_id"]
            return self.job

        def get_storage_diagnostics(self):
            return {
                "database_path": str(self.config.db_path),
                "output_path": str(self.config.output_dir),
                "temp_path": str(self.config.temp_dir),
                "app_data_path": str(Path(self.config.db_path).parent.parent),
                "audit": {
                    "row_counts": {"books": 1, "chapters": 1},
                    "stale_book_sources": [],
                    "orphan_output_dirs": [],
                    "stale_temp_uploads": [],
                    "stale_temp_stage_dirs": [],
                },
            }

        def backup_database(self):
            return Path("/tmp/audiobooks-backup.db")

        def cleanup_storage(self, *, confirm):  # noqa: ANN003
            self.cleanup_requests.append(confirm)
            if confirm != "cleanup":
                raise ValueError("Cleanup requires confirm='cleanup'")
            return {
                "backup_path": "/tmp/audiobooks-backup.db",
                "actions": {"deleted_output_dirs": []},
            }

        def get_available_tts_engines(self):
            return [
                {
                    "id": "kokoro",
                    "name": "Kokoro",
                    "voices": [{"id": "af_bella", "name": "Bella", "language": "en"}],
                    "available_for_conversion": True,
                    "capabilities": {"supports_batching": True, "batching_mode": "threaded"},
                    "supported_quantizations": ["bf16"],
                    "options_schema": {"quantization": {"type": "string"}},
                }
            ]

        def get_available_voices(self, engine_name=None):
            _ = engine_name
            return [{"id": "af_bella", "name": "Bella", "language": "en-US", "locale": "en-US"}]

        def get_available_cleaner_models(self):
            return ["qwen"]

        def get_available_api_cleaner_models(self):
            return ["moonshot-v1-8k"]

        def get_system_capabilities(self):
            return {
                "primary_runtime": "mlx",
                "mlx_runtime_available": True,
                "mlx_cleaner_runtime_available": False,
                "runtime_probe": {
                    "mode": "import",
                    "cache_ttl_seconds": 60,
                    "cache_hit": True,
                    "state": "fresh",
                    "refresh_in_progress": False,
                    "tts": {"available": True, "latency_ms": 10.5, "error": None},
                    "cleaner": {"available": False, "latency_ms": 4010.0, "error": "timeout"},
                },
                "available_tts_engines": ["kokoro"],
                "metrics": {},
            }

        def generate_voice_preview(
            self,
            voice,
            speed=1.0,
            text=None,
            engine_name=None,
            tts_quantization=None,
            tts_options=None,
        ):
            _ = (voice, speed, text, engine_name, tts_quantization, tts_options)
            if not self.preview_path:
                raise ValueError("preview not available")
            return Path(self.preview_path)

        def suggest_voice_for_book(self, source_path, engine_name=None):
            self.voice_suggestion_kwargs = {
                "source_path": source_path,
                "engine_name": engine_name,
            }
            return {
                "engine": engine_name or "kokoro",
                "source_path": str(source_path),
                "book_sample": {
                    "unit": "pages",
                    "units_analyzed": 3,
                    "total_units": 240,
                    "excerpt": "War had settled over the valley by dusk.",
                },
                "book_profile": {
                    "book_type": "war novel",
                    "tone": "serious",
                },
                "voice_suggestion": {
                    "recommended_voice_id": "af_bella",
                    "recommended_voice_style": "warm but steady",
                    "reasoning": "Balances gravity with clarity for long-form narration.",
                },
                "available_voice_count": 1,
            }

    with (
        tempfile.TemporaryDirectory(prefix="web-api-temp-") as temp_dir,
        tempfile.TemporaryDirectory(prefix="web-api-output-") as output_dir,
    ):
        fake_controller = _FakeController(temp_dir=Path(temp_dir), output_dir=Path(output_dir))
        fake_client = _make_client_with_controller(fake_controller)

        accepted_uploads = [
            ("sample.pdf", b"%PDF-1.4\n% test", "application/pdf"),
            ("book.epub", b"PK\x03\x04epub", "application/epub+zip"),
            ("notes.txt", b"plain text", "text/plain"),
            ("guide.md", b"# Guide\n\nText", "text/markdown"),
        ]
        upload_root = Path(fake_controller.config.temp_dir) / "web_uploads"
        uploaded_handle = None
        for filename, payload_bytes, content_type in accepted_uploads:
            upload = fake_client.post(
                "/api/uploads/source",
                files={"file": (filename, payload_bytes, content_type)},
            )
            assert upload.status_code == 200
            upload_payload = upload.json()
            assert upload_payload["original_name"] == filename
            assert "stored_path" not in upload_payload
            assert "source_handle" in upload_payload
            assert upload_payload["source_handle"].startswith("upload:")
            stored_path = upload_root / upload_payload["source_handle"].removeprefix("upload:")
            assert stored_path.exists()
            assert stored_path.suffix.lower() == Path(filename).suffix.lower()
            assert upload_payload["preflight"]["source_type"] == Path(
                filename
            ).suffix.lower().lstrip(".")
            assert "sample" in upload_payload["preflight"]
            if uploaded_handle is None:
                uploaded_handle = upload_payload["source_handle"]
        print("✓ /api/uploads/source returns opaque handles")

        preflight = fake_client.post(
            "/api/sources/preflight",
            json={"source_path": uploaded_handle},
        )
        assert preflight.status_code == 200
        preflight_payload = preflight.json()
        assert preflight_payload["source_type"] == "pdf"
        assert preflight_payload["title"]
        assert "chapter_count" in preflight_payload
        assert "sample" in preflight_payload
        print("✓ POST /api/sources/preflight resolves upload handles")

        handle_start = fake_client.post(
            "/api/conversions",
            json={
                "source_path": uploaded_handle,
                "voice": "af_bella",
                "speed": 1.25,
                "conversion_mode": "document_audio",
                "title": "Custom Title",
                "author": "Custom Author",
                "tts_engine": "kokoro",
                "tts_quantization": "bf16",
                "cleaner_model": "qwen",
                "cleaner_backend": "api",
                "cleaner_api_model": "moonshot-v1-8k",
                "tts_options": {"quantization": "bf16"},
            },
        )
        assert handle_start.status_code == 200
        assert handle_start.json()["job_id"] == "conv_test_1"
        handle_start_path = Path(fake_controller.start_kwargs["source_path"])
        assert handle_start_path == upload_root / uploaded_handle.removeprefix("upload:")
        runtime_options = fake_controller.start_kwargs["runtime_options"]
        assert runtime_options.voice == "af_bella"
        assert runtime_options.speed == 1.25
        assert fake_controller.start_kwargs["conversion_mode"] == "document_audio"
        assert fake_controller.start_kwargs["title"] == "Custom Title"
        assert fake_controller.start_kwargs["author"] == "Custom Author"
        assert runtime_options.tts_engine == "kokoro"
        assert runtime_options.tts_quantization == "bf16"
        assert runtime_options.cleaner_model == "qwen"
        assert runtime_options.cleaner_backend == "api"
        assert runtime_options.cleaner_api_model == "moonshot-v1-8k"
        assert runtime_options.tts_options == {"quantization": "bf16"}
        print("✓ POST /api/conversions resolves upload handles")

        voice_options = fake_client.get("/api/options/voices", params={"engine": "kokoro"})
        assert voice_options.status_code == 200
        assert voice_options.json()["engine"] == "kokoro"
        assert voice_options.json()["voices"][0]["id"] == "af_bella"
        print("✓ /api/options/voices")

        suggestion = fake_client.post(
            "/api/voices/suggest",
            json={
                "source_path": uploaded_handle,
                "tts_engine": "kokoro",
            },
        )
        assert suggestion.status_code == 200
        suggestion_payload = suggestion.json()
        assert suggestion_payload["voice_suggestion"]["recommended_voice_id"] == "af_bella"
        assert suggestion_payload["book_profile"]["tone"] == "serious"
        assert fake_controller.voice_suggestion_kwargs == {
            "source_path": upload_root / uploaded_handle.removeprefix("upload:"),
            "engine_name": "kokoro",
        }
        print("✓ POST /api/voices/suggest resolves upload handles")

        filtered = fake_client.get(
            "/api/library",
            params={
                "search": "murakami",
                "limit": 10,
                "offset": 5,
                "conversion_mode": "audiobook",
                "favorite": "true",
            },
        )
        assert filtered.status_code == 200
        assert fake_controller.library_searches[-1] == ("murakami", 10, 5, "audiobook", True)
        print("✓ /api/library filters passthrough")

        update_book = fake_client.patch(
            "/api/library/1",
            json={"title": "Updated", "author": "New Author", "tags": "favorite", "favorite": True},
        )
        assert update_book.status_code == 200
        assert update_book.json()["title"] == "Updated"
        assert update_book.json()["favorite"] is True
        print("✓ PATCH /api/library/{book_id}")

        cleaning_preview = fake_client.get("/api/library/1/chapters/1/cleaning-preview")
        assert cleaning_preview.status_code == 200
        assert cleaning_preview.json()["cleaned_text"] == "clean"
        print("✓ GET /api/library/{book_id}/chapters/{chapter_number}/cleaning-preview")

        suggestion = fake_client.post(
            "/api/voices/suggest",
            json={
                "source_path": "/tmp/sample.md",
                "tts_engine": "kokoro",
            },
        )
        assert suggestion.status_code == 200
        suggestion_payload = suggestion.json()
        assert suggestion_payload["voice_suggestion"]["recommended_voice_id"] == "af_bella"
        assert suggestion_payload["book_profile"]["tone"] == "serious"
        assert fake_controller.voice_suggestion_kwargs == {
            "source_path": Path("/tmp/sample.md"),
            "engine_name": "kokoro",
        }
        print("✓ POST /api/voices/suggest")

        capabilities = fake_client.get("/api/system/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["primary_runtime"] == "mlx"
        assert capabilities.json()["mlx_runtime_available"] is True
        assert capabilities.json()["mlx_cleaner_runtime_available"] is False
        assert capabilities.json()["runtime_probe"]["mode"] == "import"
        assert capabilities.json()["runtime_probe"]["state"] == "fresh"
        assert capabilities.json()["runtime_probe"]["refresh_in_progress"] is False
        print("✓ /api/system/capabilities")

        diagnostics = fake_client.get("/api/maintenance/diagnostics")
        assert diagnostics.status_code == 200
        assert diagnostics.json()["audit"]["row_counts"]["books"] == 1
        print("✓ GET /api/maintenance/diagnostics")

        backup = fake_client.post("/api/maintenance/backup")
        assert backup.status_code == 200
        assert Path(backup.json()["backup_path"]) == Path("/tmp/audiobooks-backup.db")
        print("✓ POST /api/maintenance/backup")

        cleanup_rejected = fake_client.post("/api/maintenance/cleanup", json={"confirm": "nope"})
        assert cleanup_rejected.status_code == 400

        cleanup = fake_client.post("/api/maintenance/cleanup", json={"confirm": "cleanup"})
        assert cleanup.status_code == 200
        assert fake_controller.cleanup_requests[-1] == "cleanup"
        print("✓ POST /api/maintenance/cleanup requires confirmation")

    detail = fake_client.get("/api/library/1")
    assert detail.status_code == 200
    assert detail.json()["title"] == "Wind-Up Bird"
    assert detail.json()["playback_status"] == "packaged"
    assert detail.json()["conversion_mode"] == "document_audio"
    assert detail.json()["playback_position_sec"] == 91.5
    print("✓ /api/library/{book_id} detail")

    missing = fake_client.get("/api/library/99")
    assert missing.status_code == 404
    print("✓ /api/library/{book_id} not found")

    playback_position = fake_client.put(
        "/api/library/1/playback-position",
        json={"position_sec": 123.75},
    )
    assert playback_position.status_code == 200
    assert playback_position.json() == {"book_id": 1, "position_sec": 123.75}
    assert fake_controller.playback_position_updates[-1] == (1, 123.75)
    print("✓ PUT /api/library/{book_id}/playback-position")

    open_folder = fake_client.post("/api/library/1/open-folder")
    assert open_folder.status_code == 200
    assert open_folder.json()["book_id"] == 1
    assert Path(open_folder.json()["folder_path"]) == Path("/tmp")
    assert fake_controller.open_folder_calls[-1] == 1
    print("✓ POST /api/library/{book_id}/open-folder")

    temp_audio_path = None
    temp_preview_path = None
    try:
        output_audio_dir = Path(fake_controller.config.output_dir)
        output_audio_dir.mkdir(parents=True, exist_ok=True)
        temp_audio_path = str(output_audio_dir / "test-audio.mp3")
        Path(temp_audio_path).write_bytes(b"ID3test-audio")
        fake_controller.audio_path = temp_audio_path

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(b"RIFF....WAVEfmt ")
            temp_preview_path = handle.name
            fake_controller.preview_path = temp_preview_path

        audio = fake_client.get("/api/library/1/audio")
        assert audio.status_code == 200
        assert audio.content.startswith(b"ID3test-audio")

        preview = fake_client.post(
            "/api/voices/preview",
            json={
                "voice": "af_bella",
                "speed": 1.1,
                "text": "hello preview",
                "tts_engine": "kokoro",
                "tts_quantization": "bf16",
                "tts_options": {"quantization": "bf16"},
            },
        )
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("audio/wav")
        assert preview.content.startswith(b"RIFF")
    finally:
        if temp_audio_path:
            Path(temp_audio_path).unlink(missing_ok=True)
        if temp_preview_path:
            Path(temp_preview_path).unlink(missing_ok=True)
    print("✓ /api/library/{book_id}/audio playback stream")
    print("✓ POST /api/voices/preview")

    active = fake_client.get("/api/conversions/active")
    assert active.status_code == 200
    active_payload = active.json()
    assert active_payload["active"] is True
    assert active_payload["job"]["id"] == "conv_test_1"
    print("✓ GET /api/conversions/active")

    repair = fake_client.post(
        "/api/conversions/repair/1",
        json={"voice": "af_bella", "speed": 1.0, "tts_engine": "kokoro"},
    )
    assert repair.status_code == 200
    assert repair.json()["job_id"] == "repair_test_1"
    assert repair.json()["book_id"] == 1
    print("✓ POST /api/conversions/repair/{book_id}")

    cancel = fake_client.post("/api/conversions/repair_test_1/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is True
    print("✓ POST /api/conversions/{job_id}/cancel")

    delete_response = fake_client.delete("/api/library/1")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"book_id": 1, "deleted": True}
    assert fake_controller.deleted_book_ids[-1] == 1
    print("✓ DELETE /api/library/{book_id}")

    print("\n" + "=" * 50)
    print("ALL WEB API TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    # Windows cannot delete the library database while the app still holds it
    # open; pytest's tmp_path tolerates that, so script mode should too.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        success = test_web_api(Path(tmp))
    sys.exit(0 if success else 1)


def test_delete_library_book_returns_409_while_book_job_active():
    class _Controller:
        def delete_book(self, book_id):  # noqa: ANN001, ANN201
            raise RuntimeError(
                "Cannot delete a book while it is being converted or repaired"
            )

    client = _make_client_with_controller(_Controller())
    response = client.delete("/api/library/7")

    assert response.status_code == 409
    assert "converted or repaired" in response.json()["detail"]
