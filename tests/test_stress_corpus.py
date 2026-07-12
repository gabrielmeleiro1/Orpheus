from __future__ import annotations

import json
from pathlib import Path

from scripts import stress_corpus


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_manifest_parsing_reads_seed_rows() -> None:
    rows = stress_corpus.load_manifest(
        Path("benchmarks/stress_corpus/manifest.tsv")
    )

    assert len(rows) == 32
    assert rows[0].gutenberg_id == "2701"
    assert rows[0].slug == "moby-dick"
    assert rows[0].stress_dimension == "long digressive novel"
    assert rows[-1].slug == "shakespeare-complete"


def test_resume_done_slugs_are_skipped(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(
        ledger_path,
        [
            {"slug": "done", "metrics": {"success": True}},
            {"slug": "failed", "metrics": {"success": False}},
        ],
    )
    books = [
        stress_corpus.BookSource(slug="done", path=tmp_path / "done.md", size_bytes=12),
        stress_corpus.BookSource(slug="new", path=tmp_path / "new.md", size_bytes=12),
    ]

    selected = stress_corpus.apply_resume_filter(
        books,
        ledger_path=ledger_path,
        resume=True,
    )

    assert [book.slug for book in selected] == ["new"]


def test_resume_keys_books_by_source_filename(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(
        ledger_path,
        [{"source_key": "alice-in-wonderland.epub", "slug": "alice-in-wonderland"}],
    )
    books = [
        stress_corpus.BookSource(
            slug="alice-in-wonderland",
            source_key="alice-in-wonderland.epub",
            output_slug="alice-in-wonderland.epub",
            path=tmp_path / "alice-in-wonderland.epub",
            size_bytes=12,
        ),
        stress_corpus.BookSource(
            slug="alice-in-wonderland",
            source_key="alice-in-wonderland.txt",
            output_slug="alice-in-wonderland.txt",
            path=tmp_path / "alice-in-wonderland.txt",
            size_bytes=12,
        ),
    ]

    selected = stress_corpus.apply_resume_filter(
        books,
        ledger_path=ledger_path,
        resume=True,
    )

    assert [book.source_key for book in selected] == ["alice-in-wonderland.txt"]


def test_include_exclude_filters_are_deterministic(tmp_path: Path) -> None:
    for name in ("moby-dick.epub", "dracula.epub", "notes.txt", "ignored.jpg"):
        (tmp_path / name).write_text("content", encoding="utf-8")

    books = stress_corpus.discover_books(
        tmp_path,
        include_patterns=["*.epub"],
        exclude_patterns=["drac*"],
    )

    assert [book.slug for book in books] == ["moby-dick"]


def test_discover_books_keeps_suffix_distinct_for_outputs(tmp_path: Path) -> None:
    for name in ("alice-in-wonderland.epub", "alice-in-wonderland.txt"):
        (tmp_path / name).write_text("content", encoding="utf-8")

    books = stress_corpus.discover_books(tmp_path)

    assert [book.slug for book in books] == [
        "alice-in-wonderland",
        "alice-in-wonderland",
    ]
    assert [book.source_key for book in books] == [
        "alice-in-wonderland.epub",
        "alice-in-wonderland.txt",
    ]
    assert [book.output_slug for book in books] == [
        "alice-in-wonderland.epub",
        "alice-in-wonderland.txt",
    ]


def test_report_aggregation_groups_taxonomy_and_percentiles(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(
        ledger_path,
        [
            {
                "slug": "alpha",
                "mode": "gate",
                "metrics": {
                    "success": True,
                    "wall_time_seconds": 10.0,
                    "parse_seconds": 2.0,
                    "clean_seconds_total": 3.0,
                    "tts_seconds_total": 0.0,
                    "package_seconds_total": 1.0,
                    "total_duration_ms": 60000,
                    "cleaner_gate_rule_only_chunks": 8,
                    "cleaner_gate_llm_needed_chunks": 2,
                    "cleaner_gate_llm_reason_counts": {"possible PDF page or footnote residue": 2},
                    "artifact_quality": {
                        "success": False,
                        "issues": [
                            {
                                "type": "pdf_page_footnote_leakage",
                                "excerpt": "Page 12",
                                "path": "chunk1",
                                "detail": "residue",
                            }
                        ],
                    },
                    "skipped_non_prose_sections": [
                        {"reason": "table_of_contents", "excerpt": "Contents"}
                    ],
                },
            },
            {
                "slug": "beta",
                "mode": "gate",
                "metrics": {
                    "success": False,
                    "error": "ValueError: bad parse",
                    "wall_time_seconds": 20.0,
                    "parse_seconds": 4.0,
                    "clean_seconds_total": 1.0,
                    "cleaner_gate_rule_only_chunks": 1,
                    "cleaner_gate_llm_needed_chunks": 1,
                    "cleaner_gate_llm_reason_counts": {"default conservative cleanup pass": 1},
                    "artifact_quality": {
                        "success": False,
                        "issues": [
                            {
                                "type": "pdf_page_footnote_leakage",
                                "excerpt": "Page 13",
                            },
                            {
                                "type": "markdown_heading_marker",
                                "excerpt": "# A",
                            },
                        ],
                    },
                },
            },
            {
                "slug": "gamma",
                "mode": "full",
                "metrics": {
                    "success": True,
                    "wall_time_seconds": 40.0,
                    "total_duration_ms": 120000,
                    "artifact_quality": {"success": True, "issues": []},
                },
            },
        ],
    )

    report = stress_corpus.build_report(
        ledger_path=ledger_path,
        run_name="unit",
        output_dir=tmp_path,
    )

    assert report["aggregate"]["books_attempted"] == 3
    assert report["aggregate"]["books_succeeded"] == 2
    assert report["aggregate"]["quality_passed"] == 1
    assert report["aggregate"]["hard_error_type_histogram"] == {"ValueError": 1}
    assert report["performance"]["wall_time_seconds"]["median"] == 20.0
    assert report["performance"]["wall_time_seconds"]["p90"] == 36.0
    assert report["performance"]["wall_seconds_per_audio_minute"]["max"] == 20.0
    assert report["quality_taxonomy"]["artifact_quality"]["pdf_page_footnote_leakage"][
        "affected_books"
    ] == 2
    assert report["quality_taxonomy"]["artifact_quality"]["pdf_page_footnote_leakage"][
        "occurrences"
    ] == 2
    assert report["quality_taxonomy"]["skipped_non_prose_sections"]["table_of_contents"][
        "occurrences"
    ] == 1
    assert report["llm_gating"]["corpus"]["llm_routed_percent"] == 25.0
    assert report["llm_gating"]["reason_counts"]["possible PDF page or footnote residue"] == 2


def test_download_corpus_does_not_persist_error_pages(tmp_path: Path, monkeypatch) -> None:
    import io
    import urllib.request

    from scripts import download_stress_corpus

    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "gutenberg_id\tslug\tstress_dimension\n123\thtml-error\ttest\n",
        encoding="utf-8",
    )

    class FakeResponse(io.BytesIO):
        status = 200

        def __init__(self) -> None:
            super().__init__(b"<html>rate limited</html>")
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, timeout=None: FakeResponse()
    )
    monkeypatch.setattr(download_stress_corpus, "MIN_EXISTING_BYTES", 0)
    monkeypatch.setattr(
        download_stress_corpus.time, "sleep", lambda seconds: None
    )

    books_dir = tmp_path / "books"
    summary = download_stress_corpus.download_corpus(
        books_dir=books_dir,
        manifest_path=manifest,
        retries=2,
    )

    assert summary == {"downloaded": 0, "skipped": 0, "failed": 1}
    assert not (books_dir / "html-error.epub").exists()
