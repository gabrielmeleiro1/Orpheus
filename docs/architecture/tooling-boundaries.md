# Developer Tooling Boundaries

Inventory for Workstream 1 of `docs/CODEBASE_CLEANUP_PLAN.md`. Covers
`scripts/`, `tools/`, `benchmarks/`, git hooks, desktop build helpers, and the
`python -m orpheus bench` entry point. No production code was changed.

## Tooling Layer Map

```
                    ┌─────────────────────┐
                    │  orpheus/bench.py   │  ← canonical CLI facade (Workstream 8 target)
                    └──────────┬──────────┘
                               │ imports run_benchmark from
                    ┌──────────▼──────────┐
                    │ scripts/benchmark_  │
                    │   nagel_pipeline.py │  ← primary pipeline benchmark impl
                    └──────────┬──────────┘
           ┌───────────────────┼───────────────────┐
           │                   │                   │
┌──────────▼─────────┐ ┌───────▼────────┐ ┌───────▼──────────────┐
│ benchmarks/kokoro_ │ │ tools/feedback │ │ tools/benchmark_     │
│ single_book/       │ │ _loop_runner   │ │ parallel_workers     │
└────────────────────┘ └────────────────┘ └──────────────────────┘

Desktop build: scripts/build_desktop_backend.py → PyInstaller → src-tauri/binaries/
Validation:    scripts/hooks/pre-push → pytest + desktop + cargo
Smoke:         run_tests.py, scripts/desktop_runtime_smoke.py
```

---

## `orpheus/` package

**Ownership:** Discoverable module entry points for installed/editable use.

| Entry | File | Role |
|-------|------|------|
| `python -m orpheus bench` | `bench.py` | Suite/full/parse/clean commands; stable `bench_summary.json` wrapper |
| `python -m orpheus` | `__main__.py` | Delegates to bench or help |

### Public API (`bench.py`)

- `run_benchmark(**kwargs)` — lazy-imports `scripts.benchmark_nagel_pipeline`
- `cleaner_backend_choices()`, `cleaner_cuda_quantization_choices()` — re-exported from nagel script
- `DEFAULT_OUTPUT_ROOT = Path(".artifacts/benchmarks")`
- `SUMMARY_SCHEMA_VERSION = 1` (CLI wrapper schema; distinct from nagel internal schema v3)

### Cleanup candidates

- Bench CLI schema v1 vs nagel `BENCHMARK_SCHEMA_VERSION = 3` — document mapping or
  unify (Workstream 8).
- `bench.py` dynamically imports `scripts` package — consider moving runnable
  benchmark core into `modules/benchmark/` or `orpheus/bench/` importable module.

---

## `scripts/`

**Ownership:** Repo-root executable scripts for benchmarks, desktop packaging,
model download, CI hooks, and regression reporting.

### Script inventory

| Script | Purpose | Key `modules` imports |
|--------|---------|----------------------|
| `benchmark_nagel_pipeline.py` | **Primary** pipeline benchmark harness (~1534 lines) | `ingestion`, `text`, `pipeline`, `tts` |
| `benchmark_nagel_repeated.py` | Repeated runs + aggregation | nagel script |
| `performance_regression_report.py` | Compare baseline/current JSON summaries | stdlib + fixture paths |
| `build_desktop_backend.py` | PyInstaller sidecar for Tauri | `modules.audio.ffmpeg.resolve_ffmpeg_binary` |
| `desktop_runtime_smoke.py` | Desktop backend smoke test helper | launcher / health |
| `download_models.py` | Model artifact download | `modules.tts.runtime_spec` |
| `install_desktop_app.py` | Desktop app install helper | packaging |
| `install-git-hooks.sh` | Copies hooks into `.git/hooks` | — |

### `scripts/benchmark_nagel_pipeline.py` (central harness)

**Public entry points:**

- `run_benchmark(...)` — invoked by `orpheus bench`, tests, other scripts
- `cleaner_backend_choices()`, `cleaner_cuda_quantization_choices()`
- CLI `main()` with `--full`, `--parse-only`, cleaner/TTS/parallel flags

**Private helpers:** parser dispatch mirroring pipeline, metrics collection,
JSON summary writers, env application (`_apply_cleaner_env`).

**Output conventions:**

- Writes `benchmark_summary.json` under per-run output directory
- `BENCHMARK_SCHEMA_VERSION = 3` with explicit `BENCHMARK_SCHEMA_KEYS` tuple

### `scripts/build_desktop_backend.py`

**Ownership:** Sidecar binary production for Tauri.

- Output: `desktop/src-tauri/binaries/orpheus-desktop-backend` (+ platform triple artifacts)
- Bundles FFmpeg via `resolve_ffmpeg_binary`
- Hardcoded `REQUIRED_MODULES` list (torch, mlx, kokoro, etc.) — must stay aligned
  with actual import graph or sidecar fails at runtime

### Git hooks

| Path | Role |
|------|------|
| `scripts/hooks/pre-push` | Full validation gate (see below) |
| `scripts/install-git-hooks.sh` | Installs `pre-push` into `.git/hooks` |

**`pre-push` steps (in order):**

1. `ruff check .` (120s timeout)
2. `pytest tests` with markers excluding property/benchmark/slow/ui (600s)
3. `cd desktop && npm ci` (180s)
4. `npm run typecheck` (180s)
5. `npm test -- --run` (180s)
6. `cargo test` in `src-tauri` (900s, skips one dev-launch test)
7. `cargo check` (600s)

Requires Python 3.12, `ffmpeg` on PATH.

### Cleanup candidates

| Item | Issue |
|------|-------|
| `benchmark_nagel_pipeline.py` size (~1500+ lines) | Split: CLI / metrics schema / runner |
| Duplicate parser imports vs `pipeline.stages.ingestion` | Two places to update for new formats |
| `_apply_cleaner_env` duplicated in `tools/feedback_loop_runner.py` and `benchmarks/kokoro_single_book/` | Env var names can drift |
| `performance_regression_report.py` vs `bench` compare command (planned) | Overlapping purpose |

---

## `tools/`

**Ownership:** Ad-hoc developer utilities not yet promoted to `orpheus bench` subcommands.

| Script | Purpose | Lines |
|--------|---------|-------|
| `feedback_loop_runner.py` | Chapter-by-chapter conversion with quality stops | ~431 |
| `benchmark_parallel_workers.py` | Parallel worker sweep benchmark | smaller |

### `feedback_loop_runner.py`

**Imports:** `ConversionPipeline`, `PipelineConfig`, `cleaner_config`, `available_cleaner_backends`

**Duplicated assumptions:**

- `SUPPORTED_SOURCE_SUFFIXES = {".pdf", ".epub"}` — stricter than app upload list
- Own `_apply_cleaner_env` setting `CLEANER_BACKEND`, `CLEANER_CUDA_DEVICE`,
  `CLEANER_CUDA_QUANTIZATION`
- `PROJECT_ROOT` path bootstrap (`sys.path.insert`) — copy-pasted pattern across tools

### `benchmark_parallel_workers.py`

**Imports:** `PipelineConfig`, `ConversionPipeline` only — narrower than nagel script.

### Cleanup candidates

- Move shared `PROJECT_ROOT` bootstrap + cleaner env helper to importable module.
- Fold feedback loop into `orpheus bench` preset (`policy` / `real-local` per plan)
  or keep as thin wrapper.
- `tools/` has no `__init__.py` — scripts are copy-paste CLI entry points, not a package.

---

## `benchmarks/`

**Ownership:** Named benchmark scenarios that compose lower-level scripts.

| Path | Role |
|------|------|
| `benchmarks/kokoro_single_book/run_kokoro_single_book.py` | Kokoro matrix (chunk size × overlap × file results) |
| `benchmarks/kokoro_single_book/README.md` | Usage docs |

**Imports:** Reuses `run_benchmark`, `_apply_cleaner_env`, choice helpers from
`scripts.benchmark_nagel_pipeline` — good reuse, but creates `benchmarks` → `scripts`
dependency (wrong direction for a layered tool tree).

### Cleanup candidates

- Invert dependency: nagel core in importable module, both `scripts/` and
  `benchmarks/` call it.
- Kokoro README commands may drift from `orpheus bench` CLI — consolidate docs in
  Workstream 10.

---

## `run_tests.py` (repo root)

**Ownership:** Tiered test entrypoint (smoke, contract, focused).

- `SMOKE_TEST_MODULES` — ordered list of high-value test files
- `CORE_MARKER_EXPRESSION` mirrors pre-push pytest markers
- Not under `scripts/` but is part of developer workflow (`python run_tests.py --contract`)

### Cleanup candidates

- Marker expression duplicated in `pre-push` hook and `run_tests.py` — single constant or doc link.
- Smoke list includes `test_feedback_loop_runner.py` coupling tools to contract tier.

---

## `desktop/` build helpers (cross-cutting)

| Path | Role |
|------|------|
| `scripts/build_desktop_backend.py` | Sidecar binary |
| `desktop/backend-sidecar.spec` | PyInstaller spec |
| `desktop/scripts/test-event-stream-model.mjs` | Node test for SSE parsing |
| `desktop/package.json` scripts | `typecheck`, `test`, vite dev |

Tauri resource dir: `desktop/src-tauri/binaries/` (gitkept `.gitkeep`; binaries generated).

---

## Duplicated CLI Arguments and Conventions

### Cleaner backend selection

| Location | Mechanism |
|----------|-----------|
| `benchmark_nagel_pipeline.py` | `--cleaner-backend`, CUDA flags, env via `_apply_cleaner_env` |
| `feedback_loop_runner.py` | Same env vars, own argparse |
| `orpheus bench` | Delegates choices to nagel `cleaner_backend_choices()` |
| Production pipeline | `PipelineConfig` / `ConversionRuntimeOptions` fields |

**Risk:** CLI uses env mutation; API uses request fields. Worker processes read
serialized config — three configuration paths.

### Output paths

| Tool | Default output root |
|------|---------------------|
| `orpheus/bench.py` | `.artifacts/benchmarks` |
| `benchmark_nagel_pipeline.py` | per-run dirs under user-specified `--output` |
| `feedback_loop_runner.py` | chapter artifacts under book workspace (pipeline-owned) |

No shared path helper; benchmark artifacts must stay out of user library output
(see `storage.maintenance` audit).

### Benchmark JSON schemas

| Artifact | Schema version | File name |
|----------|----------------|-----------|
| `orpheus bench` suite wrapper | `SUMMARY_SCHEMA_VERSION = 1` | `bench_summary.json` |
| Nagel per-run metrics | `BENCHMARK_SCHEMA_VERSION = 3` | `benchmark_summary.json` |
| Repeated benchmark | separate aggregation in `benchmark_nagel_repeated.py` | fixture-driven |
| Performance regression dashboard | JSON fixtures in `tests/fixtures/performance_regression_dashboard/` | manual baseline/current |

**Risk:** comparing across tools requires knowing which schema version each emitted.

### Hardcoded backend / runtime choices

| Location | Hardcoding |
|----------|------------|
| `build_desktop_backend.py` | `DARWIN_REQUIRED_MODULES`, `CUDA_REQUIRED_MODULES` |
| `pre-push` | Python 3.12 only; skips specific flaky tests by name |
| `benchmark_nagel_pipeline.py` | Default voice, chunk sizes, parser dispatch table |
| `download_models.py` | `KOKORO_PACKAGE_DEFAULT_MODEL` from `runtime_spec` (good — uses module constant) |

---

## Recommended Tooling Ownership (for Workstream 8)

| Concern | Canonical owner (proposed) | Wrappers kept for compat |
|---------|---------------------------|--------------------------|
| Pipeline benchmark execution | importable module extracted from nagel script | `scripts/benchmark_nagel_pipeline.py` |
| CLI discovery | `python -m orpheus bench` | — |
| Compare/report | `orpheus bench compare` (planned) | `scripts/performance_regression_report.py` |
| Desktop sidecar build | `scripts/build_desktop_backend.py` | — |
| Pre-push validation | `scripts/hooks/pre-push` | — |
| Chapter feedback loop | `tools/feedback_loop_runner.py` until folded into bench preset | — |

---

## Cleanup Priorities (ordered)

1. Extract `benchmark_nagel_pipeline` core into importable module; stop growing the 1.5k-line script.
2. Unify `_apply_cleaner_env` and `SUPPORTED_SOURCE_SUFFIXES` across tools/scripts.
3. Document schema version matrix (`bench_summary` v1 vs `benchmark_summary` v3).
4. Align pre-push pytest `-k` skips and markers with `run_tests.py` single source.
5. Reduce `benchmarks/` → `scripts/` import direction via shared library.
