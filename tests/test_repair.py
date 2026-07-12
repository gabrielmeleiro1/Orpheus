"""Tests for repair orchestration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from modules.pipeline.models import RepairPlan
from modules.storage.models import Chapter
from modules.pipeline.repair import RepairRuntime, execute_repair_plan


def _make_runtime(
    *,
    document_chapters: list[SimpleNamespace],
    cancel_requested: bool = False,
) -> tuple[RepairRuntime, dict[str, object]]:
    observed: dict[str, object] = {"stages": []}

    def begin_run() -> None:
        observed["started"] = True

    def finish_run() -> None:
        observed["finished"] = True

    def check_cancelled() -> bool:
        return cancel_requested

    def set_stage(stage) -> None:  # noqa: ANN001
        observed["stages"].append(stage.value)

    def set_total_chars(value: int) -> None:
        observed["total_chars"] = value

    def ingest_source(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(chapters=document_chapters)

    runtime = RepairRuntime(
        begin_run=begin_run,
        finish_run=finish_run,
        check_cancelled=check_cancelled,
        set_stage=set_stage,
        set_total_chars=set_total_chars,
        ingest_source=ingest_source,
        process_missing_chapters=lambda *_args: [],
        package_results=lambda *_args: Path("output.m4b"),
        cancel=lambda: None,
    )
    return runtime, observed


def test_execute_repair_plan_reports_structure_mismatch(tmp_path: Path) -> None:
    runtime, observed = _make_runtime(
        document_chapters=[SimpleNamespace(number=1, title="One", content="text")],
    )
    plan = RepairPlan(
        source_path=tmp_path / "source.md",
        stored_chapters=[Chapter(1, 77, 2, "Two", None, None)],
        title="Repair Book",
        author="Tester",
        output_dir=tmp_path / "output",
        chapters_dir=tmp_path / "output" / "chapters",
        resolve_chapter_audio=lambda _chapter: None,
    )

    result = execute_repair_plan(runtime, plan)

    assert result.success is False
    assert "structure mismatch" in str(result.error).lower()
    assert "missing source chapter rows for stored chapters: 2" in str(result.error)
    assert observed["stages"] == ["ingesting", "error"]


def test_execute_repair_plan_handles_kept_aac_chapters_and_persists_updates(
    tmp_path: Path,
) -> None:
    """Kept chapters are classified by container and repaired AAC chapters
    persist a durable audio path in chapters.mp3_path."""
    from modules.pipeline.models import ChapterResult

    document_chapters = [
        SimpleNamespace(number=1, title="One", content="text one"),
        SimpleNamespace(number=2, title="Two", content="text two"),
    ]
    stored_chapters = [
        Chapter(11, 77, 1, "One", 1000, "chapter_01.m4a"),
        Chapter(12, 77, 2, "Two", None, None),
    ]

    kept_audio = tmp_path / "chapter_01.m4a"
    kept_audio.write_bytes(b"aac")
    repaired_audio = tmp_path / "chapters" / "02_Two.m4a"
    repaired_audio.parent.mkdir(parents=True, exist_ok=True)
    repaired_audio.write_bytes(b"aac")

    packaged_inputs: list[list[ChapterResult]] = []

    def package_results(chapter_results, _title, _author, output_dir) -> Path:  # noqa: ANN001
        packaged_inputs.append(list(chapter_results))
        final = Path(output_dir) / "Repair Book.m4b"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"m4b")
        return final

    runtime, _observed = _make_runtime(document_chapters=document_chapters)
    runtime = RepairRuntime(
        begin_run=runtime.begin_run,
        finish_run=runtime.finish_run,
        check_cancelled=runtime.check_cancelled,
        set_stage=runtime.set_stage,
        set_total_chars=runtime.set_total_chars,
        ingest_source=runtime.ingest_source,
        process_missing_chapters=lambda chapters, _output_dir, _total: [
            ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                aac_path=repaired_audio,
                mp3_path=repaired_audio,
                duration_ms=2000,
            )
            for chapter in chapters
        ],
        package_results=package_results,
        cancel=runtime.cancel,
    )
    plan = RepairPlan(
        source_path=tmp_path / "source.md",
        stored_chapters=stored_chapters,
        title="Repair Book",
        author="Tester",
        output_dir=tmp_path / "output",
        chapters_dir=tmp_path / "output" / "chapters",
        resolve_chapter_audio=lambda chapter: (
            kept_audio if chapter.chapter_number == 1 else None
        ),
    )

    result = execute_repair_plan(runtime, plan)

    assert result.success is True
    kept_result, repaired_result = packaged_inputs[0]
    assert kept_result.aac_path == kept_audio
    assert kept_result.mp3_path is None
    assert repaired_result.aac_path == repaired_audio
    assert repaired_result.mp3_path == repaired_audio
    assert [
        (update.chapter_id, update.duration_ms, update.mp3_path)
        for update in result.repaired_chapter_updates
    ] == [(12, 2000, str(repaired_audio))]


def test_execute_repair_plan_persists_durable_path_from_background_aac_future(
    tmp_path: Path,
) -> None:
    """Background-AAC repair defers the durable ``.m4a`` to packaging: the
    chapter returns with ``mp3_path=None`` and only an ``aac_future`` (its WAV
    survives to satisfy the pre-packaging input check), and packaging's
    ``resolve_background_audio_futures`` backfills ``mp3_path`` with the durable
    path. The persisted ``RepairedChapterUpdate`` must carry that resolved path,
    not None."""
    from concurrent.futures import Future

    from modules.pipeline.models import ChapterResult
    from modules.pipeline.stages.packaging import resolve_background_audio_futures

    document_chapters = [
        SimpleNamespace(number=1, title="One", content="text one"),
        SimpleNamespace(number=2, title="Two", content="text two"),
    ]
    stored_chapters = [
        Chapter(11, 77, 1, "One", 1000, str(tmp_path / "chapter_01.m4a")),
        Chapter(12, 77, 2, "Two", None, None),
    ]

    kept_audio = tmp_path / "chapter_01.m4a"
    kept_audio.write_bytes(b"aac")

    chapters_dir = tmp_path / "output" / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    # Durable chapter WAV kept by the pipeline until packaging; it satisfies the
    # repair-input validation while mp3_path/aac_path are still unresolved.
    chapter_wav = chapters_dir / "chapter_02.wav"
    chapter_wav.write_bytes(b"wav")
    durable_audio = chapters_dir / "02_Two.m4a"

    def _background_encode(path: Path) -> Future:
        future: Future = Future()
        path.write_bytes(b"aac")
        future.set_result(str(path))
        return future

    def process_missing(chapters, _output_dir, _total):  # noqa: ANN001
        return [
            ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                wav_path=chapter_wav,
                mp3_path=None,
                aac_path=None,
                aac_future=_background_encode(durable_audio),
                duration_ms=2000,
            )
            for chapter in chapters
        ]

    packaged_inputs: list[list[ChapterResult]] = []

    def package_results(chapter_results, _title, _author, output_dir):  # noqa: ANN001
        # Mirror package_chapters: resolving the background AAC futures backfills
        # each repaired chapter's durable mp3_path from the encoded .m4a.
        resolve_background_audio_futures(chapter_results)
        packaged_inputs.append(list(chapter_results))
        final = Path(output_dir) / "Repair Book.m4b"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"m4b")
        return final

    runtime, _observed = _make_runtime(document_chapters=document_chapters)
    runtime = RepairRuntime(
        begin_run=runtime.begin_run,
        finish_run=runtime.finish_run,
        check_cancelled=runtime.check_cancelled,
        set_stage=runtime.set_stage,
        set_total_chars=runtime.set_total_chars,
        ingest_source=runtime.ingest_source,
        process_missing_chapters=process_missing,
        package_results=package_results,
        cancel=runtime.cancel,
    )
    plan = RepairPlan(
        source_path=tmp_path / "source.md",
        stored_chapters=stored_chapters,
        title="Repair Book",
        author="Tester",
        output_dir=tmp_path / "output",
        chapters_dir=chapters_dir,
        resolve_chapter_audio=lambda chapter: (
            kept_audio if chapter.chapter_number == 1 else None
        ),
    )

    result = execute_repair_plan(runtime, plan)

    assert result.success is True
    # Packaging resolved the future and backfilled the durable .m4a.
    _kept_result, repaired_result = packaged_inputs[0]
    assert repaired_result.mp3_path == durable_audio
    # The persisted update must carry the resolved durable path, not None.
    assert [
        (update.chapter_id, update.duration_ms, update.mp3_path)
        for update in result.repaired_chapter_updates
    ] == [(12, 2000, str(durable_audio))]


def test_execute_repair_plan_is_idempotent_after_background_aac_repair(
    tmp_path: Path,
) -> None:
    """A chapter repaired once must not re-classify as missing on a later pass.
    Persisting the durable path (not None) is what keeps repair idempotent:
    the next pass resolves the chapter's audio and repackages it instead of
    re-rendering and re-nulling the reference forever."""
    from concurrent.futures import Future

    from modules.pipeline.models import ChapterResult
    from modules.pipeline.stages.packaging import resolve_background_audio_futures

    document_chapters = [
        SimpleNamespace(number=1, title="One", content="text one"),
        SimpleNamespace(number=2, title="Two", content="text two"),
    ]
    kept_audio = tmp_path / "chapter_01.m4a"
    kept_audio.write_bytes(b"aac")
    chapters_dir = tmp_path / "output" / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    chapter_wav = chapters_dir / "chapter_02.wav"
    chapter_wav.write_bytes(b"wav")
    durable_audio = chapters_dir / "02_Two.m4a"

    # Persisted chapters.mp3_path per chapter id. Chapter 12 starts missing;
    # apply_repair_result-equivalent writes flow back here after each pass.
    persisted_paths: dict[int, str | None] = {11: str(kept_audio), 12: None}

    def make_stored_chapters() -> list[Chapter]:
        return [
            Chapter(11, 77, 1, "One", 1000, persisted_paths[11]),
            Chapter(12, 77, 2, "Two", None, persisted_paths[12]),
        ]

    def resolve_chapter_audio(chapter):  # noqa: ANN001
        # Mirror production: resolve the stored chapters.mp3_path if it exists.
        raw = getattr(chapter, "mp3_path", None)
        if raw is None:
            return None
        candidate = Path(raw)
        return candidate if candidate.exists() else None

    missing_passed: list[list[int]] = []

    def process_missing(chapters, _output_dir, _total):  # noqa: ANN001
        missing_passed.append([chapter.number for chapter in chapters])
        results: list[ChapterResult] = []
        for chapter in chapters:
            future: Future = Future()
            durable_audio.write_bytes(b"aac")
            future.set_result(str(durable_audio))
            results.append(
                ChapterResult(
                    chapter_number=chapter.number,
                    chapter_title=chapter.title,
                    wav_path=chapter_wav,
                    mp3_path=None,
                    aac_path=None,
                    aac_future=future,
                    duration_ms=2000,
                )
            )
        return results

    def package_results(chapter_results, _title, _author, output_dir):  # noqa: ANN001
        resolve_background_audio_futures(chapter_results)
        final = Path(output_dir) / "Repair Book.m4b"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"m4b")
        return final

    def run_once():
        runtime, _observed = _make_runtime(document_chapters=document_chapters)
        runtime = RepairRuntime(
            begin_run=runtime.begin_run,
            finish_run=runtime.finish_run,
            check_cancelled=runtime.check_cancelled,
            set_stage=runtime.set_stage,
            set_total_chars=runtime.set_total_chars,
            ingest_source=runtime.ingest_source,
            process_missing_chapters=process_missing,
            package_results=package_results,
            cancel=runtime.cancel,
        )
        plan = RepairPlan(
            source_path=tmp_path / "source.md",
            stored_chapters=make_stored_chapters(),
            title="Repair Book",
            author="Tester",
            output_dir=tmp_path / "output",
            chapters_dir=chapters_dir,
            resolve_chapter_audio=resolve_chapter_audio,
        )
        result = execute_repair_plan(runtime, plan)
        # Mirror sqlite_repo.apply_repair_result: persist each update's mp3_path.
        for update in result.repaired_chapter_updates:
            persisted_paths[update.chapter_id] = update.mp3_path
        return result

    first = run_once()
    assert first.success is True
    assert missing_passed[0] == [2]
    assert persisted_paths[12] == str(durable_audio)

    second = run_once()
    assert second.success is True
    # Chapter 2 now resolves to a durable file, so it is not re-rendered.
    assert missing_passed[1] == []
    assert [update.chapter_id for update in second.repaired_chapter_updates] == []


def test_execute_repair_plan_uses_shared_chapter_set_validation(tmp_path: Path) -> None:
    from unittest.mock import patch

    from modules.pipeline import repair as repair_module

    document_chapters = [SimpleNamespace(number=1, title="One", content="text")]
    stored_chapters = [Chapter(1, 77, 1, "One", None, None)]
    runtime, _observed = _make_runtime(document_chapters=document_chapters)
    plan = RepairPlan(
        source_path=tmp_path / "source.md",
        stored_chapters=stored_chapters,
        title="Repair Book",
        author="Tester",
        output_dir=tmp_path / "output",
        chapters_dir=tmp_path / "output" / "chapters",
        resolve_chapter_audio=lambda _chapter: None,
    )

    with patch.object(
        repair_module,
        "validate_chapter_sets",
        return_value=["duplicate source chapter rows: 1"],
    ) as validate_sets:
        result = execute_repair_plan(runtime, plan)

    validate_sets.assert_called_once_with(
        document_chapters,
        stored_chapters,
        context="repair",
    )
    assert result.success is False
    assert "duplicate source chapter rows: 1" in str(result.error)
