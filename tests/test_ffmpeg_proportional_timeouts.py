"""FFmpeg deadlines scale with the audio a call processes; the env override still wins."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from modules.audio import encoder as encoder_module
from modules.audio import ffmpeg
from modules.audio.packager import expected_audio_seconds


def _wav(path: Path, seconds: float, rate: int = 24000) -> Path:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(np.zeros(int(seconds * rate), dtype=np.int16).tobytes())
    return path


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("AUDIOBOOK_FFMPEG_TIMEOUT_SECONDS", raising=False)


def test_known_audio_length_sets_a_proportional_deadline_with_a_floor():
    assert ffmpeg.ffmpeg_timeout_seconds(audio_seconds=10) == 120.0
    assert ffmpeg.ffmpeg_timeout_seconds(audio_seconds=0) == 120.0
    assert ffmpeg.ffmpeg_timeout_seconds(audio_seconds=10 * 3600) == 5 * 3600
    # Unknown length keeps the fixed default.
    assert ffmpeg.ffmpeg_timeout_seconds() == 2 * 60 * 60


def test_explicit_timeout_and_env_override_take_precedence(monkeypatch):
    assert ffmpeg.ffmpeg_timeout_seconds(7, audio_seconds=10 * 3600) == 7
    monkeypatch.setenv("AUDIOBOOK_FFMPEG_TIMEOUT_SECONDS", "42")
    assert ffmpeg.ffmpeg_timeout_seconds(audio_seconds=10 * 3600) == 42


def test_wav_encode_deadline_follows_the_input_length(monkeypatch, tmp_path):
    seen: list[float] = []

    def fake_run(cmd, *, failure_message, timeout_seconds=None, cancel_requested=None, audio_seconds=None):
        seen.append(ffmpeg.ffmpeg_timeout_seconds(timeout_seconds, audio_seconds=audio_seconds))
        Path(cmd[-1]).write_bytes(b"aac")

    monkeypatch.setattr(encoder_module, "run_ffmpeg_command", fake_run)
    monkeypatch.setattr(encoder_module, "require_ffmpeg_binary", lambda name: name)
    source = _wav(tmp_path / "chapter.wav", seconds=1_000)

    encoder_module.AudioEncoder().wav_to_aac(source, tmp_path / "chapter.m4a")

    assert seen == [500.0]


def test_book_length_comes_from_durations_or_wav_headers(tmp_path):
    first = _wav(tmp_path / "a.wav", seconds=2)
    second = _wav(tmp_path / "b.wav", seconds=3)
    compressed = tmp_path / "c.m4a"
    compressed.write_bytes(b"not probed")

    assert expected_audio_seconds([first, second]) == pytest.approx(5.0)
    assert expected_audio_seconds([first, compressed], [1500, 2500]) == pytest.approx(4.0)
    assert expected_audio_seconds([first, compressed]) is None
