"""Regression coverage for pipeline orchestrator."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.pipeline.models import PipelineStage, RepairPlan
from modules.pipeline.recovery import recovery_audio_paths

from tests._factories_group2_conversion import SOURCE, TITLES, _probe


@pytest.mark.parametrize("mode", ["audiobook", "document_audio"])
@pytest.mark.parametrize("emit_mp3,aac", [(False, False), (False, True), (True, False), (True, True)])
def test_conversion_retains_chapter_artifacts_and_packages_source_order(conversion, mode, emit_mp3, aac):
    source, engine, make_pipeline = conversion
    pipeline = make_pipeline(
        conversion_mode=mode, emit_chapter_mp3_artifacts=emit_mp3,
        enable_chapter_aac_packaging=aac,
    )
    result = pipeline.convert(source, title="Tiny Book", author="Mara")

    assert result.success, result.error
    assert pipeline.stage == PipelineStage.COMPLETE
    assert [chapter.chapter_number for chapter in result.chapters] == [1, 2]
    assert [chapter.chapter_title for chapter in result.chapters] == TITLES
    assert [chapter.duration_ms for chapter in result.chapters] == [3200, 3200]
    assert result.total_duration_ms == 6400
    assert result.output_path.suffix == (".m4b" if mode == "audiobook" else ".m4a")
    media = _probe(result.output_path)
    assert media["format"]["tags"]["title"] == "Tiny Book"
    assert media["format"]["tags"]["artist"] == "Mara"
    assert media["streams"][0]["codec_name"] == "aac"
    assert float(media["format"]["duration"]) == pytest.approx(6.4, abs=0.15)
    if mode == "audiobook":
        # 3.2 s chapters are a whole number of AAC frames (75 x 1024 samples at
        # 24 kHz), so stream copy adds no seam padding here.
        second_start = 3200
        assert [marker["tags"]["title"] for marker in media["chapters"]] == TITLES
        assert [chapter.packaged_start_ms for chapter in result.chapters] == [0, second_start]
        assert [float(marker["start_time"]) for marker in media["chapters"]] == [0, second_start / 1000]
    else:
        assert media["chapters"] == []
        assert [chapter.packaged_start_ms for chapter in result.chapters] == [None, None]
    label = "Chapter" if mode == "audiobook" else "Section"
    assert [text for text in engine.texts if "number" in text] == [
        f"{label} number 1. Zebra Crossing.", f"{label} number 2. Amber Window.",
    ]
    for chapter in result.chapters:
        assert chapter.aac_future is None
        if emit_mp3 or aac:
            assert chapter.mp3_path.is_file()
            assert chapter.mp3_path.suffix == (".mp3" if emit_mp3 else ".m4a")
        else:
            assert chapter.mp3_path is None
        if aac:
            assert chapter.wav_path is None
            assert chapter.aac_path.exists() is (not emit_mp3)
        else:
            assert chapter.aac_path is None
            # The temporary WAV master is released once the book is packaged.
            assert chapter.wav_path is None or not chapter.wav_path.exists()
    assert not list(pipeline.config.temp_dir.rglob("*.wav"))
    assert not list(pipeline.config.temp_dir.rglob("*.m4a"))
    assert pipeline.stages.runtime.state.chapter_aac_executor is None
    assert source.read_bytes() == SOURCE.read_bytes()


@pytest.mark.parametrize("emit_mp3", [False, True])
def test_cancelled_conversion_keeps_published_chapter_and_repairs_from_durable_source(conversion, emit_mp3):
    source, engine, make_pipeline = conversion
    pipeline = make_pipeline(emit_chapter_mp3_artifacts=emit_mp3, enable_chapter_aac_packaging=True)
    checkpoints = []

    def checkpoint(durable, snapshot):
        assert durable.read_bytes() == SOURCE.read_bytes()
        checkpoints.append((durable, snapshot))

    pipeline.set_recovery_callback(checkpoint)
    batches = 0

    def cancel_second_batch():
        nonlocal batches
        batches += 1
        assert len(checkpoints) == 1
        if batches == 2:
            pipeline.stages.runtime.state.chapter_aac_pending_future.result(timeout=2)
            pipeline.cancel()

    engine.before_batch = cancel_second_batch
    result = pipeline.convert(source, title="Tiny Book")
    assert not result.success and result.error == "Cancelled"
    assert pipeline.stage == PipelineStage.CANCELLED
    assert result.output_path is None
    assert len(checkpoints) == 2
    durable, snapshot = checkpoints[-1]
    assert [chapter.mp3_path for chapter in checkpoints[0][1].chapters] == [None, None]
    retained = snapshot.chapters[0].mp3_path
    assert retained.is_file()
    assert snapshot.chapters[1].mp3_path is None
    retained_bytes = retained.read_bytes()
    inventory = recovery_audio_paths(durable)
    assert list(inventory) == [(1, TITLES[0]), (2, TITLES[1])]
    source.unlink()
    engine.before_batch = None
    engine.texts.clear()
    stored = [SimpleNamespace(
        id=index, chapter_number=chapter.chapter_number, title=chapter.chapter_title,
        duration_ms=chapter.duration_ms, mp3_path=str(chapter.mp3_path) if chapter.mp3_path else None,
    ) for index, chapter in enumerate(snapshot.chapters, 1)]
    repair = make_pipeline(emit_chapter_mp3_artifacts=emit_mp3, enable_chapter_aac_packaging=True)
    repaired = repair.repair(RepairPlan(
        source_path=durable, stored_chapters=list(reversed(stored)), title="Tiny Book", author=None,
        output_dir=durable.parent.parent, chapters_dir=retained.parent,
        resolve_chapter_audio=lambda chapter: inventory[(chapter.chapter_number, chapter.title)],
    ))
    assert repaired.success, repaired.error
    assert retained.read_bytes() == retained_bytes
    assert all("Mara crossed" not in text and "Zebra" not in text for text in engine.texts)
    assert any("baker" in text for text in engine.texts)
    assert [chapter.chapter_number for chapter in repaired.chapters] == [1, 2]
    assert [chapter.packaged_start_ms for chapter in repaired.chapters] == [0, 3200]
    assert [update.chapter_id for update in repaired.repaired_chapter_updates] == [2]
    assert Path(repaired.repaired_chapter_updates[0].mp3_path).is_file()
    assert [marker["tags"]["title"] for marker in _probe(repaired.output_path)["chapters"]] == TITLES


def test_cancellation_during_ingestion_does_not_register_recovery_or_synthesize(conversion):
    source, engine, make_pipeline = conversion
    pipeline = make_pipeline()
    checkpoints = []
    pipeline.set_recovery_callback(lambda *args: checkpoints.append(args))

    def cancel_ingestion(stage, *args):
        if stage == PipelineStage.INGESTING:
            pipeline.cancel()

    pipeline.progress_callback = cancel_ingestion
    result = pipeline.convert(source, title="Tiny Book")
    assert not result.success and result.error == "Cancelled"
    assert pipeline.stage == PipelineStage.CANCELLED
    assert result.chapters == []
    assert result.output_path is None
    assert engine.texts == []
    assert checkpoints == []
    assert list(pipeline.config.output_dir.iterdir()) == []
    assert source.read_bytes() == SOURCE.read_bytes()


def test_config_reprs_never_include_the_cleaner_api_key(tmp_path):
    from modules.pipeline.models import PipelineConfig
    from modules.tts.cleaner_config import CleanerConfig, build_cleaner_config_from_pipeline

    secret = "sk-test-do-not-print"
    config = PipelineConfig(
        cleaner_api_key=secret, output_dir=tmp_path / "out", temp_dir=tmp_path / "tmp",
    )

    assert secret not in repr(config)
    assert secret not in repr(CleanerConfig(api_key=secret))
    assert build_cleaner_config_from_pipeline(config).api_key == secret
