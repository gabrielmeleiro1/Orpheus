"""Repair waits for chapter audio before deciding whether it can package."""

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from modules.pipeline.models import ChapterResult, RepairPlan
from modules.pipeline.repair import RepairRuntime, execute_repair_plan
from modules.pipeline.stages.packaging import _collect_packaging_inputs
from modules.storage.models import Chapter


def _repair_inputs(tmp_path, chapter_results):
    observed = {"stages": [], "packaged": [], "finished": []}
    stored = [Chapter(number, 77, number, str(number), None, None) for number in (1, 2)]

    def package(results, title, author, output_dir):
        assert all(result.aac_future is None for result in results)
        observed["packaged"].append(_collect_packaging_inputs(results))
        output = output_dir / "book.m4b"
        output.write_bytes(b"m4b")
        return output

    runtime = RepairRuntime(
        begin_run=lambda: None,
        finish_run=lambda: observed["finished"].append(True),
        check_cancelled=lambda: False,
        set_stage=lambda stage: observed["stages"].append(stage.value),
        set_total_chars=lambda total: None,
        ingest_source=lambda source: SimpleNamespace(chapters=[
            SimpleNamespace(number=row.chapter_number, title=row.title, content="text")
            for row in stored
        ]),
        process_missing_chapters=lambda *args: chapter_results,
        package_results=package,
        cancel=lambda: None,
    )
    plan = RepairPlan(
        source_path=tmp_path / "source.md",
        stored_chapters=stored,
        title="Book",
        author=None,
        output_dir=tmp_path / "output",
        chapters_dir=tmp_path / "output" / "chapters",
        resolve_chapter_audio=lambda chapter: None,
    )
    return runtime, plan, observed


@pytest.mark.parametrize("pending", [False, True], ids=["completed", "pending"])
def test_repair_packages_background_aac_after_wav_release(tmp_path: Path, pending: bool):
    waiting = Event()

    class EncodingFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout=timeout)

    future = EncodingFuture()
    wav = tmp_path / "chapter_1.wav"
    wav.write_bytes(b"wav")
    wav.unlink()
    audio = [tmp_path / f"chapter_{number}.m4a" for number in (1, 2)]
    for path in audio:
        path.write_bytes(b"aac")
    first = ChapterResult(1, "1", duration_ms=1000, aac_future=future)
    second = ChapterResult(2, "2", duration_ms=2000, aac_path=audio[1], mp3_path=audio[1])
    runtime, plan, observed = _repair_inputs(tmp_path, [second, first])

    if not pending:
        future.set_result(audio[0])
    with ThreadPoolExecutor(max_workers=1) as executor:
        repair = executor.submit(execute_repair_plan, runtime, plan)
        try:
            if pending:
                assert waiting.wait(timeout=5), "Repair must wait for the pending encode"
                assert not repair.done()
                future.set_result(audio[0])
            result = repair.result(timeout=5)
        finally:
            if not future.done():
                future.set_result(audio[0])

    assert result.success, result.error
    assert first.wav_path is None
    assert first.aac_path == first.mp3_path == audio[0]
    assert observed["packaged"] == [(audio, ["1", "2"], [1000, 2000])]
    assert [chapter.chapter_number for chapter in result.chapters] == [1, 2]
    assert sorted(
        (update.chapter_id, update.duration_ms, update.mp3_path)
        for update in result.repaired_chapter_updates
    ) == [(1, 1000, str(audio[0])), (2, 2000, str(audio[1]))]
    assert observed["stages"][-1] == "complete"
    assert observed["finished"] == [True]


@pytest.mark.parametrize("failure", ["AAC encode failed", "Cancelled", None],
                         ids=["failed", "encoder-cancelled", "future-cancelled"])
def test_repair_propagates_background_encode_failure_without_packaging(tmp_path, failure):
    future = Future()
    if failure is None:
        future.cancel()
    else:
        future.set_exception(RuntimeError(failure))
    chapters = [ChapterResult(number, str(number), aac_future=future) for number in (1, 2)]
    runtime, plan, observed = _repair_inputs(tmp_path, chapters)

    result = execute_repair_plan(runtime, plan)

    assert result.success is False
    assert result.error == (failure or "")
    assert result.repaired_chapter_updates == []
    assert observed["packaged"] == []
    assert observed["stages"][-1] == "error"
    assert observed["finished"] == [True]
