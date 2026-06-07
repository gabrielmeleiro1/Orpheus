"""
Test pipeline packaging diagnostics.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.audio as audio_module
from modules.pipeline.orchestrator import ChapterResult, ConversionPipeline, PipelineConfig


def _test_package_error_includes_chapter_failure_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config=cfg)

        chapter_results = [
            ChapterResult(chapter_number=1, chapter_title="One", error="mlx runtime unavailable"),
            ChapterResult(chapter_number=2, chapter_title="Two", error="ffmpeg encoding failed"),
        ]

        try:
            pipeline.stages.package(  # noqa: SLF001 - intentional direct test of failure path
                chapter_results=chapter_results,
                title="Book",
                author="Author",
                output_dir=cfg.output_dir,
            )
            raise AssertionError("expected RuntimeError for missing mp3 files")
        except RuntimeError as exc:
            message = str(exc)
            assert "Refusing to package incomplete chapter set" in message
            assert "mlx runtime unavailable" in message
            assert "ffmpeg encoding failed" in message

    print("✓ package error includes chapter failure context")


def _test_package_passes_known_chapter_durations_to_packager() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config=cfg)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

        chapter_one = cfg.output_dir / "01_one.mp3"
        chapter_two = cfg.output_dir / "02_two.mp3"
        chapter_one.write_bytes(b"one")
        chapter_two.write_bytes(b"two")

        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one,
                duration_ms=1111,
            ),
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
                mp3_path=chapter_two,
                duration_ms=2222,
            ),
        ]

        with patch("modules.audio.packager.M4BPackager") as packager_cls:
            packager = packager_cls.return_value
            expected_out = cfg.output_dir / "Book.m4b"
            packager.create_m4b.return_value = expected_out

            output = pipeline.stages.package(  # noqa: SLF001 - intentional direct test
                chapter_results=chapter_results,
                title="Book",
                author="Author",
                output_dir=cfg.output_dir,
            )

            assert output == expected_out
            assert packager.create_m4b.call_count == 1
            kwargs = packager.create_m4b.call_args.kwargs
            assert kwargs["chapter_durations_ms"] == [1111, 2222]

    print("✓ package passes known chapter durations to packager")


def _test_package_prefers_wav_masters_over_mp3_chapter_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config=cfg)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

        chapter_one_wav = cfg.output_dir / "01_one.wav"
        chapter_two_wav = cfg.output_dir / "02_two.wav"
        chapter_one_mp3 = cfg.output_dir / "01_one.mp3"
        chapter_two_mp3 = cfg.output_dir / "02_two.mp3"
        chapter_one_wav.write_bytes(b"one wav")
        chapter_two_wav.write_bytes(b"two wav")
        chapter_one_mp3.write_bytes(b"one mp3")
        chapter_two_mp3.write_bytes(b"two mp3")

        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                wav_path=chapter_one_wav,
                mp3_path=chapter_one_mp3,
                duration_ms=1111,
            ),
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
                wav_path=chapter_two_wav,
                mp3_path=chapter_two_mp3,
                duration_ms=2222,
            ),
        ]

        with patch("modules.audio.packager.M4BPackager") as packager_cls:
            packager = packager_cls.return_value
            expected_out = cfg.output_dir / "Book.m4b"
            packager.create_m4b.return_value = expected_out

            output = pipeline.stages.package(  # noqa: SLF001 - intentional direct test
                chapter_results=chapter_results,
                title="Book",
                author="Author",
                output_dir=cfg.output_dir,
            )

            assert output == expected_out
            kwargs = packager.create_m4b.call_args.kwargs
            assert kwargs["mp3_files"] == [chapter_one_wav, chapter_two_wav]

    print("✓ package prefers wav masters over mp3 chapter artifacts")


def _test_package_rejects_directory_audio_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config=cfg)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

        directory_wav = cfg.output_dir / "01_one.wav"
        directory_mp3 = cfg.output_dir / "02_two.mp3"
        directory_wav.mkdir()
        directory_mp3.mkdir()

        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                wav_path=directory_wav,
                duration_ms=1111,
            ),
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
                mp3_path=directory_mp3,
                duration_ms=2222,
            ),
        ]

        try:
            pipeline.stages.package(  # noqa: SLF001 - intentional direct test
                chapter_results=chapter_results,
                title="Book",
                author="Author",
                output_dir=cfg.output_dir,
            )
            raise AssertionError("expected RuntimeError for directory audio artifacts")
        except RuntimeError as exc:
            message = str(exc)
            assert "Refusing to package incomplete chapter set" in message
            assert f"missing WAV file at {directory_wav}" in message
            assert f"missing MP3 file at {directory_mp3}" in message

    print("✓ package rejects directory audio artifacts")


def _test_package_outputs_m4b_for_audiobook_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
            conversion_mode="audiobook",
        )
        pipeline = ConversionPipeline(config=cfg)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

        chapter_one = cfg.output_dir / "01_one.mp3"
        chapter_one.write_bytes(b"one")

        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one,
                duration_ms=1111,
            ),
        ]

        with patch("modules.audio.packager.M4BPackager") as packager_cls:
            packager = packager_cls.return_value
            expected_out = cfg.output_dir / "Book.m4b"
            packager.create_m4b.return_value = expected_out

            output = pipeline.stages.package(  # noqa: SLF001 - intentional direct test
                chapter_results=chapter_results,
                title="Book",
                author="Author",
                output_dir=cfg.output_dir,
            )

            assert output == expected_out
            assert output.suffix == ".m4b"
            assert packager.create_m4b.call_count == 1

    print("✓ package preserves audiobook m4b output")


def _test_package_outputs_m4a_for_document_audio_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
            conversion_mode="document_audio",
        )
        pipeline = ConversionPipeline(config=cfg)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

        chapter_one = cfg.output_dir / "01_one.mp3"
        chapter_two = cfg.output_dir / "02_two.mp3"
        chapter_one.write_bytes(b"one")
        chapter_two.write_bytes(b"two")

        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one,
                duration_ms=1111,
            ),
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
                mp3_path=chapter_two,
                duration_ms=2222,
            ),
        ]

        assert hasattr(audio_module, "DocumentAudioPackager")

        with patch("modules.audio.document_packager.DocumentAudioPackager") as packager_cls:
            packager = packager_cls.return_value
            expected_out = cfg.output_dir / "Book.m4a"
            packager.create_m4a.return_value = expected_out

            output = pipeline.stages.package(  # noqa: SLF001 - intentional direct test
                chapter_results=chapter_results,
                title="Book",
                author="Author",
                output_dir=cfg.output_dir,
            )

            assert output == expected_out
            assert output.suffix == ".m4a"
            assert packager.create_m4a.call_count == 1

    print("✓ package switches document audio output to m4a")


def _test_package_rejects_incomplete_chapter_sets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config=cfg)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

        chapter_one = cfg.output_dir / "01_one.mp3"
        chapter_one.write_bytes(b"one")

        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one,
                duration_ms=1111,
            ),
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
            ),
        ]

        try:
            pipeline.stages.package(  # noqa: SLF001 - intentional direct test
                chapter_results=chapter_results,
                title="Book",
                author="Author",
                output_dir=cfg.output_dir,
            )
            raise AssertionError("expected RuntimeError for incomplete chapter set")
        except RuntimeError as exc:
            message = str(exc)
            assert "incomplete" in message.lower()
            assert "chapter 2" in message.lower()

    print("✓ package rejects incomplete chapter sets")


def _test_document_audio_rejects_incomplete_section_sets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
            conversion_mode="document_audio",
        )
        pipeline = ConversionPipeline(config=cfg)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

        chapter_one = cfg.output_dir / "01_one.mp3"
        chapter_one.write_bytes(b"one")

        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one,
                duration_ms=1111,
            ),
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
                mp3_path=cfg.output_dir / "02_two.mp3",
                duration_ms=2222,
            ),
        ]

        try:
            pipeline.stages.package(  # noqa: SLF001 - intentional direct test
                chapter_results=chapter_results,
                title="Book",
                author="Author",
                output_dir=cfg.output_dir,
            )
            raise AssertionError("expected RuntimeError for incomplete document audio set")
        except RuntimeError as exc:
            message = str(exc).lower()
            assert "incomplete" in message
            assert "chapter 2" in message

    print("✓ document audio rejects incomplete section sets")


def test_pipeline_packaging() -> bool:
    print("\n" + "=" * 50)
    print("PIPELINE PACKAGING TEST SUITE")
    print("=" * 50 + "\n")

    _test_package_error_includes_chapter_failure_context()
    _test_package_passes_known_chapter_durations_to_packager()
    _test_package_prefers_wav_masters_over_mp3_chapter_artifacts()
    _test_package_rejects_directory_audio_artifacts()
    _test_package_outputs_m4b_for_audiobook_mode()
    _test_package_outputs_m4a_for_document_audio_mode()
    _test_package_rejects_incomplete_chapter_sets()
    _test_document_audio_rejects_incomplete_section_sets()

    print("\n" + "=" * 50)
    print("ALL PIPELINE PACKAGING TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_pipeline_packaging()
    sys.exit(0 if success else 1)
