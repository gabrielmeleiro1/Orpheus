# Orpheus

Orpheus is a local-first desktop app that converts PDF, EPUB, Markdown, and text sources into audiobook outputs. The primary UI is the Tauri + React desktop shell, backed by a Python/FastAPI conversion engine and a local SQLite library.

## Start Here

1. Read [docs/README.md](docs/README.md).
2. Use [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) for system prerequisites and dependency manifests.
3. Use [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for runtime boundaries, API contracts, and [where to change X](docs/ARCHITECTURE.md#where-to-change-x).
4. Use [docs/RUN_APPLICATION.md](docs/RUN_APPLICATION.md) to run, package, or install the app.
5. Use [docs/TESTING.md](docs/TESTING.md) for the local verification matrix.

## Repository Layout

- `desktop/`: Tauri, React, Vite, and TypeScript desktop app.
- `apps/launchers/`: desktop backend launcher.
- `modules/app/`: controller, app events, and app-level services.
- `modules/web/`: FastAPI server, API routes, desktop auth, upload/source handling, and SSE plumbing.
- `modules/pipeline/`: conversion orchestration and stage helpers.
- `modules/tts/`, `modules/audio/`, `modules/ingestion/`: domain processing code.
- `modules/storage/`: persistence models, migrations, repository implementation.
- `tests/`: Python/core regression tests plus optional extended suites.
- `docs/`: maintained project documentation.
- `data/`: local runtime state. See `data/README.md` if present in your checkout.

## Quick Start

```bash
python3.12 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python scripts/setup_runtime.py --profile base
python -m pip install -r requirements-dev.txt
python run_tests.py --contract
cd desktop
npm ci
npm run desktop:dev
```

This quick start selects the `base` profile: document conversion, playback, and
cloud cleaning work, while local TTS and local model cleaning remain disabled.
Choose an optional local runtime before downloading models or building its
sidecar:

```bash
# macOS 14+ on arm64 Apple Silicon; no PyTorch
python scripts/setup_runtime.py --profile mlx \
  --group tts-mlx --group cleaner-mlx

# Linux/Windows with a supported NVIDIA GPU and driver
python scripts/setup_runtime.py --profile cuda --accept-pytorch-risk \
  --group tts-cpu-cuda --group cleaner-cuda
```

CUDA is opt-in because its pinned PyTorch release has unresolved upstream
advisories; the setup command refuses CUDA unless `--accept-pytorch-risk` is
provided. Use trusted model artifacts only. Run either command with `--dry-run`
first to inspect its fixed install and download commands.

`requirements.txt` installs the base runtime. The profile manifests live under
`requirements/`, and `requirements-dev.txt` adds test, lint, and desktop
sidecar packaging tooling. Frontend
dependencies are declared in `desktop/package.json` and locked by
`desktop/package-lock.json`; Rust dependencies are declared in
`desktop/src-tauri/Cargo.toml` and locked by `desktop/src-tauri/Cargo.lock`.
System prerequisites are listed in [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md).

`python run_tests.py --contract` is the Python/core gate. It does not cover
desktop build, packaged sidecar, release install, or optional property,
benchmark, slow, and UI suites; use [docs/TESTING.md](docs/TESTING.md) for the
desktop and release verification matrix.

To produce and replace the installed macOS app:

```bash
cd desktop
npm run desktop:release
```

## Common Tasks

| Task | Command | Notes |
| --- | --- | --- |
| Run desktop app (dev) | `cd desktop && npm run desktop:dev` | Tauri launches Vite, Rust shell, and Python backend on an ephemeral port. |
| Run API only | `python -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0` | Loopback FastAPI server; stdout emits one `{"event":"ready",...}` JSON line. |
| Run benchmark | `python -m orpheus bench parse path/to/book.pdf` | See [docs/TESTING.md](docs/TESTING.md#empirical-benchmark-suite) for `clean`, `full`, and `suite` modes. |
| Build base sidecar | `python scripts/build_desktop_backend.py --runtime-profile base` | Bundles no local ML runtime. Use `mlx` or `cuda` only after installing that profile; required before `desktop:build` or `desktop:release`. |
| Focused tests | `python -m pytest --no-cov <paths>` | Parser, cleaner, TTS, API, storage, and desktop matrices in [docs/TESTING.md](docs/TESTING.md#focused-checks). |
| Python/core gate | `python run_tests.py --contract` | Default merge gate; excludes property, benchmark, slow, and UI suites. |

For packaging, install, and release verification, use [docs/RUN_APPLICATION.md](docs/RUN_APPLICATION.md).
For where to change parsers, cleaners, TTS, the conversion form, API payloads, or storage schema,
see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#where-to-change-x).

## GitHub Repository Notes

Commit source, docs, tests, `requirements*.txt`, `desktop/package-lock.json`,
and `desktop/src-tauri/Cargo.lock`. Do not commit generated runtime/build
outputs such as `venv/`, `.env.local`, `data/*.db`, `output/`, `temp/`,
`desktop/node_modules/`, `desktop/dist/`, `desktop/src-tauri/target/`, or
`desktop/src-tauri/binaries/`; these are ignored in `.gitignore`.
