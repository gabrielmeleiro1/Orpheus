# Orpheus

Orpheus is a local-first desktop app that converts PDF, EPUB, Markdown, and text sources into audiobook outputs. The primary UI is the Tauri + React desktop shell, backed by a Python/FastAPI conversion engine and a local SQLite library.

## Start Here

1. Read [docs/README.md](docs/README.md).
2. Use [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) for system prerequisites and dependency manifests.
3. Use [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for runtime boundaries and API contracts.
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
python -m pip install -r requirements-dev.txt
python run_tests.py --contract
cd desktop
npm ci
npm run desktop:dev
```

`requirements.txt` contains runtime dependencies. `requirements-dev.txt` adds
test, lint, and desktop sidecar packaging tooling. Frontend
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

## GitHub Repository Notes

Commit source, docs, tests, `requirements*.txt`, `desktop/package-lock.json`,
and `desktop/src-tauri/Cargo.lock`. Do not commit generated runtime/build
outputs such as `venv/`, `.env.local`, `data/*.db`, `output/`, `temp/`,
`desktop/node_modules/`, `desktop/dist/`, `desktop/src-tauri/target/`, or
`desktop/src-tauri/binaries/`; these are ignored in `.gitignore`.
