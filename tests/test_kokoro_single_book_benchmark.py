from __future__ import annotations

import json


def test_kokoro_single_book_benchmark_builds_matrix_without_running_real_benchmark(
    tmp_path,
    monkeypatch,
) -> None:
    from benchmarks.kokoro_single_book import run_kokoro_single_book

    source = tmp_path / "book.txt"
    source.write_text("chapter text", encoding="utf-8")
    calls = []

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {
            "success": True,
            "wall_time_seconds": len(calls),
            "tts_seconds_total": len(calls) + 0.25,
            "chunks": 3,
            "output_path": str(kwargs["output_dir"] / "book.m4b"),
            "metrics_path": str(kwargs["output_dir"] / "benchmark_summary.json"),
        }

    monkeypatch.setattr(run_kokoro_single_book, "run_benchmark", fake_run_benchmark)

    exit_code = run_kokoro_single_book.main(
        [
            str(source),
            "--output-root",
            str(tmp_path / "out"),
            "--temp-root",
            str(tmp_path / "tmp"),
            "--chunk-sizes",
            "500",
            "500",
            "700",
            "--include-overlap",
        ]
    )

    assert exit_code == 0
    assert [call["enable_tts_file_batch_results"] for call in calls] == [
        False,
        True,
        True,
        True,
        True,
    ]
    assert [call["chunk_size"] for call in calls] == [500, 500, 500, 700, 700]
    assert [call["enable_chunk_overlap"] for call in calls] == [False, False, True, False, True]

    summary = json.loads((tmp_path / "out" / "kokoro_single_book_summary.json").read_text())
    assert [case["name"] for case in summary["cases"]] == [
        "baseline_array_500",
        "file_result_500",
        "file_result_500_overlap",
        "file_result_700",
        "file_result_700_overlap",
    ]
    assert summary["fastest_by_mean_wall_time"]["name"] == "baseline_array_500"


def test_kokoro_single_book_benchmark_repetitions_and_complete_matrix(
    tmp_path,
    monkeypatch,
) -> None:
    from benchmarks.kokoro_single_book import run_kokoro_single_book

    source = tmp_path / "book.txt"
    source.write_text("chapter text", encoding="utf-8")
    calls = []

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        run_index = len(calls)
        return {
            "success": True,
            "wall_time_seconds": float(run_index),
            "processing_time_seconds": float(run_index),
            "tts_seconds_total": float(run_index) + 0.5,
            "chunks": kwargs["chunk_size"] // 100,
            "chapters": 1,
            "output_path": str(kwargs["output_dir"] / "book.m4b"),
            "metrics_path": str(kwargs["output_dir"] / "benchmark_summary.json"),
        }

    monkeypatch.setattr(run_kokoro_single_book, "run_benchmark", fake_run_benchmark)

    exit_code = run_kokoro_single_book.main(
        [
            str(source),
            "--output-root",
            str(tmp_path / "out"),
            "--temp-root",
            str(tmp_path / "tmp"),
            "--chunk-sizes",
            "700",
            "900",
            "--include-array-baselines",
            "--include-overlap",
            "--repetitions",
            "2",
        ]
    )

    assert exit_code == 0
    assert len(calls) == 12
    assert [call["chunk_size"] for call in calls[:6]] == [700, 700, 700, 900, 900, 900]
    assert [call["enable_tts_file_batch_results"] for call in calls[:3]] == [
        False,
        True,
        True,
    ]
    assert [call["enable_chunk_overlap"] for call in calls[:3]] == [False, False, True]
    assert calls[0]["output_dir"].name == "r1"
    assert calls[6]["output_dir"].name == "r2"

    summary = json.loads((tmp_path / "out" / "kokoro_single_book_summary.json").read_text())
    assert summary["repetitions"] == 2
    assert [case["name"] for case in summary["case_summaries"]] == [
        "baseline_array_700",
        "file_result_700",
        "file_result_700_overlap",
        "baseline_array_900",
        "file_result_900",
        "file_result_900_overlap",
    ]
    assert summary["case_summaries"][0]["runs"] == 2
    assert summary["case_summaries"][0]["wall_time_seconds_mean"] == 4.0
    assert summary["case_summaries"][0]["wall_time_seconds_min"] == 1.0
