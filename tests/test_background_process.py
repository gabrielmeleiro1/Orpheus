"""Background tools must keep their output pipes without opening a console."""

import sys

import pytest

from modules.audio.ffmpeg import run_ffmpeg_command


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console behavior")
@pytest.mark.parametrize("cancellable", [False, True])
def test_audio_command_runs_without_console_and_preserves_output(cancellable):
    result = run_ffmpeg_command(
        [sys.executable, "-c", "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"],
        failure_message="console probe failed",
        timeout_seconds=10,
        cancel_requested=(lambda: False) if cancellable else None,
    )
    assert result.stdout.strip() == "0"
    assert result.returncode == 0
