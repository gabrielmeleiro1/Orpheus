"""Finding D: benchmark fidelity, production profile, and timing attribution."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_production_benchmark_profile_enables_chapter_aac_by_default(
    tmp_path: Path,
) -> None:
    from modules.app.config import AppConfig
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark body text. " * 40, encoding="utf-8")

    config = benchmark_nagel_pipeline._build_config(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        benchmark_profile="production",
    )

    assert config.enable_chapter_aac_packaging is AppConfig().enable_chapter_aac_packaging
    assert config.enable_chapter_aac_packaging is True


def test_benchmark_records_effective_options_and_profile(
    tmp_path: Path,
) -> None:
    from scripts.benchmark_nagel_pipeline import BENCHMARK_SCHEMA_KEYS, run_benchmark

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark body text. " * 40, encoding="utf-8")

    metrics = run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=True,
        min_extracted_chars=100,
        benchmark_profile="production",
    )

    assert set(metrics) == set(BENCHMARK_SCHEMA_KEYS)
    assert metrics["benchmark_profile"] == "production"
    effective = metrics["effective_options"]
    assert isinstance(effective, dict)
    assert effective["enable_chapter_aac_packaging"] is True
    assert effective["benchmark_profile"] == "production"
    assert effective["chunk_size"] == metrics["chunk_size"]
    assert effective["cleaner_backend"] == metrics["cleaner_backend"]


def test_full_benchmark_records_model_warmup_and_residual_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.pipeline.models import ChapterResult, ConversionResult
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark full mode body text. " * 40, encoding="utf-8")

    class _FakePipeline:
        def __init__(self, config) -> None:  # noqa: ANN001
            self.config = config

            def _warmup_models() -> None:
                time.sleep(0.01)

            self.stages = SimpleNamespace(
                chapter_processor=SimpleNamespace(),
                warmup_models=_warmup_models,
                ingest=lambda _source: SimpleNamespace(
                    title="Fake",
                    author="Tester",
                    chapters=[
                        SimpleNamespace(
                            number=1,
                            title="Chapter 1",
                            content="Benchmark full mode body text. " * 40,
                        )
                    ],
                ),
            )

        def convert(self, _source_path: Path) -> ConversionResult:
            self.stages.warmup_models()
            chapters_dir = self.config.output_dir / "Fake" / "chapters"
            chapters_dir.mkdir(parents=True)
            (chapters_dir / "chapter_01_before_cleaning.md").write_text(
                "# Chapter 1\n\n" + ("Benchmark full mode body text. " * 40),
                encoding="utf-8",
            )
            (chapters_dir / "chapter_01_cleaned.md").write_text(
                "# Chapter 1\n\n" + ("Benchmark full mode body text. " * 40),
                encoding="utf-8",
            )
            output_path = tmp_path / "book.m4b"
            output_path.write_bytes(b"m4b")
            return ConversionResult(
                success=True,
                title="Fake",
                author="Tester",
                output_path=output_path,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="Chapter 1",
                        duration_ms=1000,
                        stage_timings={
                            "clean": 0.1,
                            "tts": 0.2,
                            "encode": 0.3,
                            "package": 0.0,
                            "tts_model_load": 0.4,
                        },
                    )
                ],
                total_duration_ms=1000,
                processing_time_seconds=1.0,
            )

        def _clean_chapter_content(self, *_args, **_kwargs):
            time.sleep(0.001)
            return ["text"], False

        def _synthesize_chapter_chunks(self, *_args, **_kwargs):
            time.sleep(0.001)
            return tmp_path / "chapter.wav"

        def _encode_chapter_audio(self, *_args, **_kwargs):
            time.sleep(0.001)
            return tmp_path / "chapter.mp3", 1000

        def _package(self, *_args, **_kwargs):
            time.sleep(0.001)
            return tmp_path / "book.m4b"

        def _ingest(self, _source_path):
            time.sleep(0.001)
            return self.stages.ingest(_source_path)

        def _ensure_chapter_tts_engine(self):
            time.sleep(0.001)

    monkeypatch.setattr(benchmark_nagel_pipeline, "ConversionPipeline", _FakePipeline)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=False,
        min_extracted_chars=100,
        benchmark_profile="production",
    )

    assert metrics["success"] is True
    assert metrics["model_warmup_seconds_total"] > 0
    assert metrics["encode_seconds_total"] == 0.3
    assert metrics["tts_model_load_seconds_total"] == 0.4
    assert metrics["attributed_seconds_total"] > 0
    assert metrics["residual_seconds_total"] >= 0
    expected_residual = max(
        0.0,
        float(metrics["wall_time_seconds"]) - float(metrics["attributed_seconds_total"]),
    )
    assert metrics["residual_seconds_total"] == round(expected_residual, 4)


def test_timing_attribution_counts_clean_tts_overlap_once() -> None:
    """Concurrent clean/tts work consumes wall time once, not three times.

    Bug 4: clean, tts, and clean_tts_overlap all record the same overlapped
    region, so a naive sum triple-counts it and forces the residual to zero.
    """
    from scripts.benchmark_nagel_pipeline import compute_timing_attribution

    overlapped = {
        "wall_time_seconds": 10.0,
        "clean_seconds_total": 4.0,
        "tts_seconds_total": 4.0,
        "clean_tts_overlap_seconds_total": 4.0,
        "encode_seconds_total": 1.0,
    }

    attributed, residual = compute_timing_attribution(overlapped)

    # 4s overlapped clean/tts counted once + 1s encode = 5s attributed.
    assert attributed <= overlapped["wall_time_seconds"]
    assert attributed == 5.0
    assert residual == 5.0

    # The non-overlap path stays additive.
    sequential = {
        "wall_time_seconds": 10.0,
        "clean_seconds_total": 3.0,
        "tts_seconds_total": 5.0,
        "clean_tts_overlap_seconds_total": 0.0,
    }
    seq_attributed, seq_residual = compute_timing_attribution(sequential)
    assert seq_attributed == 8.0
    assert seq_residual == 2.0


def test_corpus_summary_includes_full_stage_timings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import run_optimization_corpus

    source = tmp_path / "book.txt"
    source.write_text("text", encoding="utf-8")

    monkeypatch.setenv("ORPHEUS_TEST_BOOK", str(source))
    monkeypatch.setattr(
        run_optimization_corpus,
        "CORPUS_CASES",
        (
            {
                "book_key": "book",
                "source_env": "ORPHEUS_TEST_BOOK",
            },
        ),
    )

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        return {
            "artifact_quality": {"success": True, "issues": [], "cleaned_chars": 10},
            "success": True,
            "wall_time_seconds": 10.0,
            "processing_time_seconds": 9.5,
            "parse_seconds": 1.0,
            "chunk_seconds": 0.2,
            "pipeline_parse_seconds": 0.8,
            "model_warmup_seconds_total": 2.0,
            "clean_seconds_total": 1.5,
            "tts_seconds_total": 2.5,
            "tts_model_load_seconds_total": 0.5,
            "encode_seconds_total": 0.7,
            "package_seconds_total": 0.3,
            "attributed_seconds_total": 8.5,
            "residual_seconds_total": 1.5,
            "benchmark_profile": "production",
            "effective_options": {"enable_chapter_aac_packaging": True},
            "source_hash": "hash",
            "source_path": str(source),
            "metrics_path": "metrics.json",
            "chapter_titles": ["One"],
            "chunk_size": 700,
            "chunks": 3,
        }

    monkeypatch.setattr(run_optimization_corpus, "run_benchmark", fake_run_benchmark)

    summary = run_optimization_corpus.run_corpus(
        run_name="timing-test",
        artifacts_root=tmp_path / "artifacts",
    )

    case = summary["cases"][0]
    assert case["encode_seconds_total"] == 0.7
    assert case["tts_model_load_seconds_total"] == 0.5
    assert case["model_warmup_seconds_total"] == 2.0
    assert case["residual_seconds_total"] == 1.5
    assert case["attributed_seconds_total"] == 8.5
    assert summary["benchmark_profile"] == "production"
    assert summary["effective_options"]["enable_chapter_aac_packaging"] is True

    aggregate = summary["aggregate"]
    assert aggregate["stage_seconds"]["encode"]["median"] == 0.7
    assert aggregate["stage_seconds"]["model_warmup"]["median"] == 2.0
    assert aggregate["stage_seconds"]["residual"]["median"] == 1.5


def test_repeated_corpus_distinguishes_cold_and_warm_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import run_optimization_corpus

    source = tmp_path / "book.txt"
    source.write_text("text", encoding="utf-8")
    calls: list[dict[str, object]] = []

    monkeypatch.setenv("ORPHEUS_TEST_BOOK", str(source))
    monkeypatch.setattr(
        run_optimization_corpus,
        "CORPUS_CASES",
        (
            {
                "book_key": "book",
                "source_env": "ORPHEUS_TEST_BOOK",
            },
        ),
    )

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        run_index = len(calls)
        return {
            "artifact_quality": {"success": True, "issues": []},
            "success": True,
            "wall_time_seconds": float(run_index),
            "processing_time_seconds": float(run_index),
            "model_warmup_seconds_total": 3.0 if run_index == 1 else 0.1,
            "attributed_seconds_total": float(run_index) - 0.5,
            "residual_seconds_total": 0.5,
            "source_hash": "hash",
            "source_path": str(source),
            "metrics_path": f"metrics-{run_index}.json",
        }

    monkeypatch.setattr(run_optimization_corpus, "run_benchmark", fake_run_benchmark)

    summary = run_optimization_corpus.run_repeated_corpus(
        repeat=3,
        run_name="cold-warm-test",
        artifacts_root=tmp_path / "artifacts",
    )

    assert summary["aggregate"]["cold_run"]["wall_time_seconds"]["total"] == 1.0
    assert summary["aggregate"]["warm_runs"]["wall_time_seconds"]["total"] == 5.0
    assert summary["aggregate"]["cold_run"]["model_warmup_seconds"]["total"] == 3.0
    assert summary["aggregate"]["warm_runs"]["model_warmup_seconds"]["total"] == 0.2
    assert calls[0]["benchmark_profile"] == "production"
    assert calls[0]["enable_chapter_aac_packaging"] is True


def test_parse_only_benchmark_computes_residual_from_final_wall_time(
    tmp_path: Path,
) -> None:
    from scripts.benchmark_nagel_pipeline import run_benchmark

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark body text. " * 40, encoding="utf-8")

    metrics = run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=True,
        min_extracted_chars=100,
        benchmark_profile="minimal",
    )

    wall = float(metrics["wall_time_seconds"])
    attributed = float(metrics["attributed_seconds_total"])
    residual = float(metrics["residual_seconds_total"])
    assert wall > 0
    assert residual == round(max(0.0, wall - attributed), 4)
    assert residual > 0 or attributed > 0
    assert attributed + residual <= wall + 0.0001
