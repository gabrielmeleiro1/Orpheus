"""Tests for the branch-count complexity benchmark (tools/branch_complexity.py).

The counters must be exact and stable: simplification passes are measured against
them, so a miscount either hides real complexity or rewards fake reductions.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tools.branch_complexity import (
    REPO_ROOT,
    bucket_for,
    count_python_source,
    count_rust_source,
    count_ts_files,
)

pytestmark = pytest.mark.unit


def _typescript_toolchain_available() -> bool:
    """The TS counter shells out to node and needs desktop/node_modules.

    A source-only checkout (fresh clone, or a CI lane that runs pytest before
    `npm ci`) has neither, which is an environment gap rather than a defect --
    skip instead of failing the suite.
    """
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(
        [
            "node",
            "-e",
            "require('node:module')"
            ".createRequire(process.argv[1])('typescript')",
            str(Path(REPO_ROOT) / "desktop" / "package.json"),
        ],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


requires_typescript = pytest.mark.skipif(
    not _typescript_toolchain_available(),
    reason="node + desktop/node_modules typescript required; run `npm ci` in desktop/",
)


class TestPythonCounter:
    def test_conditionals_loops_and_boolops(self):
        source = textwrap.dedent(
            """
            def f(items, flag):
                if flag and items:
                    total = 0
                    for item in items:
                        total += item if item > 0 else 0
                    return total
                return [x for x in items if x]
            """
        )
        # if=1, and=1, for=1, ternary=1, comprehension for=1 + its if=1
        assert count_python_source(source) == 6

    def test_elif_counts_as_branch(self):
        source = "def f(x):\n    if x:\n        pass\n    elif x > 1:\n        pass\n"
        assert count_python_source(source) == 2

    def test_match_counts_each_case(self):
        source = textwrap.dedent(
            """
            def g(v):
                match v:
                    case 1:
                        return "a"
                    case _:
                        return "b"
            """
        )
        assert count_python_source(source) == 2

    def test_except_handlers_and_while(self):
        source = textwrap.dedent(
            """
            while True:
                break
            try:
                pass
            except ValueError:
                pass
            except Exception:
                pass
            """
        )
        assert count_python_source(source) == 3

    def test_straight_line_code_is_zero(self):
        assert count_python_source("x = 1\ny = x + 2\nprint(y)\n") == 0


class TestRustCounter:
    def test_loops_conditionals_and_match_arms(self):
        source = textwrap.dedent(
            """
            fn demo(xs: &[i32]) -> i32 {
                let mut total = 0;
                for x in xs {
                    if *x > 0 && *x < 10 {
                        total += x;
                    }
                }
                match total {
                    0 => 0,
                    n if n > 5 => n,
                    _ => 1,
                }
            }
            """
        )
        # for=1, if=1, &&=1, three arms=3, guard if=1
        assert count_rust_source(source) == 7

    def test_comments_and_strings_are_ignored(self):
        source = textwrap.dedent(
            """
            // if this && that => whatever
            /* match x { _ => 1 } */
            fn f() -> &'static str {
                "if && => ||"
            }
            """
        )
        assert count_rust_source(source) == 0

    def test_impl_for_is_not_a_loop(self):
        assert count_rust_source("impl Display for Thing {}\n") == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@requires_typescript
class TestTypeScriptCounter:
    def test_conditionals_loops_and_switch(self, tmp_path):
        source = textwrap.dedent(
            """
            function f(xs: number[]): number {
              let total = 0;
              for (const x of xs) {
                if (x > 0 && x < 10) {
                  total += x;
                }
              }
              switch (total) {
                case 0:
                  return 0;
                default:
                  return total > 5 ? 1 : 2;
              }
            }
            """
        )
        path = tmp_path / "sample.ts"
        path.write_text(source, encoding="utf-8")
        # for-of=1, if=1, &&=1, case=1 (default clause excluded), ternary=1
        assert count_ts_files([str(path)]) == {str(path): 5}


class TestBuckets:
    def test_desktop_split_from_tauri(self):
        assert bucket_for("desktop/src/App.tsx") == "desktop/src"
        assert bucket_for("desktop/src-tauri/src/main.rs") == "desktop/src-tauri"
        assert bucket_for("modules/audio/encoder.py") == "modules"
        assert bucket_for("run_tests.py") == "(root)"
