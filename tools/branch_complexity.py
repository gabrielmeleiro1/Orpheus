#!/usr/bin/env python3
"""Branch-count complexity benchmark for the whole repo.

Counts *branch points* per working-tree source file — the decision points that make code
hard to hold in your head. The goal is a stable number that simplification passes can
drive down without being gameable by formatting, renaming, or comment stripping.

What counts as a branch:
  Python (ast):      if/elif, ternary, while, for, except handler, each match case,
                     each comprehension clause and its ifs, each extra and/or operand
  TS/TSX/JS (tsc):   if, ternary, case clause, catch, while/do/for loops,
                     &&/||/??/&&=/||=/??=
  Rust (lexer):      if (incl. if let / else if), while, for..in, match arms (=>),
                     &&/||  — comments and strings are stripped first; `=>` inside
                     macro_rules! and the `?` operator are known approximations

Commands:
  python tools/branch_complexity.py report              # totals per area
  python tools/branch_complexity.py report --by-file    # worst files first
  python tools/branch_complexity.py snapshot            # write committed baseline
  python tools/branch_complexity.py check               # diff current tree vs baseline
  python tools/branch_complexity.py check --fail-on-increase   # for hooks/CI
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "benchmarks" / "complexity" / "branch_baseline.json"
TS_HELPER = Path(__file__).resolve().parent / "branch_complexity_ts.mjs"
METRIC_VERSION = 1

PYTHON_SUFFIXES = {".py"}
TS_SUFFIXES = {".ts", ".tsx", ".mjs", ".js"}
RUST_SUFFIXES = {".rs"}


# ---------------------------------------------------------------------------
# Python


class _PythonBranchCounter(ast.NodeVisitor):
    def __init__(self) -> None:
        self.branches = 0

    def _count(self, node: ast.AST, amount: int = 1) -> None:
        self.branches += amount
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._count(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._count(node)

    def visit_While(self, node: ast.While) -> None:
        self._count(node)

    def visit_For(self, node: ast.For) -> None:
        self._count(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._count(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._count(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self._count(node, len(node.values) - 1)

    def visit_Match(self, node: ast.Match) -> None:
        self._count(node, len(node.cases))

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self._count(node, 1 + len(node.ifs))


def count_python_source(source: str, filename: str = "<string>") -> int:
    counter = _PythonBranchCounter()
    counter.visit(ast.parse(source, filename=filename))
    return counter.branches


# ---------------------------------------------------------------------------
# Rust

_RUST_BRANCH_PATTERNS = (
    re.compile(r"\bif\b"),
    re.compile(r"\bwhile\b"),
    re.compile(r"\bfor\s+[^{;]*?\bin\b"),  # loop only; skips `impl Trait for Type`
    re.compile(r"=>"),
    re.compile(r"&&"),
    re.compile(r"\|\|"),
)


def _strip_rust_comments_and_strings(source: str) -> str:
    """Blank out comments, string/char literals so keyword counts see only code."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                i += 1
        elif ch == "/" and nxt == "*":
            depth = 1
            i += 2
            while i < n and depth:
                if source[i] == "/" and i + 1 < n and source[i + 1] == "*":
                    depth += 1
                    i += 2
                elif source[i] == "*" and i + 1 < n and source[i + 1] == "/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif ch == "r" and re.match(r'r#*"', source[i:]):
            hashes = re.match(r"r(#*)", source[i:]).group(1)
            close = '"' + hashes
            end = source.find(close, i + len(hashes) + 2)
            i = n if end == -1 else end + len(close)
        elif ch == '"':
            i += 1
            while i < n and source[i] != '"':
                i += 2 if source[i] == "\\" else 1
            i += 1
        elif ch == "'":
            # Char literal ('x', '\n') vs lifetime ('a): a closing quote nearby
            # means char literal; otherwise skip just the quote.
            m = re.match(r"'(\\.|[^\\'])'", source[i:])
            i += m.end() if m else 1
        else:
            out.append(ch)
            i += 1
            continue
        out.append(" ")
    return "".join(out)


def count_rust_source(source: str) -> int:
    stripped = _strip_rust_comments_and_strings(source)
    return sum(len(pattern.findall(stripped)) for pattern in _RUST_BRANCH_PATTERNS)


# ---------------------------------------------------------------------------
# TypeScript / JavaScript (delegated to the TS compiler via node)


def count_ts_files(paths: list[str]) -> dict[str, int]:
    if not paths:
        return {}
    if shutil.which("node") is None:
        raise SystemExit("node is required to count TS/JS branches but was not found on PATH")
    proc = subprocess.run(
        ["node", str(TS_HELPER)],
        input="\n".join(paths),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        if "Cannot find module 'typescript'" in proc.stderr:
            raise SystemExit(
                "the TS branch counter needs the TypeScript compiler from the "
                "desktop app; run `npm ci` in desktop/ first"
            )
        raise SystemExit(f"TS branch counter failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Scanning


def source_files() -> list[str]:
    patterns = sorted("*" + s for s in PYTHON_SUFFIXES | TS_SUFFIXES | RUST_SUFFIXES)
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", *patterns],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout
    return sorted({p for p in out.split("\0") if p and (REPO_ROOT / p).is_file()})


def bucket_for(path: str) -> str:
    if path.startswith("desktop/src-tauri/"):
        return "desktop/src-tauri"
    if path.startswith("desktop/"):
        return "desktop/src"
    return path.split("/", 1)[0] if "/" in path else "(root)"


def scan() -> dict[str, dict[str, int]]:
    """Include new source files and omit unstaged deletions and ignored artifacts."""
    files: dict[str, dict[str, int]] = {}
    ts_paths: list[str] = []
    for rel in source_files():
        suffix = Path(rel).suffix
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        lines = len(text.splitlines())
        if suffix in PYTHON_SUFFIXES:
            branches = count_python_source(text, filename=rel)
        elif suffix in RUST_SUFFIXES:
            branches = count_rust_source(text)
        else:
            ts_paths.append(rel)
            branches = -1  # filled in below
        files[rel] = {"branches": branches, "lines": lines}
    for rel, branches in count_ts_files(ts_paths).items():
        files[rel]["branches"] = branches
    return files


def summarize(files: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    buckets: dict[str, dict[str, int]] = {}
    for rel, stats in files.items():
        entry = buckets.setdefault(bucket_for(rel), {"files": 0, "lines": 0, "branches": 0})
        entry["files"] += 1
        entry["lines"] += stats["lines"]
        entry["branches"] += stats["branches"]
    return buckets


def totals(files: dict[str, dict[str, int]]) -> dict[str, int]:
    return {
        "files": len(files),
        "lines": sum(s["lines"] for s in files.values()),
        "branches": sum(s["branches"] for s in files.values()),
    }


# ---------------------------------------------------------------------------
# Commands


def _print_bucket_table(buckets: dict[str, dict[str, int]], total: dict[str, int]) -> None:
    width = max(len(name) for name in [*buckets, "TOTAL"]) + 2
    print(f"{'area':<{width}}{'files':>7}{'lines':>9}{'branches':>10}")
    for name, entry in sorted(buckets.items(), key=lambda kv: -kv[1]["branches"]):
        print(f"{name:<{width}}{entry['files']:>7}{entry['lines']:>9}{entry['branches']:>10}")
    print(f"{'TOTAL':<{width}}{total['files']:>7}{total['lines']:>9}{total['branches']:>10}")


def cmd_report(args: argparse.Namespace) -> int:
    files = scan()
    if args.by_file:
        ranked = sorted(files.items(), key=lambda kv: -kv[1]["branches"])[: args.top]
        width = max(len(rel) for rel, _ in ranked) + 2
        print(f"{'file':<{width}}{'lines':>9}{'branches':>10}")
        for rel, stats in ranked:
            print(f"{rel:<{width}}{stats['lines']:>9}{stats['branches']:>10}")
    else:
        _print_bucket_table(summarize(files), totals(files))
    return 0


def cmd_snapshot(_args: argparse.Namespace) -> int:
    files = scan()
    baseline = {
        "metric_version": METRIC_VERSION,
        "total": totals(files),
        "buckets": dict(sorted(summarize(files).items())),
        "files": dict(sorted(files.items())),
    }
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {BASELINE_PATH.relative_to(REPO_ROOT)}")
    _print_bucket_table(baseline["buckets"], baseline["total"])
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    if not BASELINE_PATH.exists():
        raise SystemExit("no baseline found — run `snapshot` first")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if baseline.get("metric_version") != METRIC_VERSION:
        raise SystemExit("baseline was written with a different metric version — re-run `snapshot`")
    files = scan()
    current_total = totals(files)
    delta = current_total["branches"] - baseline["total"]["branches"]

    changed = []
    for rel in sorted(set(files) | set(baseline["files"])):
        before = baseline["files"].get(rel, {}).get("branches", 0)
        after = files.get(rel, {}).get("branches", 0)
        if before != after:
            changed.append((rel, before, after))
    if changed:
        width = max(len(rel) for rel, _, _ in changed) + 2
        for rel, before, after in sorted(changed, key=lambda c: c[1] - c[2]):
            print(f"{rel:<{width}}{before:>6} -> {after:<6} ({after - before:+d})")
    else:
        print("no per-file changes vs baseline")

    sign = "+" if delta > 0 else ""
    print(
        f"\nbranches: {baseline['total']['branches']} (baseline) -> "
        f"{current_total['branches']} (current), {sign}{delta}"
    )
    if delta > 0 and args.fail_on_increase:
        print("FAIL: branch count increased vs baseline")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="print branch totals")
    report.add_argument("--by-file", action="store_true", help="rank individual files")
    report.add_argument("--top", type=int, default=30, help="rows to show with --by-file")
    report.set_defaults(func=cmd_report)

    snapshot = sub.add_parser("snapshot", help="write the committed baseline")
    snapshot.set_defaults(func=cmd_snapshot)

    check = sub.add_parser("check", help="compare working tree against the baseline")
    check.add_argument("--fail-on-increase", action="store_true")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
