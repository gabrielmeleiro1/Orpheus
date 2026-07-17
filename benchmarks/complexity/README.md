# Branch-count complexity benchmark

`branch_baseline.json` is the committed snapshot of how many *branch points* every
tracked source file contains (Python, TS/TSX/JS, Rust). It is the scoreboard for
simplification passes: behavior-preserving refactors should drive the number down,
and nothing else should move it.

Branch points are decision constructs — `if`/`elif`/`else if`, ternaries, `match`/
`switch` cases, loops, exception handlers, and short-circuit boolean operators. The
counters parse real syntax (Python `ast`, the TypeScript compiler, a comment/string-
stripping Rust lexer), so the metric is immune to the usual reward hacks: renaming,
comment stripping, reformatting, and code-golfing don't change it. The only way to
lower it is a better abstraction. Exact rules live in the docstring of
[`tools/branch_complexity.py`](../../tools/branch_complexity.py).

## Workflow

```bash
python tools/branch_complexity.py report            # totals per area
python tools/branch_complexity.py report --by-file  # worst offenders first
python tools/branch_complexity.py check             # working tree vs baseline
python tools/branch_complexity.py snapshot          # rewrite the baseline
```

A simplification pass looks like:

1. `check` before starting — confirm the tree matches the baseline.
2. Refactor with tests green; use `check` to see the per-file branch delta.
3. When the pass lands, run `snapshot` and commit the new baseline with it, so the
   history of `branch_baseline.json` becomes the record of complexity over time.

`check --fail-on-increase` exits non-zero if the tree has more branches than the
baseline, for use in hooks or CI once we want to hold the line.

Baselines are tied to `metric_version` in the tool; if counting rules ever change,
bump the version and re-snapshot rather than comparing across rule sets.
