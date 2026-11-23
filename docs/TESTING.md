# Testing

Orpheus uses layered checks: fast gates run on every change, and slow or
hardware-dependent checks are opt-in. Install the development dependencies
first:

```bash
python -m pip install -r requirements-dev.txt
```

Commands below assume an activated virtual environment. On Windows, replace
`python` with `.\.venv\Scripts\python.exe` and `npm` with `npm.cmd`.

## Test Tiers

| Tier | Command | What it covers | What it does not cover |
| --- | --- | --- | --- |
| Contract | `python run_tests.py --contract` | The Python gate: all of `tests/` except `property`, `benchmark`, and `slow`, with strict markers and an 85% coverage floor | Desktop build, Rust shell, packaged sidecar, installers, real GPU hardware |
| Smoke | `python run_tests.py` | A curated list of modules (`SMOKE_TEST_MODULES` in `run_tests.py`) for quick iteration | Full contract coverage |
| Focused | `python -m pytest --no-cov <paths>` | One subsystem, without the global coverage floor | Cross-surface readiness |
| Property | `python -m pytest -m property` | Hypothesis invariants for parsers and the cleaner | Performance or UI behavior |
| Slow | `python -m pytest -m slow` | Long-running integration paths | Fast feedback |
| Benchmark | `python -m pytest -m benchmark` | Performance baselines | Correctness |
| GPU | `python -m pytest --no-cov -m gpu` | Real NVIDIA CUDA paths, with an opt-in variable ([GPU tests](#gpu-tests)) | Machines without that hardware |
| Frontend | `cd desktop && npm run typecheck && npm test -- --run` | TypeScript contracts and Vitest UI tests | Real WebView rendering |
| Rust | `cd desktop/src-tauri && cargo test --locked -- --test-threads=1 && cargo check --locked` | Shell lifecycle, paths, setup supervision, capabilities | The packaged app |
| Browser harness | `npm --prefix tools/frontend_stress test` | Harness unit contracts; the full run drives the real UI and backend | The packaged Tauri app |

A run that selects only `property`, `benchmark`, or `slow` skips the global
coverage floor automatically (see `tests/conftest.py`). Other
marker-only runs, such as `-m gpu`, need `--no-cov`.

Tracked parser fixtures live in `tests/fixtures/`. Tests must not modify them;
write generated samples to a temporary directory or `.artifacts/`.

## Python Contract Gate

```bash
python run_tests.py --contract
```

This runs pytest on `tests/` with the `CORE_MARKER_EXPRESSION` from
`run_tests.py` (`not property and not benchmark and not slow`) and keeps the
`pytest.ini` defaults: `--strict-markers`, coverage reporting, and
`--cov-fail-under=85`. Use the runner rather than raw pytest so the marker
filter stays in sync. Coverage output goes to `.artifacts/coverage/`, so runs
do not leave `htmlcov/`, `coverage.xml`, or `.coverage` at the repository root.

The contract is the Python gate only. It does not cover the desktop build, the
Rust shell, the packaged sidecar, installers, or the optional suites.

## Smoke Suite

```bash
python run_tests.py
```

The smoke suite runs the modules in `SMOKE_TEST_MODULES` in one pytest process
(`-q -o addopts=`) and reports each module separately. A module that fails
there is re-run on its own, and that run decides its result;
`python run_tests.py --isolated` runs every module in its own process from the
start. A listed module that no longer exists fails the run. The suite is useful
while iterating, but a green smoke run does not mean the contract passes.

## Focused Checks

Run these when you change one subsystem.

Parsers and the cleaner:

```bash
python -m pytest --no-cov tests/test_pdf_parser.py tests/test_epub_parser.py tests/test_markdown_parser.py tests/test_text_parser.py tests/test_ingestion.py
python -m pytest --no-cov tests/test_cleaner.py tests/test_cleaner_golden_set.py tests/test_cleaner_heuristic.py tests/test_cleaner_text_preservation.py
```

TTS and runtime:

```bash
python -m pytest --no-cov tests/test_tts.py tests/test_tts_engine_modularization.py tests/test_tts_factory_contract.py tests/test_tts_resilience.py tests/test_tts_runtime_lock.py tests/test_runtime_recovery.py tests/test_parallel_fallback.py
```

API and frontend payloads:

```bash
python -m pytest --no-cov tests/test_web_api.py tests/test_web_events.py tests/test_web_desktop_auth.py tests/test_conversion_service.py
cd desktop && npm run typecheck && npm test -- --run
```

Storage and paths:

```bash
python -m pytest --no-cov tests/test_storage_migrations.py tests/test_storage_repository.py tests/test_storage_maintenance.py tests/test_library_service.py
python -m pytest --no-cov tests/test_book_source_reservations.py tests/test_book_deletion_durability.py tests/test_book_deletion_kill.py
```

Desktop backend and sidecar:

```bash
python -m pytest --no-cov tests/test_desktop_backend_launcher.py tests/test_desktop_runtime_contract.py tests/test_web_desktop_auth.py
cd desktop
ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build
cd src-tauri && cargo test --locked -- --test-threads=1 && cargo check --locked
```

In PowerShell, set the profile with `$env:ORPHEUS_RUNTIME_PROFILE = "base"`
before `npm.cmd run desktop:build`. The build hook always rebuilds the sidecar
before the frontend, so no separate sidecar build is needed.

Browser harness (also run by source CI):

```bash
npm --prefix tools/frontend_stress ci
npm --prefix tools/frontend_stress test
```

The real Chromium journeys need local models; see
[the harness README](../tools/frontend_stress/README.md). Its unit contracts do
not replace a model-backed browser run.

Test runner:

```bash
python -m pytest --no-cov tests/test_run_tests.py
```

## Static Analysis

```bash
python -m ruff check .
python -m vulture apps modules scripts tools tests vulture_whitelist.py --min-confidence 80 --sort-by-size
cd desktop
npm run typecheck
npm exec --yes knip -- --include dependencies,devDependencies,files,exports,types
```

- Vulture lists candidates, not certain dead code. Include `tests` so public
  surface used only by tests is not misreported.
- The desktop TypeScript config enables `noUnusedLocals` and
  `noUnusedParameters`.
- Knip reports unused frontend exports. Prefer making internal types
  non-exported before deleting runtime code.

## Desktop Checks

Desktop commands need Node 22 or newer (`.node-version` and
`desktop/package.json` both declare this floor).

```bash
cd desktop
npm run typecheck
npm test -- --run
npm run build
cd src-tauri
cargo test --locked -- --test-threads=1
cargo check --locked
```

These cover TypeScript contracts, the React production build, the Vitest
suite, and the Rust shell's lifecycle and path handling. The Vitest suite does
not use Playwright or WebDriver, and it does not prove that the app shell
switches between Create and Library on its own; confirm that in a native smoke
test.

To run every source gate in one command with a log per step under
`.artifacts/validation/`:

```bash
python scripts/check_desktop.py
python scripts/check_desktop.py --only python frontend
python scripts/check_desktop.py --dry-run
```

It runs lint, the Python contract with coverage, frontend types, tests, and
build, Rust tests and check, and an isolated backend smoke. Python, Node
dependencies, Rust, and FFmpeg must already be installed. The groups are
`lint`, `python`, `frontend`, `rust`, and `smoke`.

## Desktop Runtime Smoke

The default smoke needs no models and no GPU:

```bash
python scripts/desktop_runtime_smoke.py
python -m pytest --no-cov tests/test_desktop_runtime_smoke.py -q -o addopts=
```

It launches the backend on an ephemeral loopback port with the bearer-token
handshake, then checks health, the authenticated library, text and source
upload, rejection of an invalid conversion, the empty active-job state, and an
optional SSE ping. It always tears down the backend and its temporary
directories.

The opt-in conversion journey also runs a short real conversion when a local
TTS runtime is ready:

```bash
python scripts/desktop_runtime_smoke.py --conversion-journey --conversion-timeout-seconds 180 --probe-wait-seconds 25
```

- It runs the model-backed part only when the runtime probe explicitly reports
  TTS as ready (`runtime_probe.tts.available === true` or
  `mlx_runtime_available === true`). Catalog fields such as
  `available_tts_engines` are not enough and never trigger downloads.
- While the probe is `pending` or `refresh_in_progress`, it polls
  `/api/system/capabilities` for up to `--probe-wait-seconds` (default 25),
  then skips with a distinct timeout reason. A finished, unavailable probe
  skips at once.
- The backend runs with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, so
  weights must already be cached. Missing weights fail the run instead of
  reaching the network.
- It tracks the started job and its `book_id` and title
  (`Conversion Journey Smoke`), then checks the matching library item and a
  non-empty authenticated audio response. When the conversion is skipped, the
  upload and preflight checks still run and the skip reason is printed.

## Continuous Integration

| Workflow | Trigger | What it runs |
| --- | --- | --- |
| `Source validation` (`.github/workflows/source-validation.yml`) | Every push and pull request | On macOS 15 (Apple Silicon) and Windows: Ruff, the base setup dry run, the Python contract, frontend types, tests, and production build, browser harness unit tests, and Rust test and check |
| `Release artifact validation` (`.github/workflows/release-artifact-validation.yml`) | Manual dispatch, `v*` tags, and `release/**` branches | On macOS 15 (Apple Silicon): a clean `base` profile check, the source gates, a `base` desktop build, and verification of the packaged app |

Source validation never builds, signs, or uploads applications. The release
artifact workflow builds with `ORPHEUS_RUNTIME_PROFILE=base` and checks that
the `.app`, the DMG, the embedded sidecar, `ffmpeg`, and `ffprobe` exist, are
executable, and match the runner architecture. It then runs the smoke harness
against that sidecar with a reduced, Finder-like `PATH`, and uploads the
`.app`, the DMG, and the logs as workflow artifacts even when a later step
fails. Neither workflow publishes a release.

A local pass does not mean CI is green; check the commit's status after
pushing.

### Pre-push hook

`bash scripts/install-git-hooks.sh` sets `git config core.hooksPath
scripts/hooks`, so Git runs the tracked hook in every worktree of the clone and
picks up edits to it without reinstalling. Git then ignores `.git/hooks/`, so
any personal hooks you kept there stop running. Clones that installed the
earlier copied hook should re-run the installer once.

Before each push the hook runs `npm ci` in `desktop/`, then
`python scripts/check_desktop.py --only lint python frontend rust`: Ruff,
`run_tests.py --contract`, the frontend type check, Vitest, `npm run build`,
`cargo test --locked -- --test-threads=1`, and `cargo check --locked`. Any
failure blocks the push, and the logs are written to `.artifacts/validation/`.
It needs Python 3.12 (the checkout's `.venv` is preferred) and FFmpeg.

## Release Verification

Run the full matrix before a desktop release candidate. The Python contract is
only the first item.

```bash
python -m ruff check .
python run_tests.py --contract
python run_tests.py --verify-profile base --verify-profile mlx
python -m pytest --no-cov tests/test_web_api.py tests/test_web_events.py tests/test_web_desktop_auth.py tests/test_desktop_backend_launcher.py tests/test_desktop_runtime_contract.py -q -o addopts=
cd desktop
npm ci
npm run typecheck
npm test -- --run
npm run build
cd src-tauri
cargo test --locked -- --test-threads=1
cargo check --locked
cd ..
ORPHEUS_RUNTIME_PROFILE=mlx npm run desktop:build
npm run desktop:install
```

Then launch and quit the installed app, and run one real short conversion with
playback. Manual checks still cover:

- installer launch and quit;
- the Tauri file picker and drag and drop;
- conversion quality on real GPU or Apple hardware, with a listen-through;
- the UI after a real conversion.

On Apple Silicon, `scripts/release_qualification.py` is the full local release
gate: it composes this matrix with
packaging, artifact inspection, lifecycle performance budgets, and a recorded
manual checklist. Its `lifecycle_performance` phase and configured budgets are
required. See [RELEASING_MACOS.md](RELEASING_MACOS.md). Profile verification
details are in
[DEPENDENCIES.md](DEPENDENCIES.md#clean-environment-release-gate).

### Packaged app smoke (macOS)

To check sidecar startup and the packaged `ffmpeg` and `ffprobe` under a
reduced, Finder-like `PATH`, without an ML conversion, point the smoke harness
at the built `.app`:

```bash
python scripts/desktop_runtime_smoke.py \
  --backend sidecar \
  --app-bundle desktop/src-tauri/target/release/bundle/macos/Orpheus.app \
  --reduced-path \
  --check-ffmpeg-runtime
```

The resolver accepts only the embedded sidecar at
`Contents/Resources/binaries/orpheus-desktop-backend/orpheus-desktop-backend`
and never falls back to `desktop/src-tauri/binaries`.

## Optional Suites

```bash
python -m pytest -m property
python -m pytest -m slow
python -m pytest -m benchmark
```

There are no Python UI tests; the Vitest suite is the UI gate.

`tests/test_cleaner_api_trace_multichapter_real.py` needs real API credentials
and network access.

### GPU tests

`pytest.ini` registers `@pytest.mark.gpu` for tests that need real GPU
hardware.

- CUDA cleaner unit and lifecycle tests (for example
  `tests/test_cleaner_cuda.py` and `tests/test_cleaner_cuda_lifecycle.py`) mock
  PyTorch and run in the contract without hardware.
- Real-hardware tests carry both `gpu` and `slow`, so the contract excludes
  them. They run only when an opt-in environment variable is set, and an
  opted-in run fails, rather than skips, when the hardware, dependencies, or
  weights are missing. Do not gate them on `torch.cuda.is_available()`: a
  silent skip would hide a broken GPU path.

The only such test today is `tests/test_cleaner_cuda_real.py`, which runs real
Qwen inference. It needs `ORPHEUS_TEST_CUDA_CLEANER=1` and
`ORPHEUS_TEST_HF_HOME` (see
[ENVIRONMENT.md](ENVIRONMENT.md#test-and-harness-inputs)):

```bash
ORPHEUS_TEST_CUDA_CLEANER=1 ORPHEUS_TEST_HF_HOME=/path/to/hf-home \
  python -m pytest --no-cov -m gpu tests/test_cleaner_cuda_real.py
```

## Evidence by Change Area

| Change area | Focused evidence | Additional verification |
| --- | --- | --- |
| Parser or ingestion | `python -m pytest --no-cov tests/test_*parser*.py tests/test_ingestion.py` | `python -m pytest -m property` |
| Cleaner or normalization | `python -m pytest --no-cov tests/test_cleaner*.py` | `python -m pytest -m property` |
| TTS or runtime | `python -m pytest --no-cov tests/test_tts*.py tests/test_runtime_recovery.py` | [GPU tests](#gpu-tests) and a real short conversion with playback |
| API or web payloads | `python -m pytest --no-cov tests/test_web_api.py tests/test_web_events.py tests/test_conversion_service.py` | `cd desktop && npm run typecheck && npm test -- --run` |
| Storage or library paths | `python -m pytest --no-cov tests/test_storage_*.py tests/test_library_service.py tests/test_book_workspace.py` | [Browser library journeys](../tools/frontend_stress/README.md#scenarios) |
| Frontend rendering or playback performance | `node tools/frontend_stress/performance.mjs --label=candidate --output-dir=.artifacts/local-performance/frontend-candidate` | Compare serial baseline and candidate runs with the [frontend performance harness](../tools/frontend_stress/README.md#portable-frontend-performance-baseline) |
| Desktop sidecar or packaging | `python -m pytest --no-cov tests/test_desktop_backend_launcher.py tests/test_desktop_runtime_contract.py` | `cd desktop && ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build && cd src-tauri && cargo test --locked -- --test-threads=1 && cargo check --locked` |
| Release candidate | `python run_tests.py --contract` | The [release verification](#release-verification) matrix, including lifecycle performance budgets |
