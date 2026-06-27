"""Synthetic cleaning-policy benchmark fixtures and expectations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

POLICY_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "policy"
MANIFEST_FILENAME = "manifest.json"


def load_policy_manifest(fixtures_dir: Path | None = None) -> dict[str, Any]:
    root = fixtures_dir or POLICY_FIXTURES_DIR
    manifest_path = root / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"policy manifest must be a JSON object: {manifest_path}")
    return payload


def policy_fixture_paths(
    fixtures_dir: Path | None = None,
    *,
    categories: set[str] | None = None,
) -> list[Path]:
    manifest = load_policy_manifest(fixtures_dir)
    root = fixtures_dir or POLICY_FIXTURES_DIR
    paths: list[Path] = []
    for case in manifest.get("cases", []):
        if not isinstance(case, dict):
            continue
        category = str(case.get("category", ""))
        if categories is not None and category not in categories:
            continue
        source = case.get("source")
        if not isinstance(source, str) or not source.strip():
            continue
        path = root / source
        if not path.exists():
            raise FileNotFoundError(f"policy fixture source missing: {path}")
        paths.append(path)
    return paths


def policy_bench_fixture_paths(fixtures_dir: Path | None = None) -> list[Path]:
    """Return policy fixtures that retain narratable prose for full benchmark runs."""
    manifest = load_policy_manifest(fixtures_dir)
    root = fixtures_dir or POLICY_FIXTURES_DIR
    paths: list[Path] = []
    for case in manifest.get("cases", []):
        if not isinstance(case, dict):
            continue
        if not (case.get("must_not_skip") or case.get("expected_skip_reasons")):
            continue
        source = case.get("source")
        if not isinstance(source, str):
            continue
        path = root / source
        if not path.exists():
            raise FileNotFoundError(f"policy fixture source missing: {path}")
        paths.append(path)
    return paths


def validate_policy_parse_metrics(case: dict[str, Any], metrics: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    case_id = str(case.get("id", case.get("source", "unknown")))

    if case.get("must_not_skip") is True:
        skipped = metrics.get("skipped_non_prose_sections") or []
        if skipped:
            errors.append(f"{case_id}: expected prose to be retained but skipped {skipped!r}")
        if int(metrics.get("chapters") or 0) <= 0:
            errors.append(f"{case_id}: expected at least one retained chapter")
        return errors

    if case.get("expected_skip") is True:
        skipped = metrics.get("skipped_non_prose_sections") or []
        expected_reason = case.get("expected_reason")
        if not skipped:
            errors.append(f"{case_id}: expected a skipped non-prose section")
        elif expected_reason is not None:
            reasons = {str(entry.get("reason")) for entry in skipped if isinstance(entry, dict)}
            if str(expected_reason) not in reasons:
                errors.append(
                    f"{case_id}: expected skip reason {expected_reason!r}, got {sorted(reasons)!r}"
                )
        expected_chapters = case.get("expected_chapters")
        if expected_chapters is not None and int(metrics.get("chapters") or 0) != int(expected_chapters):
            errors.append(
                f"{case_id}: expected {expected_chapters} retained chapters, got {metrics.get('chapters')}"
            )
        return errors

    expected_reasons = case.get("expected_skip_reasons")
    if expected_reasons is not None:
        skipped = metrics.get("skipped_non_prose_sections") or []
        reasons = {str(entry.get("reason")) for entry in skipped if isinstance(entry, dict)}
        for expected_reason in expected_reasons:
            if str(expected_reason) not in reasons:
                errors.append(
                    f"{case_id}: expected skip reason {expected_reason!r}, got {sorted(reasons)!r}"
                )
        expected_chapters = case.get("expected_chapters")
        if expected_chapters is not None and int(metrics.get("chapters") or 0) != int(expected_chapters):
            errors.append(
                f"{case_id}: expected {expected_chapters} retained chapters, got {metrics.get('chapters')}"
            )
    return errors
