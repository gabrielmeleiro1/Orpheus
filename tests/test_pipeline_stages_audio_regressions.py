"""Regressions for chapter titles, FFmpeg process handling, synthesis, cleaning and packaging."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import typing
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from modules.audio import ffmpeg as ffmpeg_module
from modules.audio.ffmpeg import resolve_ffmpeg_binary, run_ffmpeg_command
from modules.ingestion.models import Chapter
from modules.pipeline.chapter_narration import chapter_announcement
from modules.tts.strategies.base import SynthesisItem, SynthesisResult


def _require_ffmpeg() -> None:
    for name in ("ffmpeg", "ffprobe"):
        if resolve_ffmpeg_binary(name) is None:
            pytest.skip(f"{name} is not installed")


def _write_wav(path: Path, duration_s: float, *, rate: int = 24000, freq: float = 440.0) -> Path:
    samples = np.arange(int(rate * duration_s)) / rate
    pcm = (np.sin(2 * np.pi * freq * samples) * 8000).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm.tobytes())
    return path


# --- Spoken chapter labels -------------------------------------------------


@pytest.mark.parametrize(("title", "expected"), [
    ("Book Civil War", "Book number 3. Civil War."),
    ("Chapter Mild", "Chapter number 3. Mild."),
    ("Chapter Vivid Dreams", "Chapter number 3. Vivid Dreams."),
    ("Part Mid: The Reckoning", "Part number 3. Mid: The Reckoning."),
    # Title-case words spelled with numeral letters stay words even when
    # they are well-formed numerals.
    ("Part Mix", "Part number 3. Mix."),
    ("Book Div", "Book number 3. Div."),
    ("Chapter IIII", "Chapter number 3. IIII."),
    ("BOOK CIVIL WAR", "Book number 3. CIVIL WAR."),
])
def test_words_made_of_numeral_letters_are_not_read_as_numbers(title, expected):
    assert chapter_announcement(Chapter(3, title, "Body.")) == expected


@pytest.mark.parametrize(("title", "expected"), [
    ("Chapter IV. Arrival", "Chapter number 4. Arrival."),
    ("Chapter IV", "Chapter number 4."),
    ("Part ii", "Part number 2."),
    ("Book XII: The End", "Book number 12. The End."),
    ("Chapter I", "Chapter number 1."),
    ("Chapter I. I Am Born", "Chapter number 1. I Am Born."),
    ("Section xlii - Notes", "Section number 42. Notes."),
    ("CHAPTER MIX", "Chapter number 1009."),
    ("Chapter MCMXCIV", "Chapter number 1994."),
])
def test_well_formed_single_case_numerals_are_read_as_numbers(title, expected):
    assert chapter_announcement(Chapter(3, title, "Body.")) == expected


# --- FFmpeg process handling ------------------------------------------------


class _RecordingPopen:
    """Wrap real Popen so tests can inspect the process after the call returns."""

    def __init__(self) -> None:
        self.processes: list[subprocess.Popen] = []
        self._real = subprocess.Popen

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN204
        process = self._real(*args, **kwargs)
        self.processes.append(process)
        return process


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX sleep command")
@pytest.mark.parametrize("outcome", ["cancelled", "timed out"])
def test_abandoned_command_is_reaped_and_its_pipes_closed(outcome):
    recorder = _RecordingPopen()
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    kwargs = (
        {"cancel_requested": stop.is_set}
        if outcome == "cancelled"
        else {"cancel_requested": lambda: False, "timeout_seconds": 0.2}
    )
    with patch.object(ffmpeg_module.subprocess, "Popen", recorder):
        with pytest.raises(RuntimeError, match=outcome):
            run_ffmpeg_command(["sleep", "30"], failure_message="Probe", **kwargs)

    (process,) = recorder.processes
    assert process.returncode is not None
    assert process.stdout.closed and process.stderr.closed


def test_cancel_before_output_collection_closes_pipes():
    process = MagicMock(stdin=None)
    with patch.object(ffmpeg_module.subprocess, "Popen", return_value=process):
        with pytest.raises(RuntimeError, match="cancelled"):
            run_ffmpeg_command(["ffmpeg"], failure_message="Encode", cancel_requested=lambda: True)

    process.kill.assert_called_once()
    process.stdout.close.assert_called_once()
    process.stderr.close.assert_called_once()


def test_output_collection_failure_is_reported_as_ffmpeg_failure():
    process = MagicMock(stdin=None)
    process.communicate.side_effect = OSError("pipe broke")
    with patch.object(ffmpeg_module.subprocess, "Popen", return_value=process):
        with pytest.raises(RuntimeError, match="Encode: pipe broke"):
            run_ffmpeg_command(["ffmpeg"], failure_message="Encode", cancel_requested=lambda: False)

    process.kill.assert_called_once()


class _FakeProbeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = iter(lines)
        self.consumed = 0
        self.closed = False

    def __iter__(self):  # noqa: ANN204
        return self

    def __next__(self) -> str:
        line = next(self._lines)
        self.consumed += 1
        return line

    def close(self) -> None:
        self.closed = True


class _FakeProbeProcess:
    def __init__(self, lines: list[str]) -> None:
        self.stdout = _FakeProbeStdout(lines)
        self.stderr = None
        self.killed = False

    def poll(self):  # noqa: ANN201
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):  # noqa: ANN001, ANN201
        return -9 if self.killed else 0


def _packager():
    from modules.audio.packager import M4BPackager

    with patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
        return M4BPackager()


def test_packet_probe_stops_after_the_last_chapter_boundary(tmp_path):
    packager = _packager()
    process = _FakeProbeProcess([f"{i * 0.02:.6f}\n" for i in range(10_000)])
    with patch("modules.audio.packager.subprocess.Popen", return_value=process):
        boundary_pts, packets_read = packager._probe_concat_packet_starts(
            "/usr/bin/ffprobe", tmp_path / "concat.m4a", {0, 3, 5}
        )

    assert boundary_pts == {0: 0.0, 3: 0.06, 5: 0.1}
    assert packets_read == 6
    assert process.stdout.consumed == 6
    assert process.killed
    assert process.stdout.closed


def test_cancelled_packet_probe_closes_its_pipe(tmp_path):
    packager = _packager()
    release = threading.Event()

    class _BlockingStdout(_FakeProbeStdout):
        def __next__(self) -> str:
            release.wait(5)
            raise StopIteration

    process = _FakeProbeProcess([])
    process.stdout = _BlockingStdout([])
    original_kill = process.kill

    def kill() -> None:
        original_kill()
        release.set()

    process.kill = kill
    with patch("modules.audio.packager.subprocess.Popen", return_value=process):
        with pytest.raises(RuntimeError, match="cancelled"):
            packager._probe_concat_packet_starts(
                "/usr/bin/ffprobe", tmp_path / "concat.m4a", {0}, cancel_requested=lambda: True,
            )

    assert process.killed
    assert process.stdout.closed


def test_packet_probe_boundaries_match_a_full_packet_listing(tmp_path):
    _require_ffmpeg()
    from modules.audio.encoder import AudioEncoder
    from modules.audio.packager import write_ffmpeg_concat_file

    encoder = AudioEncoder()
    chapters = []
    for index, seconds in enumerate((0.3, 0.55, 0.8, 0.45), start=1):
        wav = _write_wav(tmp_path / f"c{index}.wav", seconds, freq=220.0 * index)
        chapters.append(encoder.wav_to_aac(wav, tmp_path / f"c{index}.m4a"))
    concat_list = tmp_path / "concat.txt"
    write_ffmpeg_concat_file(concat_list, chapters)
    joined = tmp_path / "joined.m4a"
    run_ffmpeg_command(
        [resolve_ffmpeg_binary("ffmpeg"), "-y", "-f", "concat", "-safe", "0",
         "-i", str(concat_list), "-vn", "-c:a", "copy", str(joined)],
        failure_message="concat",
    )
    listing = json.loads(run_ffmpeg_command(
        [resolve_ffmpeg_binary("ffprobe"), "-v", "error", "-select_streams", "a",
         "-show_entries", "packet=pts_time", "-of", "json", str(joined)],
        failure_message="probe",
    ).stdout)["packets"]
    all_pts = [float(packet["pts_time"]) for packet in listing]

    boundaries = {0, 9, 23, 40}
    recorder = _RecordingPopen()
    with patch("modules.audio.packager.subprocess.Popen", recorder):
        boundary_pts, _ = _packager_real()._probe_concat_packet_starts(
            resolve_ffmpeg_binary("ffprobe"), joined, boundaries,
        )

    assert boundary_pts == {index: all_pts[index] for index in boundaries}
    (process,) = recorder.processes
    assert process.returncode is not None
    assert process.stdout.closed


def _packager_real():
    from modules.audio.packager import M4BPackager

    return M4BPackager()


# --- Encoding and packaging settings -----------------------------------------


def test_chapter_mp3_is_encoded_at_the_pipeline_sample_rate(tmp_path, monkeypatch):
    import modules.audio.encoder as encoder_module
    from modules.pipeline.models import PipelineStage
    from modules.pipeline.stages import encoding

    created: list[dict] = []

    class FakeEncoder:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

        def wav_to_mp3(self, _input, output_path, **_kwargs) -> Path:
            Path(output_path).write_bytes(b"mp3")
            return Path(output_path)

    monkeypatch.setattr(encoder_module, "AudioEncoder", FakeEncoder)
    context = encoding.EncodingStageContext(
        config=SimpleNamespace(normalize_volume=False, mp3_bitrate="96k"),
        sample_rate=22050,
        encoding_stage=PipelineStage.ENCODING,
        set_stage=lambda _stage: None,
        notify_progress=lambda **_kwargs: None,
        log_verbose=lambda *_args: None,
    )
    wav = _write_wav(tmp_path / "chapter.wav", 0.1, rate=22050)
    encoding.encode_chapter_audio(
        context, SimpleNamespace(number=1, title="One"), 0, 1, tmp_path, wav,
    )

    assert created == [{"sample_rate": 22050}]


def test_progress_callback_type_matches_the_emitted_arguments():
    from modules.pipeline.models import PipelineStage, ProgressCallback
    from modules.pipeline.progress import PipelineProgressTracker

    declared = typing.get_args(ProgressCallback)[0]
    received: list[tuple] = []
    tracker = PipelineProgressTracker.__new__(PipelineProgressTracker)
    tracker.notify(
        callback=lambda *args: received.append(args), stage=PipelineStage.CLEANING,
        chapter_idx=1, total_chapters=2, chunk_idx=3, total_chunks=4,
        message="m", performance={"x": 1},
    )

    assert len(received[0]) == len(declared)
    assert declared[-1] == typing.Optional[dict[str, typing.Any]]


def test_reencoded_m4b_uses_the_requested_aac_bitrate(tmp_path):
    packager = _packager()
    wav = _write_wav(tmp_path / "one.wav", 0.1)
    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN202
        commands.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"m4b")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("modules.audio.packager.run_ffmpeg_command", side_effect=fake_run):
        from modules.audio.packager import AudiobookMetadata

        packager.create_m4b(
            [wav], ["One"], AudiobookMetadata("T", "A"), tmp_path / "book.m4b",
            chapter_durations_ms=[100], bitrate="64k",
        )

    final = commands[-1]
    assert final[final.index("-b:a") + 1] == "64k"


@pytest.mark.parametrize("mode", ["audiobook", "document_audio"])
def test_packaging_forwards_the_configured_bitrate(tmp_path, mode):
    from modules.pipeline.models import ChapterResult
    from modules.pipeline.stages import packaging

    wav = _write_wav(tmp_path / "one.wav", 0.1)
    result = ChapterResult(1, "One", duration_ms=100)
    result.wav_path = wav
    runtime = SimpleNamespace(
        config=SimpleNamespace(voice="af_heart", conversion_mode=mode, mp3_bitrate="96k"),
        state=SimpleNamespace(source_path=None),
    )
    with patch("modules.audio.packager.M4BPackager") as m4b, \
            patch("modules.audio.document_packager.DocumentAudioPackager") as m4a:
        m4b.return_value.chapter_markers = []
        packaging.package_chapters(runtime, [result], "Title", "Author", tmp_path)

    packager = m4a if mode == "document_audio" else m4b
    method = packager.return_value.create_m4a if mode == "document_audio" else packager.return_value.create_m4b
    assert method.call_args.kwargs["bitrate"] == "96k"


# --- Cover art ---------------------------------------------------------------


def _package_with_cover(tmp_path: Path, cover_bytes: bytes, media_type: str) -> Path:
    from modules.pipeline.models import ChapterResult
    from modules.pipeline.stages import packaging

    wav = _write_wav(tmp_path / "one.wav", 0.3)
    result = ChapterResult(1, "One", duration_ms=300)
    result.wav_path = wav
    runtime = SimpleNamespace(
        config=SimpleNamespace(voice="af_heart"),
        state=SimpleNamespace(source_path=tmp_path / "book.epub"),
        check_cancelled=lambda: False,
    )
    cover = SimpleNamespace(data=cover_bytes, media_type=media_type)
    with patch("modules.ingestion.cover_art.extract_cover", return_value=cover):
        return packaging.package_chapters(runtime, [result], "Title", "Author", tmp_path)


def _video_streams(path: Path) -> list[dict]:
    return json.loads(run_ffmpeg_command(
        [resolve_ffmpeg_binary("ffprobe"), "-v", "error", "-select_streams", "v",
         "-show_streams", "-of", "json", str(path)],
        failure_message="probe",
    ).stdout).get("streams", [])


def test_unreadable_cover_is_dropped_and_the_book_still_packages(tmp_path):
    _require_ffmpeg()
    output = _package_with_cover(tmp_path, b"\xff\xd8\xff\xe0not a jpeg" * 20, "image/jpeg")

    assert output.is_file()
    assert _video_streams(output) == []


def test_readable_cover_is_still_attached(tmp_path):
    _require_ffmpeg()
    png = tmp_path / "src.png"
    run_ffmpeg_command(
        [resolve_ffmpeg_binary("ffmpeg"), "-y", "-f", "lavfi", "-i", "color=c=red:s=32x32",
         "-frames:v", "1", str(png)],
        failure_message="make png",
    )
    output = _package_with_cover(tmp_path, png.read_bytes(), "image/png")

    streams = _video_streams(output)
    assert len(streams) == 1
    assert streams[0]["disposition"]["attached_pic"] == 1


def test_packaging_failure_unrelated_to_the_cover_still_surfaces(tmp_path):
    from modules.pipeline.models import ChapterResult
    from modules.pipeline.stages import packaging

    wav = _write_wav(tmp_path / "one.wav", 0.1)
    result = ChapterResult(1, "One", duration_ms=100)
    result.wav_path = wav
    runtime = SimpleNamespace(
        config=SimpleNamespace(voice="af_heart"),
        state=SimpleNamespace(source_path=tmp_path / "book.epub"),
        check_cancelled=lambda: False,
    )
    cover = SimpleNamespace(data=b"img", media_type="image/png")
    with patch("modules.ingestion.cover_art.extract_cover", return_value=cover), \
            patch("modules.audio.packager.cover_image_is_usable", return_value=True), \
            patch("modules.audio.packager.M4BPackager") as m4b:
        m4b.return_value.create_m4b.side_effect = RuntimeError("M4B creation failed: disk full")
        with pytest.raises(RuntimeError, match="disk full"):
            packaging.package_chapters(runtime, [result], "Title", "Author", tmp_path)

    assert m4b.return_value.create_m4b.call_count == 1


# --- Incremental synthesis ---------------------------------------------------


def _synthesis_runtime(tmp_path: Path):
    return SimpleNamespace(
        config=SimpleNamespace(temp_dir=tmp_path),
        track_temp_cleanup_path=lambda _path: None,
        tts_sample_rate=lambda: 24000,
        check_cancelled=lambda: False,
        log_verbose=lambda *_args: None,
    )


def _run_incremental(tmp_path, results, retry=None):
    from modules.pipeline.stages.synthesis import _synthesize_chapter_chunks_incrementally

    produced: list[int] = []

    def iter_batch(items, progress_callback=None):  # noqa: ANN001, ANN202
        for result in results:
            produced.append(result.index)
            yield result

    hooks = SimpleNamespace(retry_batch_results_serially=retry or MagicMock())
    items = [SynthesisItem(text=str(i), voice="v", index=i) for i in range(len(results))]
    output = _synthesize_chapter_chunks_incrementally(
        _synthesis_runtime(tmp_path), hooks,
        chapter=SimpleNamespace(number=1), batch_items=items, on_batch_progress=None,
        total_chunks=len(items), iter_synthesize_batch=iter_batch,
    )
    return output, produced, hooks


def test_failed_first_chunk_stops_synthesis_instead_of_buffering_the_rest(tmp_path):
    later = [SynthesisResult(audio=np.ones(8, dtype=np.float32), index=i) for i in range(1, 6)]
    results = [SynthesisResult(audio=None, index=0, error="boom"), *later]

    output, produced, hooks = _run_incremental(tmp_path, results)

    assert output is None
    assert produced == [0, 1]
    assert later[0].audio is None
    hooks.retry_batch_results_serially.assert_not_called()


def test_all_failed_chunks_still_retry_serially(tmp_path):
    results = [SynthesisResult(audio=None, index=i, error="boom") for i in range(3)]
    retried = [SynthesisResult(audio=np.full(8, 0.1, dtype=np.float32), index=i) for i in range(3)]
    retry = MagicMock(return_value=retried)

    output, produced, _hooks = _run_incremental(tmp_path, results, retry=retry)

    assert produced == [0, 1, 2]
    retry.assert_called_once()
    assert isinstance(output, np.ndarray) and output.size == 24


# --- Cleaning cancellation -----------------------------------------------------


@pytest.mark.parametrize("backend", ["mlx", "api"])
def test_cancelled_batch_cleaning_does_not_fall_back_to_per_chunk_cleaning(backend):
    from modules.pipeline.stages import cleaning

    logs: list[tuple[str, str]] = []
    runtime = SimpleNamespace(
        check_cancelled=lambda: True,
        log_verbose=lambda message, level: logs.append((message, level)),
        config=SimpleNamespace(save_cleaning_debug=False),
    )

    def fail(*_args, **_kwargs):  # noqa: ANN202
        raise RuntimeError("stopped")

    cleaner = SimpleNamespace(
        config=SimpleNamespace(backend=backend),
        clean_chunks_with_trace=fail,
        clean_batch_with_trace_async=fail,
    )
    hooks = MagicMock()
    result = cleaning.clean_chunks_with_trace(runtime, hooks, 0, 1, ["a", "b"], cleaner)

    assert result[-1] is True
    assert not any("falling back" in message for message, _level in logs)
    hooks.clean_chunk_safely.assert_not_called()
