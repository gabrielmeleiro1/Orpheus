"""Authenticated shutdown of the desktop Uvicorn server."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.launchers.desktop_backend import _build_desktop_server
from modules.web.events import STREAM_CLOSE_SENTINEL
from modules.web.server import create_app


@pytest.mark.parametrize("probe_times_out", [False, True])
def test_failed_worker_startup_finishes_cleanup_before_raising(monkeypatch, probe_times_out):
    from concurrent.futures import Future
    import threading

    from modules.pipeline.pool_manager import SharedWorkerPoolManager
    from modules.pipeline.worker_errors import WorkerStartupError, WorkerStartupStatus

    stopped = threading.Event()

    class Executor:
        def __init__(self, **kwargs):
            pass

        def submit(self, *args):
            probe = Future()
            if not probe_times_out:
                probe.set_result(WorkerStartupStatus(
                    success=False, provider="unknown", device=None,
                    error_type="WorkerRuntimeLoadError", message="load failed",
                ))
            return probe

        def shutdown(self, wait=True, **kwargs):
            if wait:
                stopped.wait(0.05)
                stopped.set()

    monkeypatch.setattr("modules.pipeline.pool_manager.ThreadPoolExecutor", Executor)
    manager = SharedWorkerPoolManager(
        startup_probe_timeout_seconds=0.01, shutdown_timeout_seconds=0.5,
    )
    manager._use_processes = False
    try:
        with pytest.raises(WorkerStartupError):
            manager.ensure_started("book")
        assert stopped.is_set(), "Startup raised before executor cleanup finished"
        assert not manager.cleanup_pending
        assert manager.shutdown(timeout_seconds=0.0) is True
    finally:
        stopped.set()
        manager.shutdown(timeout_seconds=1.0)


@pytest.mark.parametrize("timeout", [0.0, 0.02])
def test_shutdown_during_worker_startup_respects_cleanup_deadline(monkeypatch, timeout):
    from concurrent.futures import Future
    import threading
    import time

    from modules.pipeline.pool_manager import SharedWorkerPoolManager

    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    errors = []
    probe = Future()
    probe.set_running_or_notify_cancel()

    class Executor:
        def __init__(self, **kwargs):
            pass

        def submit(self, *args):
            entered.set()
            return probe

        def shutdown(self, wait=True, **kwargs):
            if wait:
                assert release.wait(3)
                stopped.set()

    monkeypatch.setattr("modules.pipeline.pool_manager.ThreadPoolExecutor", Executor)
    manager = SharedWorkerPoolManager(startup_probe_timeout_seconds=0.8, shutdown_timeout_seconds=0.0)
    manager._use_processes = False

    def start():
        try:
            manager.ensure_started("book")
        except RuntimeError as exc:
            errors.append(exc)

    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert entered.wait(2)
        before = time.monotonic()
        quiesced = manager.shutdown(timeout_seconds=timeout)
        assert time.monotonic() - before < 0.2, "Shutdown waited for the startup probe"
        assert quiesced is False
        starter.join(0.3)
        assert not starter.is_alive(), "Startup did not observe shutdown"
        assert errors
        assert manager.shutdown(timeout_seconds=0.0) is False
        assert not stopped.is_set()
    finally:
        release.set()
        starter.join(3)
        manager.shutdown(timeout_seconds=1.0)


@pytest.mark.parametrize("authorization", [None, "Bearer wrong"])
def test_shutdown_rejects_missing_or_wrong_token(authorization):
    app = create_app(controller=SimpleNamespace(), desktop_mode=True, desktop_api_token="secret")
    server = _build_desktop_server(app, host="127.0.0.1", port=0, token="secret", runtime_profile="base")
    headers = {} if authorization is None else {"Authorization": authorization}
    response = TestClient(app).post("/api/desktop/shutdown", headers=headers)
    assert response.status_code == 401
    assert not server.should_exit


def test_authenticated_shutdown_closes_streams_and_requests_uvicorn_exit():
    app = create_app(controller=SimpleNamespace(), desktop_mode=True, desktop_api_token="secret")
    server = _build_desktop_server(app, host="127.0.0.1", port=0, token="secret", runtime_profile="base")
    subscriber = app.state.event_broker.subscribe()
    client = TestClient(app)
    for _ in range(2):
        response = client.post("/api/desktop/shutdown", headers={"Authorization": "Bearer secret"})
        assert response.status_code == 202
    assert server.should_exit
    assert subscriber.get_nowait() is STREAM_CLOSE_SENTINEL
    assert subscriber.empty()


def test_shutdown_is_not_exposed_outside_desktop_mode():
    app = create_app(controller=SimpleNamespace())
    assert TestClient(app).post("/api/desktop/shutdown").status_code == 404


def test_shutdown_without_server_callback_reports_unavailable():
    app = create_app(controller=SimpleNamespace(), desktop_mode=True, desktop_api_token="secret")
    response = TestClient(app).post("/api/desktop/shutdown", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 503


@pytest.mark.parametrize("terminal", [False, True])
def test_cleanup_keeps_resources_open_until_conversion_thread_exits(tmp_path, terminal, capsys, monkeypatch):
    import threading
    import time
    from unittest.mock import Mock

    from modules.app.controller import AppController, ConversionJob, JobStatus
    from tests.test_conversion_service import _make_service

    service = _make_service(tmp_path, None, job_cls=ConversionJob, status_enum=JobStatus)
    service.cleanup = Mock()
    job = ConversionJob(id="active", book_id=None, status=JobStatus.RUNNING)
    service._active_job = job
    released = threading.Event()
    entered = threading.Event()
    controller = object.__new__(AppController)
    controller._conversion_service = service
    controller._shared_worker_pool_manager = None
    controller._voice_service = Mock()
    controller._cleaner_api_limiter = Mock()
    controller._owns_repository = True
    controller.repository = Mock()
    controller._CLEANUP_CONVERSION_CANCEL_TIMEOUT_SECONDS = 0.01
    controller._CLEANUP_POST_WORKER_CANCEL_TIMEOUT_SECONDS = 0.01
    monkeypatch.setenv("AUDIOBOOK_DESKTOP_READY_FILE", str(tmp_path / "ready"))

    def finish():
        if terminal:
            job.complete(1)
        entered.set()
        released.wait(2)
        controller.repository.update_book(1)
        job.complete(1)

    job._thread = threading.Thread(target=finish)
    job._thread.start()
    assert entered.wait(2)
    try:
        started = time.monotonic()
        assert controller.cleanup() is False
        assert time.monotonic() - started < 0.5
        controller.repository.close.assert_not_called()
        service.cleanup.assert_not_called()
        controller._voice_service.cleanup.assert_not_called()
        controller._cleaner_api_limiter.shutdown.assert_not_called()
        assert '"event":"shutdown"' not in capsys.readouterr().out
        with pytest.raises(RuntimeError, match="shutting down"):
            service._preflight_job_slot_available()
    finally:
        released.set()
        job._thread.join(2)
    assert controller.cleanup() is True
    controller.repository.close.assert_called_once()
    assert '"event":"shutdown"' in capsys.readouterr().out


def test_cleanup_does_not_close_repository_before_pool_quiescence():
    from unittest.mock import Mock

    from modules.app.controller import AppController

    controller = object.__new__(AppController)
    controller._conversion_service = Mock(cancel_active_job=Mock(return_value=True))
    controller._shared_worker_pool_manager = Mock(shutdown=Mock(return_value=False))
    controller._voice_service = Mock()
    controller._cleaner_api_limiter = Mock()
    controller._owns_repository = True
    controller.repository = Mock()
    controller._emit_desktop_shutdown_ack = Mock()
    assert controller.cleanup() is False
    controller.repository.close.assert_not_called()
    controller._emit_desktop_shutdown_ack.assert_not_called()


def test_pending_conversion_is_not_quiesced_before_thread_launch(tmp_path):
    from modules.app.controller import ConversionJob, JobStatus
    from tests.test_conversion_service import _make_service

    service = _make_service(tmp_path, None, job_cls=ConversionJob, status_enum=JobStatus)
    service._active_job = ConversionJob(id="pending", book_id=None, status=JobStatus.IDLE)
    assert service.cancel_active_job(timeout_seconds=0.0) is False
