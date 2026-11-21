"""Exercise audio subprocess boundaries with installed FFmpeg binaries."""

import json
import os
import time
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from modules.audio.encoder import AudioEncoder
from modules.audio.ffmpeg import resolve_ffmpeg_binary, run_ffmpeg_command
from modules.pipeline.models import ChapterResult
from modules.pipeline.stages.packaging import package_chapters

pytestmark = [pytest.mark.integration, pytest.mark.audio, pytest.mark.ffmpeg]


@pytest.fixture
def encoder(monkeypatch):
    for name in ("ffmpeg", "ffprobe"):
        if resolve_ffmpeg_binary(name) is None:
            pytest.skip(f"{name} is not installed")
    monkeypatch.setenv("AUDIOBOOK_FFMPEG_TIMEOUT_SECONDS", "5")
    return AudioEncoder(sample_rate=24000)


def _write_wav(path, duration_ms=240):
    samples = np.arange(24 * duration_ms) / 24000
    pcm = (np.sin(2 * np.pi * 440 * samples) * 8000).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(pcm.tobytes())


@pytest.mark.parametrize("suffix,codec", [("mp3", "mp3"), ("m4a", "aac")])
def test_encoding_preserves_audio_with_unicode_and_quoted_paths(
    tmp_path, encoder, suffix, codec
):
    folder = tmp_path / "音声 author's chapters"
    folder.mkdir()
    # A double quote cannot occur in a Windows filename.
    quote = '"' if os.name != "nt" else "“"
    wav = folder / f"entrée {quote}one{quote}.wav"
    _write_wav(wav)
    output = wav.with_suffix(f".{suffix}")
    method = encoder.wav_to_mp3 if suffix == "mp3" else encoder.wav_to_aac
    assert method(wav, output) == output
    assert encoder.get_duration(output) == pytest.approx(0.24, abs=0.10)
    probe = run_ffmpeg_command(
        [resolve_ffmpeg_binary("ffprobe"), "-v", "error", "-show_streams",
         "-of", "json", str(output)],
        failure_message="Audio probe failed",
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == codec
    assert int(stream["sample_rate"]) == 24000
    assert stream["channels"] == 1


@pytest.mark.parametrize("source", ["wav", "aac", "mp3"])
def test_packaged_chapter_starts_match_recorded_offsets(tmp_path, encoder, source):
    folder = tmp_path / "日本語 author's book"
    folder.mkdir()
    results = []
    for index, duration in enumerate((240, 360, 480), start=1):
        wav = folder / f"chapter '{index}'.wav"
        _write_wav(wav, duration)
        result = ChapterResult(index, f"章 {index}: author's story", duration_ms=duration)
        if source == "wav":
            result.wav_path = wav
        elif source == "aac":
            result.aac_path = encoder.wav_to_aac(wav, wav.with_suffix(".m4a"))
        else:
            result.mp3_path = encoder.wav_to_mp3(wav, wav.with_suffix(".mp3"))
        results.append(result)
    runtime = SimpleNamespace(config=SimpleNamespace(voice="af_heart"))
    output = package_chapters(runtime, results, "旅er's story", "Author", folder)
    assert output.suffix == ".m4b"
    assert encoder.get_duration(output) > 1
    probe = run_ffmpeg_command(
        [resolve_ffmpeg_binary("ffprobe"), "-v", "error", "-show_chapters",
         "-of", "json", str(output)],
        failure_message="Chapter probe failed",
    )
    markers = json.loads(probe.stdout)["chapters"]
    assert len(markers) == 3
    assert [marker["tags"]["title"] for marker in markers] == [r.chapter_title for r in results]
    starts = [r.packaged_start_ms for r in results]
    assert starts[0] == 0
    assert all(right > left for left, right in zip(starts, starts[1:]))
    assert [int(float(marker["start_time"]) * 1000) for marker in markers] == starts


@pytest.mark.parametrize("mode", ["timeout", "polled_timeout", "cancel"])
def test_unbounded_audio_process_stops_on_deadline_or_cancellation(encoder, mode):
    started = time.monotonic()
    def cancel():
        return mode == "cancel" and time.monotonic() - started >= 0.15
    with pytest.raises(RuntimeError, match="cancelled" if mode == "cancel" else "timed out"):
        run_ffmpeg_command(
            [encoder.ffmpeg_path, "-nostdin", "-v", "error", "-re", "-f", "lavfi",
             "-i", "anullsrc=r=24000:cl=mono", "-f", "null", "-"],
            failure_message="Audio generation failed",
            timeout_seconds=2 if mode == "cancel" else 0.2,
            cancel_requested=None if mode == "timeout" else cancel,
        )
    assert time.monotonic() - started < 4


def test_failure_message_keeps_only_the_tail_of_stderr(encoder, tmp_path):
    corrupt = tmp_path / "corrupt.wav"
    corrupt.write_bytes(b"RIFF" + os.urandom(4096))

    with pytest.raises(RuntimeError) as raised:
        run_ffmpeg_command(
            [encoder.ffmpeg_path, "-loglevel", "debug", "-i", str(corrupt), "-f", "null", "-"],
            failure_message="Audio probe failed",
        )

    message = str(raised.value)
    assert message.startswith("Audio probe failed: ")
    assert len(message) <= len("Audio probe failed: ") + 2003


def test_ffmpeg_failure_keeps_short_stderr_verbatim():
    from modules.audio.ffmpeg import ffmpeg_failure

    assert str(ffmpeg_failure("Encode failed", "  disk full\n")) == "Encode failed: disk full"
    long_error = ffmpeg_failure("Encode failed", "banner\n" * 1000 + "real cause")
    assert str(long_error).endswith("real cause")
    assert "..." in str(long_error)
