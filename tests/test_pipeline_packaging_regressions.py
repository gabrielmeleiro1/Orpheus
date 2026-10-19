"""Regression coverage for pipeline packaging."""

from __future__ import annotations

import pytest

from tests._factories_group2_conversion import _probe


@pytest.mark.parametrize("emit_mp3", [False, True])
@pytest.mark.parametrize("background", [False, True])
def test_aac_completion_releases_wav_and_resolution_attaches_the_encoded_path(conversion, emit_mp3, background):
    from modules.pipeline.stages.packaging import resolve_background_audio_futures

    source, _, make_pipeline = conversion
    pipeline = make_pipeline(
        emit_chapter_mp3_artifacts=emit_mp3, enable_chapter_aac_packaging=True,
        enable_background_chapter_aac_encoding=background,
    )
    chapters_dir = pipeline.config.output_dir / "chapters"
    chapters_dir.mkdir()
    chapter = pipeline.stages.ingest(source).chapters[0]
    try:
        result = pipeline.stages.process_chapter(chapter, 0, 1, chapters_dir)
        assert result.error is None
        if background:
            future = result.aac_future
            encoded = future.result(timeout=2)
            assert encoded.is_file()
            assert result.aac_future is future
            assert result.aac_path is None
            assert (result.mp3_path is not None) is emit_mp3
            assert result.wav_path is None
            resolve_background_audio_futures([result], runtime=pipeline.stages.runtime)
        assert result.aac_future is None
        assert result.wav_path is None
        assert result.aac_path.is_file()
        if emit_mp3:
            assert result.mp3_path.suffix == ".mp3"
            assert result.aac_path.parent == pipeline.config.temp_dir
        else:
            assert result.mp3_path == result.aac_path
            assert result.aac_path.parent == chapters_dir
        assert _probe(result.aac_path)["streams"][0]["codec_name"] == "aac"
    finally:
        pipeline._finish_conversion_run()
    assert result.mp3_path.is_file()
    assert result.aac_path.exists() is (not emit_mp3)
    assert not list(pipeline.config.temp_dir.rglob("*.wav"))
    assert pipeline.stages.runtime.state.chapter_aac_executor is None


def test_failed_background_encode_keeps_wav_until_run_cleanup_and_prevents_packaging(conversion):
    source, _, make_pipeline = conversion
    pipeline = make_pipeline(enable_chapter_aac_packaging=True)
    chapters_dir = pipeline.config.output_dir / "chapters"
    blocked_output = chapters_dir / "01_Zebra Crossing.m4a"
    blocked_output.mkdir(parents=True)
    chapter = pipeline.stages.ingest(source).chapters[0]
    try:
        result = pipeline.stages.process_chapter(chapter, 0, 1, chapters_dir)
        future = result.aac_future
        with pytest.raises(OSError):
            future.result(timeout=2)
        master = result.wav_path
        assert master.is_file()
        assert result.aac_path is None and result.mp3_path is None
        with pytest.raises(OSError):
            pipeline.stages.package([result], "Tiny Book", None, pipeline.config.output_dir)
        assert result.aac_future is future
        assert not (pipeline.config.output_dir / "Tiny Book.m4b").exists()
    finally:
        pipeline._finish_conversion_run()
    assert not master.exists()
    assert pipeline.stages.runtime.state.chapter_aac_executor is None
    assert {path.name for path in chapters_dir.iterdir()} == {
        blocked_output.name, "chapter_01_before_cleaning.md", "chapter_01_cleaned.md",
    }
