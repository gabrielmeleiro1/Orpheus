"""Cheaper chapter packaging, narration and warmup paths keep their output."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from modules.ingestion.models import Chapter
from modules.tts.strategies.base import SynthesisResult


# --- M4B stream-copy packet counts --------------------------------------------


def _packager():
    from modules.audio.packager import M4BPackager

    with patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
        return M4BPackager()


class _PacketProbe:
    """Popen stand-in streaming one pts_time line per packet."""

    def __init__(self, pts: list[float]) -> None:
        self.stdout = iter(f"{value:.6f}\n" for value in pts)
        self.stderr = None

    def wait(self, timeout=None):  # noqa: ANN001, ANN201
        return 0

    def poll(self):  # noqa: ANN201
        return 0

    def kill(self) -> None:
        return None


def _probe_offsets(header_counts, counted, concat_frames, pts):
    """Run the stream-copy offset probe against scripted ffprobe answers."""
    commands: list[list[str]] = []
    headers = iter(header_counts)
    counts = iter(counted)

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN202
        commands.append(list(cmd))
        if "-count_packets" in cmd:
            return SimpleNamespace(stdout=f"{next(counts)}\n")
        if "stream=nb_frames" in cmd:
            return SimpleNamespace(stdout=f"{next(headers)}\n")
        return SimpleNamespace(stdout=f"{concat_frames}\n{len(pts) * 0.04:.6f}\n")

    packager = _packager()
    with patch("modules.audio.packager.run_ffmpeg_command", side_effect=fake_run), patch(
        "modules.audio.packager.subprocess.Popen", return_value=_PacketProbe(pts),
    ):
        offsets = packager._probe_stream_copy_offsets_ms(
            Path("concat.m4a"), [Path(f"{i}.m4a") for i in range(len(counted))],
        )
    return offsets, commands


def test_header_packet_counts_skip_the_full_demux_when_they_add_up():
    pts = [i * 0.04 for i in range(9)]
    offsets, commands = _probe_offsets([2, 3, 4], [2, 3, 4], 9, pts)

    assert offsets == ([0, 80, 200], 360)
    assert not any("-count_packets" in cmd for cmd in commands)
    assert sum("stream=nb_frames" in cmd for cmd in commands) == 3
    assert sum("stream=nb_frames:format=duration" in cmd for cmd in commands) == 1


@pytest.mark.parametrize(
    ("headers", "concat_frames"),
    [
        ([2, 4, 4], 9),  # a chapter header over-reports its packets
        (["N/A", 3, 4], 9),  # a header without a sample count
        ([2, 3, 4], "N/A"),  # the joined file without a sample count
    ],
)
def test_disagreeing_or_missing_header_counts_fall_back_to_counting(headers, concat_frames):
    pts = [i * 0.04 for i in range(9)]
    offsets, commands = _probe_offsets(headers, [2, 3, 4], concat_frames, pts)

    # Markers come from the demuxed counts, never from the bad header.
    assert offsets == ([0, 80, 200], 360)
    assert sum("-count_packets" in cmd for cmd in commands) == 3


def _require_ffmpeg() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not (ffmpeg and ffprobe):
        pytest.skip("ffmpeg/ffprobe required")
    return ffmpeg, ffprobe


def _write_pcm16(path: Path, samples: np.ndarray, sample_rate: int = 24000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def test_header_counted_m4b_is_byte_identical_to_a_demux_counted_one(tmp_path):
    ffmpeg, ffprobe = _require_ffmpeg()
    from modules.audio.encoder import AudioEncoder
    from modules.audio.packager import AudiobookMetadata, M4BPackager

    rng = np.random.default_rng(7)
    encoder = AudioEncoder(bitrate="64k")
    chapters = []
    # Includes chapters shorter than one AAC frame and seam-heavy short ones.
    for index, seconds in enumerate((1.3, 0.021, 4.7, 0.05, 2.2), start=1):
        wav = _write_pcm16(tmp_path / f"c{index}.wav", rng.uniform(-0.3, 0.3, int(seconds * 24000)))
        chapters.append(encoder.wav_to_aac(wav, tmp_path / f"c{index}.m4a"))
    cover = tmp_path / "cover.png"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=64x64:d=1",
         "-frames:v", "1", str(cover)],
        check=True,
    )
    metadata = AudiobookMetadata(title="Book", author="Author", narrator="N", cover_path=cover)
    names = [f"Chapter {i}" for i in range(1, len(chapters) + 1)]

    counted_calls: list[bool] = []
    real_probe = M4BPackager._probe_segment_packet_counts

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        counted_calls.append(not kwargs.get("header_only", False))
        return real_probe(*args, **kwargs)

    def demux_counts(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if kwargs.get("header_only"):
            raise ValueError("header counts unavailable")
        return real_probe(*args, **kwargs)

    with patch.object(M4BPackager, "_probe_segment_packet_counts", staticmethod(demux_counts)):
        demuxed = M4BPackager()
        demuxed.create_m4b(chapters, names, metadata, tmp_path / "counted.m4b")
    with patch.object(M4BPackager, "_probe_segment_packet_counts", staticmethod(spy)):
        fast = M4BPackager()
        fast.create_m4b(chapters, names, metadata, tmp_path / "header.m4b")

    assert counted_calls == [False]
    assert fast.chapter_markers == demuxed.chapter_markers
    assert (tmp_path / "header.m4b").read_bytes() == (tmp_path / "counted.m4b").read_bytes()


# --- Single-pass chapter narration ---------------------------------------------


_INTRO_TEXT_PREFIXES = ("Chapter number", "Section number")


class _FakeEngine:
    """Deterministic TTS engine for differential chapter builds."""

    is_loaded = True

    def __init__(
        self,
        *,
        incremental: bool,
        file_backed: bool,
        chunk_samples: list[int],
        intro: str = "ok",
        sample_rate: int = 24000,
    ) -> None:
        self.supports_incremental_batch_results = incremental
        self.supports_file_batch_results = file_backed
        self.chunk_samples = chunk_samples
        self.intro = intro
        self.sample_rate = sample_rate
        self.spoken: list[str] = []

    def synthesize(self, *, text, voice, speed):  # noqa: ANN001, ANN201
        self.spoken.append(text)
        if text.startswith(_INTRO_TEXT_PREFIXES):
            if self.intro == "raise":
                raise RuntimeError("announcement backend failed")
            if self.intro == "empty":
                return np.zeros(0, dtype=np.float32)
            rng = np.random.default_rng(len(text))
            return rng.uniform(-1.1, 1.1, 7_919).astype(np.float32)
        raise AssertionError(f"unexpected single synthesis: {text!r}")

    def _result(self, item) -> SynthesisResult:  # noqa: ANN001
        rng = np.random.default_rng(1000 + item.index)
        # Loud audio, clipped beyond full scale, so PCM16 rounding is exercised.
        audio = rng.uniform(-1.2, 1.2, self.chunk_samples[item.index]).astype(np.float32)
        if item.output_path is not None:
            return SynthesisResult(
                audio=None, index=item.index,
                audio_path=_write_pcm16(Path(item.output_path), audio, self.sample_rate),
            )
        return SynthesisResult(audio=audio, index=item.index)

    def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001, ANN201
        for completed, item in enumerate(items, start=1):
            result = self._result(item)
            if progress_callback:
                progress_callback(completed, len(items))
            yield result

    def synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001, ANN201
        return list(self.iter_synthesize_batch(items, progress_callback))


def _build_chapter(tmp_path: Path, *, frame_enabled: bool, engine: _FakeEngine,
                   max_in_memory: int, chapter: Chapter, cancel_after_body: bool = False):
    from modules.pipeline import ConversionPipeline, PipelineConfig

    config = PipelineConfig(
        output_dir=tmp_path / "out", temp_dir=tmp_path / "temp",
        enable_tts_file_batch_results=engine.supports_file_batch_results,
    )
    config.max_in_memory_chapter_samples = max_in_memory
    pipeline = ConversionPipeline(config)
    processor = pipeline.stages.chapter_processor
    processor.runtime.set_tts_engine(engine)
    chunks = [f"Chunk {index}." for index in range(len(engine.chunk_samples))]

    real_synthesize = processor.synthesize_chapter_chunks

    def synthesize(**kwargs):  # noqa: ANN003, ANN202
        output = real_synthesize(**kwargs)
        if cancel_after_body:
            processor.runtime.state.cancelled = True
        return output

    tracked: list[RMSBlock] = []
    patches = [
        patch.object(processor, "ensure_chapter_tts_engine"),
        patch.object(processor, "chunk_chapter_content", return_value=chunks),
        patch.object(processor, "clean_chapter_content", return_value=(chunks, False)),
        patch.object(processor, "synthesize_chapter_chunks", side_effect=synthesize),
        _record_rms_blocks(tracked),
    ]
    if not frame_enabled:
        patches.append(
            patch("modules.pipeline.chapter_narration.prepare_chapter_frame", return_value=None)
        )
    for active in patches:
        active.start()
    try:
        result, output, cancelled = processor.prepare_chapter_audio(
            chapter=chapter, chapter_idx=0, total_chapters=1, output_dir=tmp_path / "out",
        )
    finally:
        for active in reversed(patches):
            active.stop()
    narrated_blocks = [block for block in tracked if block.path.name.endswith("_narrated.wav")]
    return SimpleNamespace(
        result=result,
        output=output,
        cancelled=cancelled,
        data=output.read_bytes() if isinstance(output, Path) else None,
        blocks=[(block.size, block.square_sum) for block in narrated_blocks],
        temp_files=sorted(p.name for p in (tmp_path / "temp").glob("chapter_*")),
    )


class RMSBlock(SimpleNamespace):
    pass


def _record_rms_blocks(tracked: list[RMSBlock]):
    from modules.pipeline.stages.synthesis import ChapterAudioAccumulator

    real_track = ChapterAudioAccumulator._track_pcm16

    def track(self, pcm16):  # noqa: ANN001, ANN202
        if pcm16.size:
            tracked.append(RMSBlock(
                path=self.output_path,
                size=int(pcm16.size),
                square_sum=float(np.sum(np.square(pcm16, dtype=np.float64))),
            ))
        return real_track(self, pcm16)

    return patch.object(ChapterAudioAccumulator, "_track_pcm16", track)


# 70_000 + 150_001 + ... crosses several 65_536-frame blocks at odd offsets.
_CHUNKS = [70_000, 150_001, 3, 65_536, 99_999]


@pytest.mark.parametrize("incremental", [True, False])
@pytest.mark.parametrize(
    ("storage", "max_in_memory"),
    [("memory", 10_000_000), ("spilled", 100_000), ("spilled_first", 1), ("chunk_files", 10_000_000)],
)
@pytest.mark.parametrize("title", ["The Coast", "", "Chapter 3"])
def test_single_pass_narration_matches_the_two_pass_build(
    tmp_path, incremental, storage, max_in_memory, title,
):
    chapter = Chapter(3, title, "Chunk 0. Chunk 1.")
    builds = {}
    for name, frame_enabled in (("two_pass", False), ("single_pass", True)):
        engine = _FakeEngine(
            incremental=incremental, file_backed=storage == "chunk_files", chunk_samples=_CHUNKS,
        )
        builds[name] = _build_chapter(
            tmp_path / name, frame_enabled=frame_enabled, engine=engine,
            max_in_memory=max_in_memory, chapter=chapter,
        )
        builds[name].spoken = engine.spoken

    old, new = builds["two_pass"], builds["single_pass"]
    assert old.result.error is None and new.result.error is None
    assert not old.cancelled and not new.cancelled
    assert new.output.name == old.output.name == "chapter_03_narrated.wav"
    assert new.data == old.data
    assert new.result.pcm_rms == old.result.pcm_rms
    assert new.blocks == old.blocks
    assert new.spoken == old.spoken  # one announcement, same text
    # No intermediate body WAV is written next to the narrated chapter.
    assert new.temp_files == ["chapter_03_narrated.wav"]


@pytest.mark.parametrize("incremental", [True, False])
def test_in_memory_concat_failure_streams_the_same_narrated_chapter(tmp_path, incremental):
    from modules.pipeline.stages import synthesis

    real_concatenate = np.concatenate

    def concatenate(arrays, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if all(np.asarray(array).dtype == np.float32 for array in arrays):
            raise MemoryError("no room for the chapter")
        return real_concatenate(arrays, *args, **kwargs)

    builds = {}
    for name, frame_enabled in (("two_pass", False), ("single_pass", True)):
        engine = _FakeEngine(incremental=incremental, file_backed=False, chunk_samples=_CHUNKS)
        with patch.object(synthesis.np, "concatenate", side_effect=concatenate):
            builds[name] = _build_chapter(
                tmp_path / name, frame_enabled=frame_enabled, engine=engine,
                max_in_memory=10_000_000, chapter=Chapter(1, "Arrival", "Body."),
            )

    old, new = builds["two_pass"], builds["single_pass"]
    assert new.result.error is None
    assert new.data == old.data
    assert new.result.pcm_rms == old.result.pcm_rms
    assert new.blocks == old.blocks


@pytest.mark.parametrize("intro", ["raise", "empty"])
@pytest.mark.parametrize("storage_max", [10_000_000, 100_000])
def test_failed_announcement_still_fails_the_chapter_after_its_body(tmp_path, intro, storage_max):
    builds = {}
    for name, frame_enabled in (("two_pass", False), ("single_pass", True)):
        engine = _FakeEngine(
            incremental=True, file_backed=False, chunk_samples=_CHUNKS, intro=intro,
        )
        builds[name] = _build_chapter(
            tmp_path / name, frame_enabled=frame_enabled, engine=engine,
            max_in_memory=storage_max, chapter=Chapter(1, "Arrival", "Body."),
        )
        builds[name].spoken = engine.spoken

    old, new = builds["two_pass"], builds["single_pass"]
    assert old.result.error and new.result.error == old.result.error
    assert new.output is None and old.output is None
    assert new.cancelled is old.cancelled is False
    # The body is still synthesized and the announcement retried after it.
    assert new.spoken[-1] == old.spoken[-1]
    assert new.temp_files == old.temp_files


def test_cancel_after_the_body_reports_a_cancelled_chapter(tmp_path):
    for name, frame_enabled in (("two_pass", False), ("single_pass", True)):
        engine = _FakeEngine(incremental=True, file_backed=False, chunk_samples=_CHUNKS)
        build = _build_chapter(
            tmp_path / name, frame_enabled=frame_enabled, engine=engine,
            max_in_memory=100_000, chapter=Chapter(1, "Arrival", "Body."),
            cancel_after_body=True,
        )
        assert build.result.error == "Cancelled"
        assert build.output is None
        assert build.cancelled is True


def test_cancelled_before_synthesis_defers_the_announcement():
    from modules.pipeline.chapter_narration import prepare_chapter_frame

    runtime = SimpleNamespace(
        check_cancelled=lambda: True,
        synthesize_speech=lambda **_kwargs: pytest.fail("announcement synthesized after cancel"),
    )
    assert prepare_chapter_frame(runtime, Chapter(1, "A", "Body")) is None


def test_long_chapter_body_is_scanned_once(tmp_path):
    from modules.pipeline.stages import synthesis

    copied: list[Path] = []
    real_copy = synthesis._write_wav_file_segment

    def copy(writer, audio_path, sample_rate):  # noqa: ANN001, ANN202
        copied.append(Path(audio_path))
        return real_copy(writer, audio_path, sample_rate)

    engine = _FakeEngine(incremental=True, file_backed=False, chunk_samples=_CHUNKS)
    with patch.object(synthesis, "_write_wav_file_segment", side_effect=copy):
        build = _build_chapter(
            tmp_path, frame_enabled=True, engine=engine, max_in_memory=100_000,
            chapter=Chapter(1, "Arrival", "Body."),
        )

    assert build.result.error is None
    assert copied == []  # the body WAV is never re-read into a second file
    assert sum(size for size, _ in build.blocks) == sum(_CHUNKS) + 7_919 + int(3.0 * 24000)


# --- Warmup calibration before a sequential fallback ---------------------------


_GIB = 1024**3


class _DispatchHarness:
    """Dispatch runtime and runner whose live memory follows the parent's models."""

    def __init__(self, *, total_gib: float, free_gib: float, model_gib: float = 3.0,
                 cancel_after_warmup: bool = False) -> None:
        from modules.pipeline.memory_policy import MemorySnapshot, SafeMemoryPolicy
        from modules.pipeline.models import WorkerPlan

        self.calls: list[str] = []
        self.models_loaded = False
        self.samples_with_models: list[bool] = []
        self.cancel_after_warmup = cancel_after_warmup
        self.cancelled = False
        self.selected = None

        def sample():  # noqa: ANN202
            self.samples_with_models.append(self.models_loaded)
            free = free_gib - (model_gib if self.models_loaded else 0.0)
            return MemorySnapshot(total_bytes=int(total_gib * _GIB), available_bytes=int(free * _GIB))

        self.policy = SafeMemoryPolicy(memory_sampler=sample, estimated_worker_bytes=2 * _GIB)
        self.config = SimpleNamespace(
            worker_plan=WorkerPlan(
                requested_workers=4, max_workers=4, enable_parallel=True,
                worker_mode="auto", worker_auto_mode="memory_safe", process_safe=False,
            ),
            memory_policy=self.policy,
        )
        self.state = SimpleNamespace(total_chars=800_000)
        self.is_parallel = True

    # runtime
    def check_cancelled(self) -> bool:
        return self.cancelled

    def log_verbose(self, *_args) -> None:  # noqa: ANN002
        return None

    def parallel_config_max_workers(self, fallback=1):  # noqa: ANN001, ANN201
        return 4

    def set_parallel_execution_active(self, _active) -> None:  # noqa: ANN001
        return None

    def set_selected_parallel_chapters(self, count) -> None:  # noqa: ANN001
        self.selected = count

    def set_selected_parallel_worker_count_reason(self, _reason) -> None:  # noqa: ANN001
        return None

    # runner
    def warmup_models(self) -> None:
        self.calls.append("warmup")
        self.policy.record_baseline()
        self.models_loaded = True
        self.policy.record_warmup()
        self.cancelled = self.cancel_after_warmup

    def cleanup_models(self, *, force=False) -> None:  # noqa: ANN001
        self.calls.append(f"cleanup(force={force})")
        self.models_loaded = False

    def process_chapters_sequential(self, chapters, _output_dir):  # noqa: ANN001, ANN201
        self.calls.append(f"sequential(models_loaded={self.models_loaded})")
        return ["sequential"]

    def process_chapters_parallel(self, chapters, _output_dir):  # noqa: ANN001, ANN201
        self.calls.append(f"parallel(models_loaded={self.models_loaded})")
        return ["parallel"]

    def dispatch(self, tmp_path):  # noqa: ANN001, ANN201
        from modules.pipeline.chapter_dispatch import ChapterDispatchContext, dispatch_chapters

        chapters = [Chapter(i, f"C{i}", "x" * 200_000) for i in range(1, 5)]
        with patch("modules.pipeline.chapter_dispatch.shutdown_shared_worker_pool"):
            return dispatch_chapters(
                ChapterDispatchContext(runtime=self, runner=self), chapters, tmp_path,
            )


def test_warm_models_are_kept_when_no_free_memory_could_fit_a_second_worker(tmp_path):
    # 8 GiB device, 4 GiB reserve: a 4.5 GiB worker never fits beside the first.
    harness = _DispatchHarness(total_gib=8, free_gib=7)

    assert harness.dispatch(tmp_path) == ["sequential"]
    assert harness.calls == ["warmup", "sequential(models_loaded=True)"]
    assert harness.selected == 1


@pytest.mark.parametrize(
    ("total_gib", "free_gib", "expected_calls", "expected_selected"),
    [
        # Memory-limited: only a sample with the models released decides it.
        (16, 7, ["warmup", "cleanup(force=True)", "sequential(models_loaded=False)"], 1),
        # Released memory fits two more workers; with the models loaded it would fit one.
        (32, 17.5, ["warmup", "cleanup(force=True)", "parallel(models_loaded=False)"], 3),
    ],
)
def test_worker_count_is_still_decided_on_released_memory(
    tmp_path, total_gib, free_gib, expected_calls, expected_selected,
):
    harness = _DispatchHarness(total_gib=total_gib, free_gib=free_gib)

    harness.dispatch(tmp_path)

    assert harness.calls == expected_calls
    assert harness.selected == expected_selected
    # The deciding sample is taken after the release.
    assert harness.samples_with_models[-1] is False


def test_cancel_during_calibration_still_releases_the_warm_models(tmp_path):
    harness = _DispatchHarness(total_gib=8, free_gib=7, cancel_after_warmup=True)

    assert harness.dispatch(tmp_path) == []
    assert harness.calls == ["warmup", "cleanup(force=True)"]


def test_sequential_regardless_of_free_memory_agrees_with_allowed_workers():
    from modules.pipeline.memory_policy import MemorySnapshot, SafeMemoryPolicy

    rng = np.random.default_rng(3)
    for _ in range(2_000):
        total = int(rng.integers(1, 128)) * _GIB // 2
        estimate = int(rng.integers(1, 64)) * _GIB // 4
        requested = int(rng.integers(2, 9))
        current = {"available": 0}
        policy = SafeMemoryPolicy(
            memory_sampler=lambda: MemorySnapshot(total_bytes=total, available_bytes=current["available"]),
            estimated_worker_bytes=estimate,
        )
        certain = policy.sequential_regardless_of_free_memory(requested)
        for available in np.linspace(0, total, 9).astype(int):
            current["available"] = int(available)
            if certain:
                assert policy.allowed_workers(requested) == 1
        current["available"] = total
        assert certain == (policy.allowed_workers(requested) == 1)
