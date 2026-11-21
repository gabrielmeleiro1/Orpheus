"""Deterministic performance telemetry benchmarks."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from modules.audio.packager import M4BPackager
from modules.pipeline.orchestrator import ChapterResult, ConversionPipeline, PipelineConfig
from modules.tts import cleaner as cleaner_module
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from tests.fixtures.fake_cleaner_backend import attach_cleaner_backend_strategy


def _write_pcm16_wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24_000)
        wav_file.writeframes(np.zeros(24_000, dtype=np.int16).tobytes())
    return path


@pytest.mark.benchmark
def test_wav_master_packaging_skips_redundant_mp3_encode_stage(tmp_path: Path) -> None:
    chapter = SimpleNamespace(number=1, title="Chapter 1")

    def _run_finalize(*, emit_chapter_mp3_artifacts: bool) -> float:
        cfg = PipelineConfig(
            output_dir=tmp_path / f"output-{emit_chapter_mp3_artifacts}",
            temp_dir=tmp_path / f"temp-{emit_chapter_mp3_artifacts}",
            enable_parallel=False,
            emit_chapter_mp3_artifacts=emit_chapter_mp3_artifacts,
        )
        pipeline = ConversionPipeline(cfg)
        pipeline._tts_engine = SimpleNamespace(sample_rate=24_000)
        result = ChapterResult(chapter_number=1, chapter_title="Chapter 1")

        def _slow_encode(**_kwargs):
            time.sleep(0.05)
            mp3_path = tmp_path / f"chapter-{emit_chapter_mp3_artifacts}.mp3"
            mp3_path.write_bytes(b"mp3")
            return mp3_path, 1000

        with patch.object(pipeline.stages.chapter_processor, "encode_chapter_audio", side_effect=_slow_encode):
            started_at = time.perf_counter()
            finalized = pipeline.stages.chapter_processor.finalize_chapter_encoding(  # noqa: SLF001
                result=result,
                chapter_wav=_write_pcm16_wav(cfg.temp_dir / "chapter_01_narrated.wav"),
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                output_dir=cfg.output_dir,
            )
            elapsed = time.perf_counter() - started_at

        assert finalized.wav_path is not None
        assert finalized.wav_path.exists()
        assert finalized.duration_ms == 1000
        if emit_chapter_mp3_artifacts:
            assert finalized.mp3_path is not None
        else:
            assert finalized.mp3_path is None
        return elapsed

    wav_only_seconds = _run_finalize(emit_chapter_mp3_artifacts=False)
    mp3_artifact_seconds = _run_finalize(emit_chapter_mp3_artifacts=True)
    improvement = (mp3_artifact_seconds - wav_only_seconds) / mp3_artifact_seconds

    print(
        "benchmark.wav_master_skip_mp3_improvement="
        f"{improvement:.2%} (wav_only_s={wav_only_seconds:.4f}, "
        f"mp3_artifact_s={mp3_artifact_seconds:.4f})"
    )
    assert improvement >= 0.30


@pytest.mark.benchmark
def test_orchestrator_sequential_dispatch_overhead(tmp_path: Path) -> None:
    cfg = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=False,
        enable_pipelining=False,
    )
    pipeline = ConversionPipeline(cfg)

    chapter_count = 200
    chapters = [
        SimpleNamespace(number=i + 1, title=f"Chapter {i + 1}", content="x" * 100)
        for i in range(chapter_count)
    ]

    def _fake_process_chapter(chapter, chapter_idx, total_chapters, output_dir):
        _ = (chapter_idx, total_chapters, output_dir)
        return ChapterResult(
            chapter_number=chapter.number,
            chapter_title=chapter.title,
            duration_ms=1000,
        )

    with patch.object(pipeline.stages.chapter_processor, "process_chapter", side_effect=_fake_process_chapter):
        start = time.perf_counter()
        results = pipeline.stages.process_chapters_sequential(chapters, cfg.output_dir)
        elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(results) == chapter_count
    per_chapter_overhead_ms = elapsed_ms / chapter_count
    print(
        "benchmark.orchestrator_overhead_ms_per_chapter="
        f"{per_chapter_overhead_ms:.4f} (total_ms={elapsed_ms:.2f}, chapters={chapter_count})"
    )
    assert per_chapter_overhead_ms < 5.0


@pytest.mark.benchmark
def test_cleaner_api_batch_trace_throughput_with_bounded_concurrency() -> None:
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig(
        backend="api",
        api_base_url="https://api.openai.com/v1",
        api_key_env="TEST_OPENAI_API_KEY",
        api_model="gpt-4o-mini",
        api_batch_concurrency=6,
    )
    cleaner._loaded = True
    cleaner._model = None
    cleaner._tokenizer = None
    cleaner._model_lock = threading.RLock()
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    cleaner._needs_llm_cleaning = lambda _text: True
    cleaner._rule_based_clean = lambda text: text
    cleaner._extract_result = lambda _response: ""

    async def _fake_generate_api_response_async(prompt: str) -> str:
        _ = prompt
        await asyncio.sleep(0.005)
        return ""

    cleaner._generate_api_response_async = _fake_generate_api_response_async

    attach_cleaner_backend_strategy(cleaner)

    texts = [f"sample chunk {i}" for i in range(48)]
    start = time.perf_counter()
    traces = asyncio.run(cleaner.clean_batch_with_trace_async(texts))
    elapsed_s = max(time.perf_counter() - start, 1e-9)

    assert len(traces) == len(texts)
    assert [trace.original_text for trace in traces] == texts
    throughput = len(texts) / elapsed_s
    print(
        "benchmark.cleaner_api_chunks_per_second="
        f"{throughput:.2f} (chunks={len(texts)}, elapsed_s={elapsed_s:.4f}, "
        f"concurrency={cleaner._get_api_batch_concurrency()})"
    )
    assert throughput > 100.0


@pytest.mark.benchmark
def test_synthesis_iterator_releases_chunk_audio_incrementally(tmp_path: Path) -> None:
    from modules.tts.engine import BatchResult

    cfg = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=False,
    )
    cfg.max_in_memory_chapter_samples = 1
    pipeline = ConversionPipeline(cfg)

    class _IncrementalTTSEngine:
        supports_incremental_batch_results = True
        sample_rate = 24000
        is_loaded = True

        def __init__(self) -> None:
            self.max_retained_arrays = 0

        def synthesize_batch(self, *_args, **_kwargs):
            raise AssertionError("pipeline should consume iter_synthesize_batch")

        def iter_synthesize_batch(self, items, progress_callback=None):
            for completed, item in enumerate(items, start=1):
                result = BatchResult(
                    audio=np.ones(2048, dtype=np.float32),
                    index=item.index,
                    error=None,
                    duration_ms=1,
                )
                self.max_retained_arrays = max(
                    self.max_retained_arrays,
                    int(result.audio is not None),
                )
                if progress_callback is not None:
                    progress_callback(completed, len(items))
                yield result
                if result.audio is not None:
                    raise AssertionError("pipeline retained yielded audio after consumption")

    engine = _IncrementalTTSEngine()
    pipeline._tts_engine = engine
    chapter = SimpleNamespace(number=1, title="Streaming Chapter")
    chunks = [f"chunk {idx}" for idx in range(16)]

    chapter_audio = pipeline.stages.chapter_processor.synthesize_chapter_chunks(
        chapter=chapter,
        chapter_idx=0,
        total_chapters=1,
        chunks=chunks,
    )

    assert isinstance(chapter_audio, Path)
    assert chapter_audio.exists()
    assert engine.max_retained_arrays == 1
    print(
        "benchmark.synthesis_iterator_max_retained_arrays="
        f"{engine.max_retained_arrays} (chunks={len(chunks)})"
    )


@pytest.mark.benchmark
def test_file_backed_normalization_avoids_full_wav_read(tmp_path: Path) -> None:
    wav_path = tmp_path / "chapter.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(np.full(24000, 1000, dtype=np.int16).tobytes())

    cfg = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        normalize_volume=True,
    )
    pipeline = ConversionPipeline(cfg)
    pipeline._tts_engine = SimpleNamespace(sample_rate=24000)

    captured: dict[str, object] = {}
    fake_encoder = MagicMock()

    def _wav_to_mp3(input_path, output_path, bitrate="128k", audio_filter=None, cancel_requested=None):
        captured["input_path"] = Path(input_path)
        captured["audio_filter"] = audio_filter
        captured["cancel_requested"] = cancel_requested
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"mp3")

    fake_encoder.wav_to_mp3.side_effect = _wav_to_mp3

    with patch("modules.audio.encoder.AudioEncoder", return_value=fake_encoder):
        mp3_path, duration_ms = pipeline.stages.chapter_processor.encode_chapter_audio(
            chapter=SimpleNamespace(number=1, title="Chapter 1"),
            chapter_idx=0,
            total_chapters=1,
            output_dir=tmp_path,
            chapter_wav=wav_path,
        )

    assert mp3_path.exists()
    assert duration_ms == 1000
    assert captured["input_path"] == wav_path
    assert str(captured["audio_filter"]).startswith("volume=")
    # The encode must stay cancellable: FFmpeg polls the pipeline's cancel flag.
    assert captured["cancel_requested"] == pipeline.stages.runtime.check_cancelled
    print(
        "benchmark.file_backed_normalization_full_read_calls=0 "
        f"(filter={captured['audio_filter']})"
    )


@pytest.mark.benchmark
def test_cleaner_api_batch_reuses_single_client_session() -> None:
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig(
        backend="api",
        api_base_url="https://api.test/v1",
        api_key="test-key",
        api_model="gpt-4o-mini",
        api_batch_concurrency=4,
    )
    cleaner._loaded = True
    cleaner._model = None
    cleaner._tokenizer = None
    cleaner._model_lock = threading.RLock()
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    cleaner._needs_llm_cleaning = lambda _text: True
    cleaner._rule_based_clean = lambda text: text
    cleaner._extract_result = lambda _response: ""

    attach_cleaner_backend_strategy(cleaner)

    session_creations = 0
    requests = 0

    class _FakeAioHTTPResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        async def text(self) -> str:
            return json.dumps({"choices": [{"message": {"content": "cleaned"}}]})

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            _ = (args, kwargs)
            nonlocal session_creations
            session_creations += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def post(self, url, *, headers, json, ssl):  # noqa: A002
            _ = (url, headers, json, ssl)
            nonlocal requests
            requests += 1
            return _FakeAioHTTPResponse()

    fake_aiohttp = type(
        "_FakeAioHTTP",
        (),
        {
            "ClientSession": _FakeClientSession,
            "ClientTimeout": lambda **_kwargs: object(),
            "ClientError": Exception,
        },
    )

    texts = [f"chunk {idx}" for idx in range(10)]
    with patch.object(cleaner_module, "aiohttp", fake_aiohttp):
        traces = asyncio.run(cleaner.clean_batch_with_trace_async(texts))

    assert len(traces) == len(texts)
    assert requests == len(texts)
    assert session_creations == 1
    print(
        "benchmark.cleaner_api_batch_client_sessions="
        f"{session_creations} (requests={requests}, concurrency={cleaner._get_api_batch_concurrency()})"
    )


@pytest.mark.benchmark
def test_packager_duration_probe_vs_supplied_durations_overhead(tmp_path: Path) -> None:
    packager = M4BPackager()
    file_count = 8

    mp3_files = [tmp_path / f"chapter_{i + 1:02d}.mp3" for i in range(file_count)]
    for mp3 in mp3_files:
        mp3.write_bytes(b"mp3")
    chapter_names = [f"Chapter {i + 1}" for i in range(file_count)]

    def _fake_get_duration(_self, _path: Path) -> float:
        time.sleep(0.003)
        return 1.0

    with patch(
        "modules.audio.encoder.AudioEncoder.get_duration",
        autospec=True,
        side_effect=_fake_get_duration,
    ) as mocked_get_duration:
        start_probe = time.perf_counter()
        probed = packager._generate_chapter_markers(mp3_files, chapter_names)
        probe_ms = (time.perf_counter() - start_probe) * 1000
        assert mocked_get_duration.call_count == file_count

        mocked_get_duration.reset_mock()
        supplied_durations = [1000] * file_count
        start_supplied = time.perf_counter()
        reused = packager._generate_chapter_markers(
            mp3_files,
            chapter_names,
            chapter_durations_ms=supplied_durations,
        )
        supplied_ms = (time.perf_counter() - start_supplied) * 1000
        assert mocked_get_duration.call_count == 0

    assert len(probed) == file_count
    assert len(reused) == file_count
    saved_ms = probe_ms - supplied_ms
    print(
        "benchmark.packaging_duration_reuse_saved_ms="
        f"{saved_ms:.2f} (probe_ms={probe_ms:.2f}, supplied_ms={supplied_ms:.2f}, chapters={file_count})"
    )
    assert saved_ms > 0
