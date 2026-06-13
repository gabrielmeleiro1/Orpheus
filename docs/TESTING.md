# Testing Guide

Architecture reference: `docs/ARCHITECTURE.md`

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
- `--cov-fail-under=69`

Coverage output is generated under `.artifacts/coverage/` so contract runs do
not leave `htmlcov/`, `coverage.xml`, or `.coverage` at the repository root.

This is not a full desktop, sidecar, release, property, benchmark, slow, or UI
verification pass. This checkout does not contain `.github` workflows, so do not
treat the command as evidence that CI is configured or green.

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

Static cleanup checks:

```bash
venv/bin/python -m ruff check .
venv/bin/python -m vulture apps modules scripts tools tests vulture_whitelist.py --min-confidence 80 --sort-by-size
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

```bash
cd desktop
npm run typecheck
npm run build
cd src-tauri
cargo test
cargo check
```

What this exercises:
- TypeScript contracts and React production build
- Tauri/Rust shell lifecycle and path handling tests
- frontend assets as bundled for the desktop app

## Desktop Release Readiness Verification

Use this full matrix before desktop release candidates. The Python/core contract
is only the first command in the release evidence set:

```bash
python run_tests.py --contract
python -m pytest tests/test_web_api.py tests/test_web_events.py tests/test_web_desktop_auth.py tests/test_desktop_backend_launcher.py tests/test_desktop_runtime_contract.py -q -o addopts=
cd desktop
npm run typecheck
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
- `cd desktop && npm run typecheck && npm run build`
- `cd desktop/src-tauri && cargo test && cargo check`
- `python scripts/build_desktop_backend.py && cd desktop && npm run desktop:build`
- installed-app launch/quit smoke, plus one real short conversion/playback before
  considering a release candidate reliable

Coverage note:
- The Python/core contract does not exercise the desktop TypeScript build,
  Tauri/Rust shell, packaged sidecar, release installer, or installed app.
- The additional commands above cover frontend build/type contracts, Rust shell
  behavior, backend launcher/runtime checks, sidecar packaging, and app install.
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
