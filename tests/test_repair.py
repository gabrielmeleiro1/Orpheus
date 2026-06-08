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
