#!/usr/bin/env python3
"""Thin wrapper around the shared benchmark regression report module."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orpheus.bench.report import run_report_cli  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    return run_report_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
