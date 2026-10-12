"""Regression coverage for model download service."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from modules.app.services import model_download_service as downloads


def test_download_pipe_failure_bounds_worker_cleanup_before_releasing_resources(monkeypatch):
    service = downloads.ModelDownloadService()
    live = [False]
    process = Mock(exitcode=0)
    process.start.side_effect = lambda: live.__setitem__(0, True)
    process.is_alive.side_effect = lambda: live[0]
    process.kill.side_effect = lambda: live.__setitem__(0, False)

    def join(timeout=None):
        assert timeout is not None or not live[0], "Cleanup waited indefinitely for a live download worker"

    process.join.side_effect = join
    reader, writer = Mock(), Mock()
    reader.poll.return_value = True
    reader.recv.side_effect = OSError("download pipe failed")
    context = Mock()
    context.Pipe.return_value = reader, writer
    context.Process.return_value = process
    monkeypatch.setattr(downloads.multiprocessing, "get_context", lambda _method: context)

    with pytest.raises(OSError, match="download pipe failed"):
        service._download_interruptibly("org/model")

    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert not live[0]
    process.close.assert_called_once()
    reader.close.assert_called_once()
    assert writer.close.called
