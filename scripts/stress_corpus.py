#!/usr/bin/env python3
"""Run corpus stress sweeps and group failures into actionable patterns."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import shutil
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_MANIFEST_PATH = REPO_ROOT / "benchmarks" / "stress_corpus" / "manifest.tsv"
SUPPORTED_EXTENSIONS = {".epub", ".pdf", ".txt", ".md"}
BULKY_METRIC_KEYS = {"tts_chunk_profile"}


@dataclass(frozen=True)
class ManifestRow:
    gutenberg_id: str
    slug: str
    stress_dimension: str


@dataclass(frozen=True)
class BookSource:
    slug: str
    path: Path
    size_bytes: int


def _ascii_safe(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def _slug_from_path(path: Path) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "-" for char in path.stem)
    return cleaned.strip(".-_") or "book"


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> list[ManifestRow]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    if header != ["gutenberg_id", "slug", "stress_dimension"]:
        raise ValueError("manifest header must be: gutenberg_id, slug, stress_dimension")
    rows: list[ManifestRow] = []
    for line_number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise ValueError(f"manifest row {line_number} must have exactly 3 columns")
        rows.append(ManifestRow(*parts))
    return rows


def discover_books(
    books_dir: Path,
    *,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> list[BookSource]:
    books_dir = Path(books_dir)
    include_patterns = include_patterns or ["*"]
    exclude_patterns = exclude_patterns or []
    discovered: list[BookSource] = []
    for path in sorted(books_dir.rglob("*"), key=lambda item: str(item).lower()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        name = path.name
        slug = _slug_from_path(path)
        if not any(fnmatch(name, pattern) or fnmatch(slug, pattern) for pattern in include_patterns):
            continue
        if any(fnmatch(name, pattern) or fnmatch(slug, pattern) for pattern in exclude_patterns):
            continue
        discovered.append(BookSource(slug=slug, path=path, size_bytes=path.stat().st_size))
    return discovered


def _load_ledger_rows(ledger_path: Path) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def apply_resume_filter(
    books: list[BookSource],
    *,
    ledger_path: Path,
    resume: bool,
) -> list[BookSource]:
    if not resume:
        return books
    done = {str(row.get("slug")) for row in _load_ledger_rows(ledger_path) if row.get("slug")}
    return [book for book in books if book.slug not in done]


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(ordered[lower], 4)
    weight = rank - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 4)


def _stats(values: list[float]) -> dict[str, float | None]:
    return {
        "min": _percentile(values, 0.0),
        "median": _percentile(values, 0.5),
        "p90": _percentile(values, 0.9),
        "max": _percentile(values, 1.0),
    }


def _error_type(error: Any) -> str:
    if not error:
        return "unknown"
    text = str(error).strip()
    if ":" in text:
        return text.split(":", 1)[0].strip() or "unknown"
    return text.split()[0] if text.split() else "unknown"


def _clean_excerpt(value: Any, *, limit: int = 180) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _add_taxonomy_example(bucket: dict[str, Any], slug: str, excerpt: Any) -> None:
    text = _clean_excerpt(excerpt)
    if not text:
        return
    examples = bucket["examples"]
    if len(examples) >= 10:
        return
    candidate = {"slug": slug, "excerpt": text}
    if candidate not in examples:
        examples.append(candidate)


def _taxonomy_from_issues(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    affected: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        slug = str(row.get("slug") or "")
        issues = (((row.get("metrics") or {}).get("artifact_quality") or {}).get("issues") or [])
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            issue_type = str(issue.get("type") or "unknown")
            bucket = buckets.setdefault(
                issue_type,
                {"affected_books": 0, "occurrences": 0, "examples": []},
            )
            bucket["occurrences"] += 1
            affected[issue_type].add(slug)
            _add_taxonomy_example(bucket, slug, issue.get("excerpt") or issue.get("detail"))
    for issue_type, slugs in affected.items():
        buckets[issue_type]["affected_books"] = len(slugs)
    return dict(sorted(buckets.items()))


def _taxonomy_from_skipped(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    affected: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        slug = str(row.get("slug") or "")
        skipped = (row.get("metrics") or {}).get("skipped_non_prose_sections") or []
        for item in skipped:
            if isinstance(item, dict):
                reason = str(item.get("reason") or "unknown")
                excerpt = item.get("excerpt") or item.get("title") or item.get("heading")
            else:
                reason = str(item or "unknown")
                excerpt = item
            bucket = buckets.setdefault(
                reason,
                {"affected_books": 0, "occurrences": 0, "examples": []},
            )
            bucket["occurrences"] += 1
            affected[reason].add(slug)
            _add_taxonomy_example(bucket, slug, excerpt)
    for reason, slugs in affected.items():
        buckets[reason]["affected_books"] = len(slugs)
    return dict(sorted(buckets.items()))


def _taxonomy_from_errors(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    affected: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        metrics = row.get("metrics") or {}
        if metrics.get("success") is not False or not metrics.get("error"):
            continue
        slug = str(row.get("slug") or "")
        kind = _error_type(metrics.get("error"))
        bucket = buckets.setdefault(kind, {"affected_books": 0, "occurrences": 0, "examples": []})
        bucket["occurrences"] += 1
        affected[kind].add(slug)
        _add_taxonomy_example(bucket, slug, metrics.get("error"))
    for kind, slugs in affected.items():
        buckets[kind]["affected_books"] = len(slugs)
    return dict(sorted(buckets.items()))


def _stage_shares(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    wall = sum(
        float((row.get("metrics") or {}).get("wall_time_seconds") or 0.0)
        for row in rows
    )
    shares: dict[str, float | None] = {}
    for output_key, metric_key in (
        ("parse", "parse_seconds"),
        ("clean", "clean_seconds_total"),
        ("tts", "tts_seconds_total"),
        ("package", "package_seconds_total"),
    ):
        total = sum(float((row.get("metrics") or {}).get(metric_key) or 0.0) for row in rows)
        shares[output_key] = round((total / wall) * 100.0, 4) if wall > 0 else None
    return shares


def _quality_passed(metrics: dict[str, Any]) -> bool:
    return metrics.get("success") is True and (metrics.get("artifact_quality") or {}).get("success") is True


def build_report(*, ledger_path: Path, run_name: str, output_dir: Path) -> dict[str, Any]:
    rows = _load_ledger_rows(ledger_path)
    wall_times = [
        float((row.get("metrics") or {}).get("wall_time_seconds"))
        for row in rows
        if isinstance((row.get("metrics") or {}).get("wall_time_seconds"), int | float)
    ]
    per_audio_minute: list[float] = []
    slowest: list[dict[str, Any]] = []
    for row in rows:
        metrics = row.get("metrics") or {}
        wall = metrics.get("wall_time_seconds")
        duration_ms = metrics.get("total_duration_ms")
        if isinstance(wall, int | float) and isinstance(duration_ms, int | float) and duration_ms > 0:
            value = float(wall) / (float(duration_ms) / 60000.0)
            rounded = round(value, 4)
            per_audio_minute.append(rounded)
            slowest.append({"slug": row.get("slug"), "wall_seconds_per_audio_minute": rounded})
    hard_error_histogram = Counter(
        _error_type((row.get("metrics") or {}).get("error"))
        for row in rows
        if (row.get("metrics") or {}).get("success") is not True
        and (row.get("metrics") or {}).get("error")
    )
    rule_only = sum(int((row.get("metrics") or {}).get("cleaner_gate_rule_only_chunks") or 0) for row in rows)
    llm_needed = sum(int((row.get("metrics") or {}).get("cleaner_gate_llm_needed_chunks") or 0) for row in rows)
    reason_counts: Counter[str] = Counter()
    per_book_gate: list[dict[str, Any]] = []
    for row in rows:
        metrics = row.get("metrics") or {}
        book_rule = int(metrics.get("cleaner_gate_rule_only_chunks") or 0)
        book_llm = int(metrics.get("cleaner_gate_llm_needed_chunks") or 0)
        if book_rule or book_llm:
            per_book_gate.append(
                {
                    "slug": row.get("slug"),
                    "rule_only_chunks": book_rule,
                    "llm_needed_chunks": book_llm,
                }
            )
        reason_counts.update(metrics.get("cleaner_gate_llm_reason_counts") or {})
    report = {
        "schema_version": 1,
        "run_name": run_name,
        "created_at": datetime.now(UTC).isoformat(),
        "ledger_path": str(ledger_path),
        "aggregate": {
            "books_attempted": len(rows),
            "books_succeeded": sum(1 for row in rows if (row.get("metrics") or {}).get("success") is True),
            "quality_passed": sum(1 for row in rows if _quality_passed(row.get("metrics") or {})),
            "hard_error_type_histogram": dict(sorted(hard_error_histogram.items())),
        },
        "performance": {
            "wall_time_seconds": _stats(wall_times),
            "wall_seconds_per_audio_minute": _stats(per_audio_minute),
            "stage_share_percent_of_wall": _stage_shares(rows),
            "slowest_5_by_wall_seconds_per_audio_minute": sorted(
                slowest,
                key=lambda item: item["wall_seconds_per_audio_minute"],
                reverse=True,
            )[:5],
        },
        "quality_taxonomy": {
            "artifact_quality": _taxonomy_from_issues(rows),
            "skipped_non_prose_sections": _taxonomy_from_skipped(rows),
            "hard_errors": _taxonomy_from_errors(rows),
        },
        "llm_gating": {
            "per_book": per_book_gate,
            "corpus": {
                "rule_only_chunks": rule_only,
                "llm_needed_chunks": llm_needed,
                "llm_routed_percent": round((llm_needed / (rule_only + llm_needed)) * 100.0, 4)
                if (rule_only + llm_needed)
                else None,
            },
            "reason_counts": dict(sorted(reason_counts.items())),
        },
        "books": [_book_appendix_row(row) for row in rows],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(render_markdown_report(report), encoding="utf-8")
    return report


def _book_appendix_row(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") or {}
    return {
        "slug": row.get("slug"),
        "mode": row.get("mode"),
        "success": metrics.get("success"),
        "quality_success": (metrics.get("artifact_quality") or {}).get("success"),
        "wall_time_seconds": metrics.get("wall_time_seconds"),
        "chunks": metrics.get("chunks"),
        "chapters": metrics.get("chapters"),
        "error": metrics.get("error"),
    }


def _taxonomy_table_lines(title: str, taxonomy: dict[str, dict[str, Any]]) -> list[str]:
    lines = [f"## {title}", "", "| Type | Books | Occurrences | Top excerpts |", "| --- | ---: | ---: | --- |"]
    if not taxonomy:
        lines.append("| none | 0 | 0 | |")
        return lines
    for issue_type, data in taxonomy.items():
        examples = "; ".join(
            f"{example['slug']}: {_clean_excerpt(example['excerpt'], limit=80)}"
            for example in data.get("examples", [])[:3]
        )
        lines.append(
            f"| {issue_type} | {data.get('affected_books', 0)} | {data.get('occurrences', 0)} | {examples} |"
        )
    return lines


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [f"# Stress Corpus Report: {report['run_name']}", ""]
    lines.extend(_taxonomy_table_lines("Artifact Quality Taxonomy", report["quality_taxonomy"]["artifact_quality"]))
    lines.extend([""])
    lines.extend(_taxonomy_table_lines("Skipped Non-Prose Sections", report["quality_taxonomy"]["skipped_non_prose_sections"]))
    lines.extend([""])
    lines.extend(_taxonomy_table_lines("Hard Errors", report["quality_taxonomy"]["hard_errors"]))
    perf = report["performance"]
    lines.extend(
        [
            "",
            "## Performance",
            "",
            f"- Wall seconds: {perf['wall_time_seconds']}",
            f"- Wall seconds per audio minute: {perf['wall_seconds_per_audio_minute']}",
            f"- Stage share percent of wall: {perf['stage_share_percent_of_wall']}",
            "",
            "## LLM Gating",
            "",
            f"- Corpus: {report['llm_gating']['corpus']}",
            f"- Reasons: {report['llm_gating']['reason_counts']}",
            "",
            "## Per-Book Appendix",
            "",
            "| Slug | Mode | Success | Quality | Wall seconds | Chunks | Chapters | Error |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for book in report["books"]:
        lines.append(
            "| {slug} | {mode} | {success} | {quality_success} | {wall} | {chunks} | {chapters} | {error} |".format(
                slug=book.get("slug"),
                mode=book.get("mode"),
                success=book.get("success"),
                quality_success=book.get("quality_success"),
                wall=book.get("wall_time_seconds"),
                chunks=book.get("chunks"),
                chapters=book.get("chapters"),
                error=_clean_excerpt(book.get("error"), limit=100),
            )
        )
    return "\n".join(lines) + "\n"


def _load_benchmark_runner() -> Any:
    with contextlib.redirect_stdout(io.StringIO()):
        from scripts import benchmark_nagel_pipeline

    return benchmark_nagel_pipeline


def _mode_flags(mode: str) -> dict[str, bool]:
    if mode == "parse":
        return {"parse_only": True, "cleaner_gate": False}
    if mode == "gate":
        return {"parse_only": False, "cleaner_gate": True}
    if mode == "full":
        return {"parse_only": False, "cleaner_gate": False}
    raise ValueError(f"unsupported mode: {mode}")


def _prune_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key not in BULKY_METRIC_KEYS}


def _append_ledger_row(ledger_path: Path, row: dict[str, Any]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()


def _quality_label(metrics: dict[str, Any]) -> str:
    quality = metrics.get("artifact_quality") or {}
    if quality.get("success") is True:
        return "pass"
    if quality.get("success") is False:
        return "fail"
    return "n/a"


def run_sweep(args: argparse.Namespace) -> dict[str, Any]:
    run_root = REPO_ROOT / ".artifacts" / "stress" / args.run_name
    ledger_path = run_root / "ledger.jsonl"
    if args.report_only:
        return build_report(ledger_path=ledger_path, run_name=args.run_name, output_dir=run_root)
    if args.books_dir is None:
        raise SystemExit("--books-dir is required unless --report-only is used")
    books = discover_books(
        args.books_dir,
        include_patterns=args.include,
        exclude_patterns=args.exclude,
    )
    if args.max_books is not None:
        books = books[: args.max_books]
    books = apply_resume_filter(books, ledger_path=ledger_path, resume=args.resume)
    runner = _load_benchmark_runner()
    failures = 0
    flags = _mode_flags(args.mode)
    chapter_limit = args.chapter_limit
    if chapter_limit is None and args.mode == "full":
        chapter_limit = 4
    total = len(books)
    for index, book in enumerate(books, start=1):
        started_perf = time.perf_counter()
        started_at = datetime.now(UTC).isoformat()
        output_dir = run_root / "books" / book.slug / "output"
        temp_dir = run_root / "books" / book.slug / "temp"
        metrics: dict[str, Any]
        try:
            metrics = runner.run_benchmark(
                source_path=book.path,
                output_dir=output_dir,
                temp_dir=temp_dir,
                parse_only=flags["parse_only"],
                cleaner_gate=flags["cleaner_gate"],
                cleaner_backend=args.cleaner_backend,
                cleaner_cuda_device=args.cleaner_cuda_device,
                cleaner_cuda_quantization=args.cleaner_cuda_quantization,
                chapter_limit=chapter_limit,
                enable_chapter_aac_packaging=args.mode == "full",
                enable_background_chapter_aac_encoding=True,
            )
        except Exception as exc:  # noqa: BLE001
            metrics = {
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "errors": [traceback.format_exc()],
                "wall_time_seconds": round(time.perf_counter() - started_perf, 4),
                "artifact_quality": {"success": False, "issues": []},
            }
        elapsed = round(time.perf_counter() - started_perf, 4)
        row = {
            "slug": book.slug,
            "source_file": str(book.path),
            "file_size_bytes": book.size_bytes,
            "mode": args.mode,
            "started_at": started_at,
            "elapsed_seconds": elapsed,
            "metrics": _prune_metrics(metrics),
        }
        _append_ledger_row(ledger_path, row)
        if metrics.get("success") is True:
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            failures += 1
        progress = (
            f"[{index}/{total}] {book.slug}: "
            f"{'ok' if metrics.get('success') is True else 'fail'} "
            f"wall={float(metrics.get('wall_time_seconds') or elapsed):.1f}s "
            f"quality={_quality_label(metrics)}"
        )
        print(_ascii_safe(progress), file=sys.stderr)
        if args.stop_after_failures is not None and failures >= args.stop_after_failures:
            break
    return build_report(ledger_path=ledger_path, run_name=args.run_name, output_dir=run_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Orpheus corpus stress harness.")
    parser.add_argument("--books-dir", type=Path)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--mode", choices=("parse", "gate", "full"), default="gate")
    parser.add_argument("--chapter-limit", type=int, default=None)
    parser.add_argument("--max-books", type=int, default=None)
    parser.add_argument("--include", action="append", default=None)
    parser.add_argument("--exclude", action="append", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after-failures", type=int, default=None)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--cleaner-backend", default="cuda")
    parser.add_argument("--cleaner-cuda-device", default="cuda:0")
    parser.add_argument("--cleaner-cuda-quantization", default="4bit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_sweep(args)
    print(json.dumps(report["aggregate"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
