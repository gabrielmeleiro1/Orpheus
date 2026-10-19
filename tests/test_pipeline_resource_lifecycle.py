"""Chunk cancellation and chapter resource lifetime tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from modules.pipeline.orchestrator import ConversionPipeline, PipelineConfig
from modules.tts.strategies.base import SynthesisResult


def test_synthesis_closes_iterator_without_scheduling_more_chunks_on_cancel(tmp_path, monkeypatch):
    pipeline = ConversionPipeline(PipelineConfig(
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp", enable_parallel=False,
    ))
    processed = []
    closed = []

    class Engine:
        supports_incremental_batch_results = True
        sample_rate = 24000
        is_loaded = True

        def iter_synthesize_batch(self, items, progress_callback=None):
            try:
                for item in items:
                    processed.append(item.index)
                    pipeline.cancel()
                    yield SynthesisResult(audio=np.ones(4, dtype=np.float32), index=item.index)
            finally:
                closed.append(True)

    pipeline._tts_engine = Engine()
    processor = pipeline.stages.chapter_processor
    monkeypatch.setattr(processor, "ensure_chapter_tts_engine", lambda: pipeline._tts_engine)
    chapter = SimpleNamespace(number=1, title="One")

    result = processor.synthesize_chapter_chunks(
        chapter=chapter, chapter_idx=0, total_chapters=1, chunks=["one", "two", "three"],
    )

    assert result is None
    assert pipeline._check_cancelled()
    assert processed == [0]
    assert closed == [True]


def test_package_runtime_stops_batch_when_cancelled(monkeypatch):
    from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime
    from modules.tts.strategies.base import SynthesisItem

    runtime = object.__new__(PackageKokoroRuntime)
    runtime.sample_rate = 24000
    processed = []

    def synthesize(text, *_args):
        processed.append(text)
        return np.ones(4, dtype=np.float32)

    monkeypatch.setattr(runtime, "synthesize", synthesize)
    results = list(runtime.iter_synthesize_batch(
        items=[SynthesisItem(text=text, voice="am_adam", index=index)
               for index, text in enumerate(["one", "two", "three"])],
        cancel_requested=lambda: bool(processed),
    ))

    assert processed == ["one"]
    assert len(results) == 1


def test_force_shutdown_terminates_worker_descendants_before_workers(monkeypatch):
    import psutil
    from modules.pipeline.pool_shutdown import _terminate_processes

    events = []
    child = MagicMock()
    child.terminate.side_effect = lambda: events.append("child terminated")
    child.kill.side_effect = lambda: events.append("child killed")
    root = MagicMock()
    root.children.return_value = [child]
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: ([], processes))
    worker = MagicMock(pid=123)
    worker.is_alive.return_value = True
    worker.terminate.side_effect = lambda: events.append("worker terminated")

    _terminate_processes([worker])

    root.children.assert_called_once_with(recursive=True)
    assert events.index("child terminated") < events.index("worker terminated")
    assert "child killed" in events


def test_cancelled_conversion_cleans_only_its_atomic_outputs_after_pool_stops(tmp_path, monkeypatch):
    pipeline = ConversionPipeline(PipelineConfig(
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp", enable_parallel=False,
    ))
    atomic_name = "." + "a" * 32 + ".tmp.m4a"
    other_job = pipeline.config.output_dir / "Other book"
    other_job.mkdir(parents=True)
    other_temp = other_job / atomic_name
    other_temp.write_bytes(b"other encode")
    paths = []
    stopped = []

    class Manager:
        def shutdown(self):
            if pipeline._check_cancelled():
                assert all(path.exists() for path in paths)
                stopped.append(True)
            return True

    pipeline.config.shared_worker_pool_manager = Manager()
    monkeypatch.setattr(pipeline.stages, "ingest", lambda _path: SimpleNamespace(
        title="Book", author=None,
        chapters=[SimpleNamespace(number=1, title="One", content="body")],
    ))
    monkeypatch.setattr(pipeline.stages, "warmup_models", lambda: None)
    monkeypatch.setattr(pipeline.stages, "save_intermediate_source", lambda *_args: None)

    def chapters(_chapters, chapters_dir):
        for directory in [chapters_dir, chapters_dir.parent]:
            path = directory / atomic_name
            path.write_bytes(b"unfinished encode")
            paths.append(path)
        (chapters_dir / "chapter_01.m4a").write_bytes(b"published")
        (chapters_dir / ".notes.tmp.m4a").write_bytes(b"unrelated")
        pipeline.cancel()
        return []

    monkeypatch.setattr(pipeline.stages, "process_chapters_sequential", chapters)
    result = pipeline.convert(tmp_path / "source.txt")

    assert result.error == "Cancelled"
    assert stopped == [True]
    assert all(not path.exists() for path in paths)
    assert other_temp.exists()
    assert (paths[0].parent / "chapter_01.m4a").exists()
    assert (paths[0].parent / ".notes.tmp.m4a").exists()


@pytest.mark.parametrize("emit_mp3", [False, True])
@pytest.mark.parametrize("background", [False, True])
def test_aac_chapter_releases_master_after_required_encodes(tmp_path, monkeypatch, emit_mp3, background):
    from modules.pipeline.models import ChapterResult
    from modules.pipeline.stages.encoding import write_wav_from_array

    pipeline = ConversionPipeline(PipelineConfig(
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        enable_chapter_aac_packaging=True, emit_chapter_mp3_artifacts=emit_mp3,
        enable_background_chapter_aac_encoding=background,
    ))
    master = pipeline.config.temp_dir / "chapter_01.wav"
    write_wav_from_array(np.ones(4, dtype=np.float32), master, sample_rate=24000)
    events = []
    processor = pipeline.stages.chapter_processor
    mp3 = tmp_path / "chapter_01.mp3"

    def encode_mp3(**kwargs):
        assert kwargs["chapter_wav"].exists()
        mp3.write_bytes(b"mp3")
        events.append("mp3")
        return mp3, 1

    def encode_aac(**kwargs):
        assert master.exists()
        assert events == (["mp3"] if emit_mp3 else [])
        output = kwargs["output_path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"aac")
        return output

    monkeypatch.setattr(processor, "encode_chapter_audio", encode_mp3)
    monkeypatch.setattr("modules.pipeline.stages.encoding.encode_chapter_aac", encode_aac)
    result = ChapterResult(chapter_number=1, chapter_title="One")
    processor.finalize_chapter_encoding(
        result=result, chapter_wav=master, chapter=SimpleNamespace(number=1, title="One"),
        chapter_idx=0, total_chapters=2, output_dir=pipeline.config.output_dir,
    )
    try:
        aac = result.aac_future.result(timeout=5) if background else result.aac_path
        assert aac.exists()
        assert not master.exists()
        assert result.wav_path is None
        if emit_mp3:
            assert mp3.exists()
    finally:
        if pipeline._run_state.chapter_aac_executor is not None:
            pipeline._run_state.chapter_aac_executor.shutdown(wait=True)


def test_failed_aac_encode_keeps_master_for_recovery(tmp_path, monkeypatch):
    from modules.pipeline.models import ChapterResult
    from modules.pipeline.stages.encoding import write_wav_from_array

    pipeline = ConversionPipeline(PipelineConfig(
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        enable_chapter_aac_packaging=True, enable_background_chapter_aac_encoding=False,
    ))
    master = pipeline.config.temp_dir / "chapter_01.wav"
    write_wav_from_array(np.ones(4, dtype=np.float32), master, sample_rate=24000)

    def encode_aac(**kwargs):
        raise RuntimeError("encode failed")

    monkeypatch.setattr("modules.pipeline.stages.encoding.encode_chapter_aac", encode_aac)
    result = ChapterResult(chapter_number=1, chapter_title="One")
    with pytest.raises(RuntimeError, match="encode failed"):
        pipeline.stages.chapter_processor.finalize_chapter_encoding(
            result=result, chapter_wav=master, chapter=SimpleNamespace(number=1, title="One"),
            chapter_idx=0, total_chapters=1, output_dir=pipeline.config.output_dir,
        )

    assert master.exists()
    assert result.wav_path == master


def test_narration_releases_temporary_body_after_composition(tmp_path):
    from modules.pipeline.chapter_narration import add_chapter_narration
    from modules.pipeline.stages.encoding import write_wav_from_array

    body = tmp_path / "body.wav"
    write_wav_from_array(np.ones(4, dtype=np.float32), body, sample_rate=24000)
    runtime = SimpleNamespace(
        config=SimpleNamespace(temp_dir=tmp_path, conversion_mode="audiobook", voice="am_adam", speed=1),
        check_cancelled=lambda: False, tts_sample_rate=lambda: 24000,
        synthesize_speech=lambda **_kwargs: np.ones(4, dtype=np.float32),
        track_temp_cleanup_path=lambda _path: None,
    )
    output = add_chapter_narration(
        runtime, SimpleNamespace(number=1, title="One"), body, SimpleNamespace(pcm_rms=None),
    )

    assert output.exists()
    assert not body.exists()


def test_force_shutdown_stops_ffmpeg_child_and_removes_abandoned_encode(tmp_path):
    import shutil
    import subprocess
    import sys
    import time

    import psutil

    from modules.audio.ffmpeg import cleanup_abandoned_atomic_outputs
    from modules.pipeline.pool_shutdown import _shutdown_executor_bounded

    try:
        psutil.Process().children(recursive=True)
    except PermissionError:
        pytest.skip("Process enumeration is blocked by the sandbox")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is unavailable")
    output = tmp_path / ("." + "b" * 32 + ".tmp.m4a")
    pid_path = tmp_path / "child.pid"
    program = """
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([
    sys.argv[1], "-nostdin", "-v", "quiet", "-re", "-f", "lavfi",
    "-i", "anullsrc=r=24000:cl=mono", "-c:a", "aac", "-y", sys.argv[2],
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path(sys.argv[3]).write_text(str(child.pid))
time.sleep(60)
"""
    parent = subprocess.Popen([sys.executable, "-c", program, ffmpeg, str(output), str(pid_path)])
    child = None
    try:
        deadline = time.monotonic() + 5
        while not (pid_path.exists() and output.exists()) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.exists() and output.exists()
        child = psutil.Process(int(pid_path.read_text()))
        assert child.is_running()
        worker = SimpleNamespace(
            pid=parent.pid, is_alive=lambda: parent.poll() is None,
            terminate=parent.terminate, kill=parent.kill,
            join=lambda timeout: parent.wait(timeout=timeout),
        )
        executor = SimpleNamespace(_processes={parent.pid: worker}, shutdown=lambda **_kwargs: None)

        _shutdown_executor_bounded(executor, timeout_seconds=0.0, cancel_futures=True)
        cleanup_abandoned_atomic_outputs(tmp_path)

        assert parent.poll() is not None
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        assert not output.exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child is not None and child.is_running():
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass


def test_force_shutdown_still_terminates_worker_when_descendant_lookup_is_denied(monkeypatch):
    import psutil
    from modules.pipeline.pool_shutdown import _terminate_processes

    root = MagicMock()
    root.children.side_effect = PermissionError("process enumeration denied")
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    worker = MagicMock(pid=123)
    worker.is_alive.return_value = True

    _terminate_processes([worker])

    worker.terminate.assert_called_once()
    worker.kill.assert_called_once()
