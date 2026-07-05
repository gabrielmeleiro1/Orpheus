#!/usr/bin/env python3
"""Download the Project Gutenberg stress corpus from benchmarks/stress_corpus."""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.stress_corpus import DEFAULT_MANIFEST_PATH, load_manifest  # noqa: E402

MIN_EXISTING_BYTES = 10 * 1024


def download_corpus(*, books_dir: Path, manifest_path: Path, retries: int = 3) -> dict[str, int]:
    books_dir.mkdir(parents=True, exist_ok=True)
    rows = load_manifest(manifest_path)
    downloaded = 0
    skipped = 0
    failed = 0
    for row in rows:
        target = books_dir / f"{row.slug}.epub"
        if target.exists() and target.stat().st_size > MIN_EXISTING_BYTES:
            skipped += 1
            print(f"skip {row.slug}: existing", file=sys.stderr)
            continue
        url = f"https://www.gutenberg.org/ebooks/{row.gutenberg_id}.epub3.images"
        for attempt in range(1, retries + 1):
            try:
                print(f"download {row.slug}: attempt {attempt}", file=sys.stderr)
                with urllib.request.urlopen(url, timeout=60) as response:
                    data = response.read()
                target.write_bytes(data)
                downloaded += 1
                break
            except (OSError, urllib.error.URLError) as exc:
                if attempt == retries:
                    failed += 1
                    print(f"error {row.slug}: {exc}", file=sys.stderr)
                else:
                    time.sleep(min(2**attempt, 10))
    return {"downloaded": downloaded, "skipped": skipped, "failed": failed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download the Orpheus stress corpus.")
    parser.add_argument("--books-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--retries", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = download_corpus(
        books_dir=args.books_dir,
        manifest_path=args.manifest,
        retries=args.retries,
    )
    print(summary)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
