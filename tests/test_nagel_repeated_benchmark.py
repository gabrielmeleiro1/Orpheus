from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_repeated_benchmark_rejects_removed_chunk_size_flag(tmp_path: Path) -> None:
    from scripts import benchmark_nagel_repeated

    with pytest.raises(SystemExit):
        benchmark_nagel_repeated.parse_args(
            [str(tmp_path / "book.txt"), "--chunk-size", "700"]
        )


def test_repeated_benchmark_default_case_writes_expected_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_repeated

    source = tmp_path / "book.txt"
    source.write_text("Benchmark text.", encoding="utf-8")
    calls = []

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True)
        (output_dir / "benchmark_summary.json").write_text("{}", encoding="utf-8")
        value = float(len(calls))
        return {
            "success": True,
            "wall_time_seconds": value,
            "processing_time_seconds": value + 0.5,
            "extracted_chars": 100 + len(calls),
            "chunks": len(calls),
            "output_path": None,
            "metrics_path": str(output_dir / "benchmark_summary.json"),
            "tts_chunk_profile": {"enabled": False, "path": None, "rows": 0, "errors": 0},
        }

    monkeypatch.setattr(benchmark_nagel_repeated, "run_benchmark", fake_run_benchmark)

    exit_code = benchmark_nagel_repeated.main(
        [
            str(source),
            "--parse-only",
            "--repetitions",
            "2",
            "--output-root",
            str(tmp_path / "out"),
            "--temp-root",
            str(tmp_path / "tmp"),
            "--min-extracted-chars",
            "1",
        ]
    )

    assert exit_code == 1
    assert [
        (
            call["chunk_size"],
            call["enable_chunk_overlap"],
            call["enable_tts_file_batch_results"],
        )
        for call in calls
    ] == [
        (None, False, False),
        (None, False, False),
    ]
    assert calls[0]["output_dir"].parts[-3:] == ("default_current", "r1", "output")
    assert calls[1]["output_dir"].parts[-3:] == ("default_current", "r2", "output")

    summary = json.loads((tmp_path / "out" / "repeated_benchmark_summary.json").read_text())
    assert summary["provenance"]["source_path"] == str(source.resolve())
    assert summary["provenance"]["command"][0].endswith("benchmark_nagel_repeated.py")
    assert summary["provenance"]["environment"]["python_version"]
    assert [run["case_name"] for run in summary["runs"]] == ["default_current", "default_current"]
    assert [run["success"] for run in summary["runs"]] == [True, True]
    assert summary["parse_only"] is True
    assert "output_path" not in summary["runs"][0]["artifacts"]
    assert summary["artifact_pointers"]["summary_path"].endswith(
        "repeated_benchmark_summary.json"
    )
    assert summary["validity"] == {
        "status": "fail",
        "gates": {
            "artifacts": {"status": "pass", "missing": []},
            "provenance": {"status": "pass", "missing": []},
            "repetitions": {"status": "fail", "minimum": 3, "actual": 2},
            "thresholds": {"status": "pass", "missing": []},
            "variance": {"status": "fail", "max_spread_pct_of_median": 10.0, "violations": ["default_current/wall_time_seconds"]},
        },
    }
    default_wall = summary["case_summaries"][0]["metrics"]["wall_time_seconds"]
    assert default_wall["values"] == [1.0, 2.0]
    assert default_wall["median"] == 1.5
    assert default_wall["min"] == 1.0
    assert default_wall["max"] == 2.0
    assert default_wall["mean"] == 1.5
    assert default_wall["spread"] == 1.0
    assert default_wall["stdev"] > 0
    assert default_wall["status"] == "pass"


def test_repeated_benchmark_explicit_cases_interleave_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_repeated

    source = tmp_path / "book.txt"
    source.write_text("Benchmark text.", encoding="utf-8")
    calls = []

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True)
        return {
            "success": True,
            "wall_time_seconds": float(len(calls)),
            "metrics_path": str(output_dir / "benchmark_summary.json"),
            "tts_chunk_profile": {"enabled": False, "path": None, "rows": 0, "errors": 0},
        }

    monkeypatch.setattr(benchmark_nagel_repeated, "run_benchmark", fake_run_benchmark)

    assert benchmark_nagel_repeated.main(
        [
            str(source),
            "--parse-only",
            "--repetitions",
            "2",
            "--output-root",
            str(tmp_path / "out"),
            "--temp-root",
            str(tmp_path / "tmp"),
            "--case",
            "name=chunk_700,chunk_size=700,enable_chunk_overlap=false",
            "--case",
            (
                "name=chunk_700_overlap,chunk_size=700,"
                "enable_chunk_overlap=true,enable_tts_file_batch_results=true"
            ),
        ]
    ) == 1

    assert [
        (
            call["chunk_size"],
            call["enable_chunk_overlap"],
            call["enable_tts_file_batch_results"],
        )
        for call in calls
    ] == [
        (700, False, False),
        (700, True, True),
        (700, False, False),
        (700, True, True),
    ]
    summary = json.loads((tmp_path / "out" / "repeated_benchmark_summary.json").read_text())
    assert [run["case_name"] for run in summary["runs"]] == [
        "chunk_700",
        "chunk_700_overlap",
        "chunk_700",
        "chunk_700_overlap",
    ]


def test_repeated_benchmark_requires_three_runs_for_gate_pass_timing_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_repeated

    source = tmp_path / "book.txt"
    source.write_text("Benchmark text.", encoding="utf-8")

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True)
        output_path = output_dir / "book.m4b"
        metrics_path = output_dir / "benchmark_summary.json"
        output_path.write_text("audio", encoding="utf-8")
        metrics_path.write_text("{}", encoding="utf-8")
        return {
            "success": True,
            "wall_time_seconds": 1.0,
            "metrics_path": str(metrics_path),
            "output_path": str(output_path),
            "tts_chunk_profile": {"enabled": False, "path": None, "rows": 0, "errors": 0},
        }

    monkeypatch.setattr(benchmark_nagel_repeated, "run_benchmark", fake_run_benchmark)

    exit_code = benchmark_nagel_repeated.main(
        [
            str(source),
            "--parse-only",
            "--repetitions",
            "2",
            "--output-root",
            str(tmp_path / "out"),
            "--temp-root",
            str(tmp_path / "tmp"),
            "--max-median-wall-seconds",
            "2",
        ]
    )

    assert exit_code == 1
    summary_path = tmp_path / "out" / "repeated_benchmark_summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["case_summaries"][0]["metrics"]["wall_time_seconds"]["status"] == "pass"
    assert summary["validity"]["gates"]["repetitions"] == {
        "status": "fail",
        "minimum": 3,
        "actual": 2,
    }
    assert summary["validity"]["status"] == "fail"


def test_parse_only_repeated_benchmark_can_pass_without_output_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_repeated

    source = tmp_path / "book.txt"
    source.write_text("Benchmark text.", encoding="utf-8")

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True)
        metrics_path = output_dir / "benchmark_summary.json"
        metrics_path.write_text("{}", encoding="utf-8")
        return {
            "success": True,
            "wall_time_seconds": 1.0,
            "processing_time_seconds": 1.0,
            "metrics_path": str(metrics_path),
            "output_path": None,
            "tts_chunk_profile": {"enabled": False, "path": None, "rows": 0, "errors": 0},
        }

    monkeypatch.setattr(benchmark_nagel_repeated, "run_benchmark", fake_run_benchmark)

    exit_code = benchmark_nagel_repeated.main(
        [
            str(source),
            "--parse-only",
            "--repetitions",
            "3",
            "--output-root",
            str(tmp_path / "out"),
            "--temp-root",
            str(tmp_path / "tmp"),
            "--max-median-wall-seconds",
            "2",
        ]
    )

    assert exit_code == 0
    summary = json.loads((tmp_path / "out" / "repeated_benchmark_summary.json").read_text())
    assert summary["parse_only"] is True
    assert summary["validity"]["status"] == "pass"
    assert summary["validity"]["gates"]["artifacts"] == {"status": "pass", "missing": []}
    assert "metrics_path" in summary["runs"][0]["artifacts"]
    assert "output_path" not in summary["runs"][0]["artifacts"]


def test_repeated_benchmark_can_pass_without_wall_time_threshold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_repeated

    source = tmp_path / "book.txt"
    source.write_text("Benchmark text.", encoding="utf-8")

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True)
        metrics_path = output_dir / "benchmark_summary.json"
        metrics_path.write_text("{}", encoding="utf-8")
        return {
            "success": True,
            "wall_time_seconds": 1.0,
            "processing_time_seconds": 1.0,
            "metrics_path": str(metrics_path),
            "output_path": None,
            "tts_chunk_profile": {"enabled": False, "path": None, "rows": 0, "errors": 0},
        }

    monkeypatch.setattr(benchmark_nagel_repeated, "run_benchmark", fake_run_benchmark)

    exit_code = benchmark_nagel_repeated.main(
        [
            str(source),
            "--parse-only",
            "--repetitions",
            "3",
            "--output-root",
            str(tmp_path / "out"),
            "--temp-root",
            str(tmp_path / "tmp"),
        ]
    )

    assert exit_code == 0
    summary = json.loads((tmp_path / "out" / "repeated_benchmark_summary.json").read_text())
    assert summary["validity"]["gates"]["thresholds"] == {"status": "pass", "missing": []}


def test_full_repeated_benchmark_requires_output_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_repeated

    source = tmp_path / "book.txt"
    source.write_text("Benchmark text.", encoding="utf-8")

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True)
        metrics_path = output_dir / "benchmark_summary.json"
        metrics_path.write_text("{}", encoding="utf-8")
        return {
            "success": True,
            "wall_time_seconds": 1.0,
            "processing_time_seconds": 1.0,
            "metrics_path": str(metrics_path),
            "output_path": None,
            "tts_chunk_profile": {"enabled": False, "path": None, "rows": 0, "errors": 0},
        }

    monkeypatch.setattr(benchmark_nagel_repeated, "run_benchmark", fake_run_benchmark)

    exit_code = benchmark_nagel_repeated.main(
        [
            str(source),
            "--full",
            "--repetitions",
            "3",
            "--output-root",
            str(tmp_path / "out"),
            "--temp-root",
            str(tmp_path / "tmp"),
            "--max-median-wall-seconds",
            "2",
        ]
    )

    assert exit_code == 1
    summary = json.loads((tmp_path / "out" / "repeated_benchmark_summary.json").read_text())
    assert summary["parse_only"] is False
    assert summary["validity"]["gates"]["artifacts"] == {
        "status": "fail",
        "missing": [
            "default_current/r1/output_path",
            "default_current/r2/output_path",
            "default_current/r3/output_path",
        ],
    }


def test_threshold_status_and_missing_profile_are_reported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_repeated

    source = tmp_path / "book.txt"
    source.write_text("Benchmark text.", encoding="utf-8")

    def fake_run_benchmark(**kwargs):  # noqa: ANN001
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True)
        return {
            "success": True,
            "wall_time_seconds": 10.0,
            "processing_time_seconds": 10.0,
            "metrics_path": str(output_dir / "benchmark_summary.json"),
            "tts_chunk_profile": {
                "enabled": True,
                "path": str(output_dir / "missing.jsonl"),
                "rows": 1,
                "errors": 0,
            },
        }

    monkeypatch.setattr(benchmark_nagel_repeated, "run_benchmark", fake_run_benchmark)

    assert benchmark_nagel_repeated.main(
        [
            str(source),
            "--repetitions",
            "1",
            "--output-root",
            str(tmp_path / "out"),
            "--temp-root",
            str(tmp_path / "tmp"),
            "--max-median-wall-seconds",
            "1",
            "--profile-tts-chunks",
        ]
    ) == 1

    summary = json.loads((tmp_path / "out" / "repeated_benchmark_summary.json").read_text())
    wall = summary["case_summaries"][0]["metrics"]["wall_time_seconds"]
    assert wall["threshold"] == {"max": 1.0}
    assert wall["status"] == "fail"
    assert summary["runs"][0]["artifacts"]["tts_chunk_profile"]["exists"] is False
    assert summary["runs"][0]["profile_analysis"]["status"] == "missing"
    assert "not found" in summary["runs"][0]["profile_analysis"]["message"]


def test_tts_chunk_profile_analysis_derives_per_chunk_summaries(tmp_path: Path) -> None:
    from scripts.benchmark_nagel_repeated import analyze_tts_chunk_profile

    profile = tmp_path / "tts_chunk_profile.jsonl"
    rows = [
        {
            "call_index": 1,
            "text_chars": 100,
            "elapsed_seconds": 2.0,
            "duration_ms": 1000,
            "text_preview": "Hello, world. A short chunk!",
        },
        {
            "call_index": 2,
            "text_chars": 50,
            "elapsed_seconds": 3.0,
            "duration_ms": 500,
            "text_preview": "No punctuation clue",
        },
    ]
    profile.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    analysis = analyze_tts_chunk_profile(profile)

    assert analysis["status"] == "ok"
    assert analysis["rows"] == 2
    assert analysis["chunks"][0]["audio_duration_seconds"] == 1.0
    assert analysis["chunks"][0]["real_time_factor"] == 2.0
    assert analysis["chunks"][0]["chars_per_second"] == 50.0
    assert analysis["chunks"][0]["seconds_per_100_chars"] == 2.0
    assert analysis["chunks"][0]["punctuation"] == {
        "commas": 1,
        "periods": 1,
        "semicolons": 0,
        "colons": 0,
        "question_marks": 0,
        "exclamation_marks": 1,
    }
    assert analysis["chunks"][0]["text_clues"]["ends_with_punctuation"] is True
    assert analysis["slowest_by_elapsed"][0]["call_index"] == 2
    assert analysis["highest_real_time_factor"][0]["call_index"] == 2
