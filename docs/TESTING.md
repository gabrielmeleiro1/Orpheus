# Testing Guide

Architecture reference: [docs/ARCHITECTURE.md](ARCHITECTURE.md) (including
[where to change X](ARCHITECTURE.md#where-to-change-x) and the
[architecture inventory](architecture/python-boundaries.md)).

## Test Tiers

Orpheus uses layered validation so fast checks stay fast and slow or
environment-dependent work stays opt-in.

| Tier | Command | What it proves | What it does not prove |
| --- | --- | --- | --- |
| Contract | `python run_tests.py --contract` | Python/core pytest gate with coverage floor (`--cov-fail-under=85`), strict markers, and the default marker filter (`not property`, `not benchmark`, `not slow`, `not ui`) | Desktop build, Tauri/Rust shell, packaged sidecar, release installer, real GPU hardware, property fuzzing, long benchmarks |
| Smoke | `python run_tests.py` | Curated script/pytest modules in `SMOKE_TEST_MODULES` for quick local iteration | Full contract coverage, desktop release matrix, optional suites |
| Focused pytest | `python -m pytest --no-cov <paths>` | Targeted regression for one subsystem without global coverage enforcement | Cross-surface release readiness |
| Property | `pytest -m property` | Invariant/fuzz coverage for parsers and cleaners | Runtime performance or UI behavior |
| Benchmark | `pytest -m benchmark` or `python -m orpheus bench ...` | Performance regressions and pipeline timing evidence | Functional correctness by itself |
| Slow | `pytest -m slow` | Long-running integration paths that are too heavy for contract | Fast pre-commit feedback |
| UI | `pytest -m ui` | UI-adjacent Python checks when present | Desktop Vitest/Tauri UI |
| GPU | `pytest -m gpu` | Real CUDA/ANE hardware paths when explicitly run | Default laptop/CI environments without hardware |

Tracked parser fixtures live under `tests/fixtures/` and `tests/samples/`.
Tests must not rewrite those paths; generated samples belong in temp dirs or
`.artifacts/`.

## Python/Core Regression Gate

Install development dependencies before running the test and static-analysis
commands:

```bash
python -m pip install -r requirements-dev.txt
```

Run this as the Python/core gate before committing. Pair it with the desktop and
release checks below before publishing changes that affect those surfaces:

```bash
python run_tests.py --contract
```

This command runs the repo's Python/core pytest contract:

```bash
python -m pytest tests -m "not property and not benchmark and not slow and not ui"
```

The contract keeps `pytest.ini` defaults intact, including:
- `--strict-markers`
- coverage reporting
- `--cov-fail-under=85`

Coverage output is generated under `.artifacts/coverage/` so contract runs do
not leave `htmlcov/`, `coverage.xml`, or `.coverage` at the repository root.

This is not a full desktop, sidecar, release, property, benchmark, slow, or UI
verification pass. The `Source validation` GitHub Actions workflow runs this
gate plus Ruff, frontend type/tests, and Rust test/check on macOS and Windows.
It validates source checkouts only: it does not package or sign applications,
build installers, publish releases, or upload binaries. A local contract pass
does not mean the remote GitHub Actions jobs are green; check the commit's CI
status after pushing.

## Local Smoke Suite

For a fast local sanity check, run:

```bash
python run_tests.py
```

This is a curated smoke suite defined by `SMOKE_TEST_MODULES` in `run_tests.py`.
It intentionally mixes:
- `script` mode: `python <test_file>.py`
- `pytest` mode: `python -m pytest <test_file> -q -o addopts=`

A green smoke run is useful for iteration, but it does **not** prove that the
Python/core pytest contract is green.

## Focused Checks

Use these when you change one subsystem and want signal without the full
contract or desktop release matrix.

Parser or cleaner changes:

```bash
python -m pytest --no-cov tests/test_pdf_parser.py tests/test_epub_parser.py tests/test_markdown_parser.py tests/test_text_parser.py tests/test_ingestion.py
python -m pytest --no-cov tests/test_cleaner.py tests/test_cleaner_golden_set.py tests/test_cleaner_heuristic.py tests/test_cleaner_text_preservation.py
```

TTS or runtime changes:

```bash
python -m pytest --no-cov tests/test_tts.py tests/test_tts_engine_modularization.py tests/test_tts_factory_contract.py tests/test_tts_resilience.py tests/test_tts_runtime_lock.py tests/test_runtime_recovery.py tests/test_parallel_fallback.py
```

API or frontend payload changes:

```bash
python -m pytest --no-cov tests/test_web_api.py tests/test_web_events.py tests/test_web_desktop_auth.py tests/test_conversion_service.py
cd desktop && npm run typecheck && npm test -- --run
```

Storage or path changes:

```bash
python -m pytest --no-cov tests/test_storage_migrations.py tests/test_storage_repository.py tests/test_storage_maintenance.py tests/test_library_service.py
```

Desktop sidecar changes:

```bash
python -m pytest --no-cov tests/test_desktop_backend_launcher.py tests/test_desktop_runtime_contract.py
python scripts/build_desktop_backend.py
cd desktop/src-tauri && cargo test && cargo check
```

Static cleanup checks:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m vulture apps modules scripts tools tests vulture_whitelist.py --min-confidence 80 --sort-by-size
cd desktop
npm run typecheck
npm exec --yes knip -- --include dependencies,devDependencies,files,exports,types
```

Notes:
- Vulture is useful for candidates, not automatic deletion. Include `tests` for the safer pass so test-referenced Python public surface is not misclassified.
- The desktop TypeScript config already enables `noUnusedLocals` and `noUnusedParameters`.
- Knip reports unused exported frontend surface; prefer making internal types non-exported before deleting runtime code.

Focused web/API checks:

```bash
python -m pytest --no-cov tests/test_web_api.py tests/test_web_events.py
```

Focused runner checks:

```bash
python -m pytest --no-cov tests/test_run_tests.py
```

## Desktop Contained Verification

The desktop UI is verified through the contained Tauri path, not a separate
preview harness:

Desktop Node commands require Node 20 or newer. The repository root
`.node-version` and `desktop/package.json` both declare this floor.

```bash
cd desktop
npm run typecheck
npm test -- --run
npm run build
cd src-tauri
cargo test
cargo check
```

What this exercises:
- TypeScript contracts and React production build
- Vitest desktop unit coverage, including the UI workflow behavioral journey
  (`desktop/src/app/uiWorkflowJourney.test.ts`) that composes production
  reducers/presentation helpers (primary actions, terminal progress semantics,
  playback labels/duration, tab keyboard mapping, log filtering) without
  Playwright/WebDriver. App shell auto-routing to Progress/Library is not
  asserted there; confirm that in native smoke.
- Tauri/Rust shell lifecycle and path handling tests
- frontend assets as bundled for the desktop app

### Desktop runtime smoke (Python sidecar/API)

Fast default smoke (no model download, no GPU conversion):

```bash
python scripts/desktop_runtime_smoke.py
python -m pytest --no-cov tests/test_desktop_runtime_smoke.py -q -o addopts=
```

What the default smoke proves:
- Backend launch on an ephemeral loopback port with bearer token handshake
- Health, authenticated library, text/source upload, invalid conversion reject,
  active-job empty state, optional SSE ping
- Always tears down the backend and temporary runtime directories

Opt-in production-contract conversion journey (capability-aware; skips the
model-backed portion unless an **explicit** runtime-ready signal is present —
`runtime_probe.tts.available === true` and/or `mlx_runtime_available === true`.
Factory catalog fields like `available_tts_engines` / `default_tts_engine` are
**not** sufficient and must not trigger downloads). When the probe is still
`pending` / `refresh_in_progress`, the journey polls `/api/system/capabilities`
up to `--probe-wait-seconds` (default 25) before skipping with a distinct
timeout reason. An explicit finished-unavailable probe skips immediately.

The journey launches the child backend with Hugging Face / Transformers offline
flags (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`) so model resolution cannot
surprise-download weights. Cached local weights are required for a successful
model-backed conversion; missing weights fail (or skip only when the probe
already reports unavailable) rather than reaching the network. The journey also
tracks the started job id and resulting `book_id` / title
(`Conversion Journey Smoke`) instead of assuming library item zero:

```bash
python scripts/desktop_runtime_smoke.py --conversion-journey --conversion-timeout-seconds 180 --probe-wait-seconds 25
```

When a positive runtime probe is present and conversion runs, this also verifies
the matching library item (by terminal `book_id` or unique expected title) and a
non-empty authenticated audio response. Otherwise the upload/preflight contract
still runs and the conversion portion prints an explicit skip reason.

The Vitest UI workflow journey (`desktop/src/app/uiWorkflowJourney.test.ts`)
composes production reducers/presentation helpers. It does **not** claim to
prove App shell tab routing; Progress/Library auto-navigation remains a native
manual smoke.

### What still requires native manual release smoke

- Packaged installer launch/quit
- File picker / drag-drop UX inside Tauri
- Real GPU/ANE conversion quality and listen-through
- Visual confirmation of the workflow-clarity UI fixes after a real conversion

## Desktop Release Readiness Verification

Use this full matrix before desktop release candidates. The Python/core contract
is only the first command in the release evidence set:

```bash
python run_tests.py --contract
python -m pytest tests/test_web_api.py tests/test_web_events.py tests/test_web_desktop_auth.py tests/test_desktop_backend_launcher.py tests/test_desktop_runtime_contract.py -q -o addopts=
cd desktop
npm run typecheck
npm test -- --run
npm run build
cd src-tauri
cargo test
cargo check
cd ../..
python scripts/build_desktop_backend.py
cd desktop
npm run desktop:build
npm run desktop:install
```

Release-candidate evidence should include:
- `python run_tests.py --contract`
- focused backend/web command above
- `cd desktop && npm run typecheck && npm test -- --run && npm run build`
- `cd desktop/src-tauri && cargo test && cargo check`
- `python scripts/build_desktop_backend.py && cd desktop && npm run desktop:build`
- installed-app launch/quit smoke, plus one real short conversion/playback before
  considering a release candidate reliable

Coverage note:
- The Python/core contract does not exercise the desktop TypeScript build,
  Vitest unit coverage, Tauri/Rust shell, packaged sidecar, release installer,
  or installed app.
- The additional commands above cover frontend build/type/test contracts, Rust
  shell behavior, backend launcher/runtime checks, sidecar packaging, and app
  install.
- Packaged-user-flow checks (file picker through completed conversion/playback)
  still require installed-app smoke validation.

## Optional Extended Suites

These suites stay outside the default contract because they are slow, noisy, or
environment-dependent:
- `property` tests
- `benchmark` tests
- `slow` tests
- `ui` tests

Run them explicitly as needed:

```bash
pytest -m property
pytest -m benchmark
pytest -m slow
pytest -m ui
```

Examples:
- `tests/test_cleaner_api_trace_multichapter_real.py` requires real API credentials/network.
- `tests/test_perf_smoke.py` and `tests/test_performance_benchmark.py` are performance-only checks.

## Empirical Benchmark Suite

Use the unified benchmark CLI when you need comparable local evidence for
parsing, cleaning, TTS, or full-pipeline changes:

```bash
python -m orpheus bench parse path/to/book.pdf
python -m orpheus bench clean path/to/book.pdf
python -m orpheus bench full path/to/book.pdf --chapter-limit 1 --profile-tts-chunks
python -m orpheus bench suite path/to/small.md path/to/medium.pdf --modes parse,clean
```

By default the suite writes summaries and temp files under
`.artifacts/benchmarks/`, which is ignored local artifact state. Each command
writes a top-level `bench_summary.json` plus the existing per-run
`benchmark_summary.json` files from `scripts/benchmark_nagel_pipeline.py`.

Recommended workflow:
- Use small or medium project-authored fixtures for quick regression checks.
- Use local real books only for local baseline evidence; do not commit them.
- Run `parse,clean` first for fast signal, then run `full` only on bounded
  chapter limits unless you intentionally want a long TTS benchmark.
- Compare `wall_time_seconds`, `parse_seconds`, cleaner gate counts,
  `tts_seconds_total`, `artifact_quality`, chapter counts, chunk counts, and
  output validity across branches.

### Presets and regression reports

Deterministic suite presets live under `tests/fixtures/benchmark_suite/`:

```bash
python -m orpheus bench quick
python -m orpheus bench policy
python -m orpheus bench compare BASELINE/current/repeated_benchmark_summary.json CURRENT/repeated_benchmark_summary.json --output-dir .artifacts/benchmarks/report
python -m orpheus bench report path/to/repeated_benchmark_summary.json --output-dir .artifacts/benchmarks/report
```

`quick` runs parse and clean against short synthetic markdown fixtures with
`chapter_limit=1`. `policy` runs parse and clean against heading-structure and
cleaner-noise fixtures with bounded chapter limits.

Repeated-run summaries from `scripts/benchmark_nagel_repeated.py` can be compared
branch-to-branch with `bench compare` or inspected alone with `bench report`.
Both commands write `report.json` and `report.md` using the shared validity
thresholds in `orpheus/bench/validity.py`.

### Evidence command matrix

| Change area | Fast evidence | Deeper evidence (opt-in) |
| --- | --- | --- |
| Parser / ingestion | `python -m pytest --no-cov tests/test_*parser*.py tests/test_ingestion.py` | `python -m orpheus bench policy` |
| Cleaner / normalization | `python -m pytest --no-cov tests/test_cleaner*.py` | `python -m orpheus bench clean tests/fixtures/benchmark_suite/policy_cleaner_noise.md` |
| TTS / runtime | `python -m pytest --no-cov tests/test_tts*.py tests/test_runtime_recovery.py` | `python -m orpheus bench full path/to/book.md --chapter-limit 1 --profile-tts-chunks` |
| API / web payloads | `python -m pytest --no-cov tests/test_web_api.py tests/test_web_events.py tests/test_conversion_service.py` | `cd desktop && npm run typecheck && npm test -- --run` |
| Storage / library paths | `python -m pytest --no-cov tests/test_storage_*.py tests/test_library_service.py tests/test_book_workspace.py` | — |
| Desktop sidecar / packaging | `python -m pytest --no-cov tests/test_desktop_backend_launcher.py tests/test_desktop_runtime_contract.py` | `python scripts/build_desktop_backend.py && cd desktop/src-tauri && cargo test` |
| Benchmark / perf regressions | `python -m orpheus bench quick` | `scripts/benchmark_nagel_repeated.py …` then `python -m orpheus bench compare BASELINE CURRENT` |
| Full release candidate | `python run_tests.py --contract` | Desktop release matrix in [Desktop Release Readiness Verification](#desktop-release-readiness-verification) |

## GPU Test Markers and Skip Policy

`pytest.ini` defines `@pytest.mark.gpu` for tests that require real GPU hardware
(NVIDIA CUDA and/or Apple Neural Engine, depending on the test).

Policy:
- CUDA cleaner unit and lifecycle tests (for example `tests/test_cleaner_cuda.py`,
  `tests/test_cleaner_cuda_lifecycle.py`) use mocked PyTorch and run in the default
  contract without hardware.
- Tests that load models on real CUDA hardware should carry `@pytest.mark.gpu` and
  skip when the required device is unavailable (for example
  `@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA GPU required")`).
- Run GPU-only suites explicitly when hardware is present:

```bash
pytest -m gpu
```

The default contract does not exclude `gpu` today; keep real-hardware tests behind
explicit skip guards until they are moved behind the marker in a follow-up change.
