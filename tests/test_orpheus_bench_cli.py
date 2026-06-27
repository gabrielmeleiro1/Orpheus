from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_parse_command_runs_parse_only_benchmark_and_writes_command_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from orpheus import bench

    source = tmp_path / "book.md"
    source.write_text("# One\n\nA short benchmark chapter.", encoding="utf-8")
    output_root = tmp_path / "benchmarks"
    calls: list[dict[str, object]] = []

    def fake_run_benchmark(**kwargs):
        calls.append(kwargs)
        output_dir = Path(kwargs["output_dir"])
        metrics = {
            "success": True,
            "mode": "parse_only",
            "source_path": str(Path(kwargs["source_path"]).resolve()),
            "wall_time_seconds": 1.25,
            "parse_seconds": 0.5,
            "extracted_chars": 128,
            "chapters": 1,
            "chunks": 1,
            "metrics_path": str(output_dir / "benchmark_summary.json"),
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "benchmark_summary.json").write_text(
            json.dumps(metrics),
            encoding="utf-8",
        )
        return metrics

    monkeypatch.setattr(bench, "run_benchmark", fake_run_benchmark)

    exit_code = bench.main(
        [
            "parse",
            str(source),
            "--output-root",
            str(output_root),
            "--chunk-size",
            "333",
            "--chapter-limit",
            "2",
        ]
    )

    assert exit_code == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["source_path"] == source
    assert call["parse_only"] is True
    assert call["cleaner_gate"] is False
    assert call["chunk_size"] == 333
    assert call["chapter_limit"] == 2
    summary = json.loads((output_root / "bench_summary.json").read_text())
    assert summary["schema_version"] == 1
    assert summary["command"] == "parse"
    assert summary["status"] == "pass"
    assert summary["runs"][0]["mode"] == "parse_only"
    assert summary["runs"][0]["metrics_path"].endswith("benchmark_summary.json")


def test_clean_command_runs_cleaner_gate_without_tts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from orpheus import bench

    source = tmp_path / "book.md"
    source.write_text("# One\n\nA short benchmark chapter.", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def fake_run_benchmark(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "mode": "cleaner_gate",
            "source_path": str(Path(kwargs["source_path"]).resolve()),
            "wall_time_seconds": 2.0,
            "parse_seconds": 0.25,
            "cleaner_gate_rule_only_chunks": 3,
            "cleaner_gate_llm_needed_chunks": 1,
            "metrics_path": str(Path(kwargs["output_dir"]) / "benchmark_summary.json"),
        }

    monkeypatch.setattr(bench, "run_benchmark", fake_run_benchmark)

    exit_code = bench.main(["clean", str(source), "--output-root", str(tmp_path / "out")])

    assert exit_code == 0
    assert calls[0]["parse_only"] is False
    assert calls[0]["cleaner_gate"] is True


def test_full_command_runs_full_pipeline_with_tts_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from orpheus import bench

    source = tmp_path / "book.md"
    source.write_text("# One\n\nA short benchmark chapter.", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def fake_run_benchmark(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "mode": "full",
            "source_path": str(Path(kwargs["source_path"]).resolve()),
            "wall_time_seconds": 5.0,
            "tts_seconds_total": 3.0,
            "total_duration_ms": 10_000,
            "metrics_path": str(Path(kwargs["output_dir"]) / "benchmark_summary.json"),
        }

    monkeypatch.setattr(bench, "run_benchmark", fake_run_benchmark)

    exit_code = bench.main(
        [
            "full",
            str(source),
            "--output-root",
            str(tmp_path / "out"),
            "--profile-tts-chunks",
            "--max-parallel-chapters",
            "1",
        ]
    )

    assert exit_code == 0
    assert calls[0]["parse_only"] is False
    assert calls[0]["cleaner_gate"] is False
    assert calls[0]["profile_tts_chunks"] is True
    assert calls[0]["max_parallel_chapters"] == 1
    summary = json.loads((tmp_path / "out" / "bench_summary.json").read_text())
    assert summary["runs"][0]["audio_duration_seconds"] == 10.0
    assert summary["runs"][0]["audio_realtime_factor"] == 2.0


def test_suite_command_runs_requested_modes_for_multiple_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from orpheus import bench

    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# One\n\nFirst chapter.", encoding="utf-8")
    second.write_text("# One\n\nSecond chapter.", encoding="utf-8")
    output_root = tmp_path / "suite"
    calls: list[dict[str, object]] = []

    def fake_run_benchmark(**kwargs):
        calls.append(kwargs)
        source_path = Path(kwargs["source_path"])
        mode = "cleaner_gate" if kwargs["cleaner_gate"] else "parse_only"
        return {
            "success": True,
            "mode": mode,
            "source_path": str(source_path.resolve()),
            "wall_time_seconds": 1.0,
            "parse_seconds": 0.25,
            "extracted_chars": 42,
            "chapters": 1,
            "chunks": 1,
            "metrics_path": str(Path(kwargs["output_dir"]) / "benchmark_summary.json"),
        }

    monkeypatch.setattr(bench, "run_benchmark", fake_run_benchmark)

    exit_code = bench.main(
        [
            "suite",
            str(first),
            str(second),
            "--modes",
            "parse,clean",
            "--output-root",
            str(output_root),
        ]
    )

    assert exit_code == 0
    assert len(calls) == 4
    summary = json.loads((output_root / "bench_summary.json").read_text())
    assert summary["command"] == "suite"
    assert summary["status"] == "pass"
    assert [run["label"] for run in summary["runs"]] == [
        "first",
        "first",
        "second",
        "second",
    ]
    assert [run["command"] for run in summary["runs"]] == [
        "parse",
        "clean",
        "parse",
        "clean",
    ]
    assert all(run["success"] is True for run in summary["runs"])
    assert all(run["wall_time_seconds"] == 1.0 for run in summary["runs"])
    assert all(run["parse_seconds"] == 0.25 for run in summary["runs"])
    assert all(run["metrics_path"].endswith("benchmark_summary.json") for run in summary["runs"])
    assert all(run["extracted_chars"] == 42 for run in summary["runs"])


def test_cli_clears_stale_per_run_artifacts_before_benchmarking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from orpheus import bench

    source = tmp_path / "book.md"
    source.write_text("# One\n\nA short benchmark chapter.", encoding="utf-8")
    output_root = tmp_path / "out"
    stale = output_root / "parse" / "book" / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("old artifact", encoding="utf-8")

    def fake_run_benchmark(**kwargs):
        assert not stale.exists()
        return {
            "success": True,
            "mode": "parse_only",
            "source_path": str(Path(kwargs["source_path"]).resolve()),
            "wall_time_seconds": 1.0,
            "metrics_path": str(Path(kwargs["output_dir"]) / "benchmark_summary.json"),
        }

    monkeypatch.setattr(bench, "run_benchmark", fake_run_benchmark)

    assert (
        bench.main(["parse", str(source), "--output-root", str(output_root)])
        == 0
    )


def test_suite_uses_unique_run_dirs_for_same_stem_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from orpheus import bench

    first = tmp_path / "a" / "book.md"
    second = tmp_path / "b" / "book.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("# One\n\nFirst.", encoding="utf-8")
    second.write_text("# One\n\nSecond.", encoding="utf-8")
    output_dirs: list[Path] = []

    def fake_run_benchmark(**kwargs):
        output_dirs.append(Path(kwargs["output_dir"]))
        return {
            "success": True,
            "mode": "parse_only",
            "source_path": str(Path(kwargs["source_path"]).resolve()),
            "wall_time_seconds": 1.0,
            "metrics_path": str(Path(kwargs["output_dir"]) / "benchmark_summary.json"),
        }

    monkeypatch.setattr(bench, "run_benchmark", fake_run_benchmark)

    assert (
        bench.main(
            [
                "suite",
                str(first),
                str(second),
                "--modes",
                "parse",
                "--output-root",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )

    assert len(output_dirs) == 2
    assert output_dirs[0] != output_dirs[1]
    assert output_dirs[0].name.startswith("01-")
    assert output_dirs[1].name.startswith("02-")


def test_module_cli_stdout_is_json_for_markdown_parse(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("# One\n\nA short benchmark chapter.", encoding="utf-8")
    output_root = tmp_path / "out"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "orpheus",
            "bench",
            "parse",
            str(source),
            "--output-root",
            str(output_root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["command"] == "parse"
    assert payload["status"] == "pass"


def test_benchmark_runner_stdout_is_redirected_to_stderr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from orpheus import bench

    source = tmp_path / "book.md"
    source.write_text("# One\n\nA short benchmark chapter.", encoding="utf-8")

    def noisy_run_benchmark(**kwargs):
        print("runtime log should not corrupt stdout JSON")
        return {
            "success": True,
            "mode": "full",
            "source_path": str(Path(kwargs["source_path"]).resolve()),
            "wall_time_seconds": 1.0,
            "metrics_path": str(Path(kwargs["output_dir"]) / "benchmark_summary.json"),
        }

    monkeypatch.setattr(bench, "run_benchmark", noisy_run_benchmark)

    assert (
        bench.main(["full", str(source), "--output-root", str(tmp_path / "out")])
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["command"] == "full"
    assert "runtime log should not corrupt stdout JSON" not in captured.out
    assert "runtime log should not corrupt stdout JSON" in captured.err


def test_module_entrypoint_dispatches_to_bench_subcommand(monkeypatch) -> None:
    import orpheus.__main__ as entrypoint

    calls: list[list[str]] = []

    def fake_bench_main(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(entrypoint.bench, "main", fake_bench_main)

    assert entrypoint.main(["bench", "parse", "book.md"]) == 0
    assert calls == [["parse", "book.md"]]
