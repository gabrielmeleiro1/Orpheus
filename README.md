# Orpheus

Orpheus is a local-first desktop app that converts PDF, EPUB, Markdown, and text sources into audiobook outputs. The primary UI is the Tauri + React desktop shell, backed by a Python/FastAPI conversion engine and a local SQLite library.

## Start Here

1. Read [docs/README.md](docs/README.md).
2. Use [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) for system prerequisites and dependency manifests.
3. Use [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for runtime boundaries, API contracts, and [where to change X](docs/ARCHITECTURE.md#where-to-change-x).
4. Use [docs/RUN_APPLICATION.md](docs/RUN_APPLICATION.md) to run, package, or install the app.
5. Use [docs/TESTING.md](docs/TESTING.md) for the local verification matrix.
6. For an unsigned Apple Silicon GitHub Release, use
   [docs/RELEASING_MACOS.md](docs/RELEASING_MACOS.md) (maintainers) and
   [docs/UNSIGNED_MACOS_INSTALL.md](docs/UNSIGNED_MACOS_INSTALL.md) (install).
   Source CI (`.github/workflows/source-validation.yml`) is not the full local
   release gate; `scripts/release_qualification.py` is.

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

The commands below are for macOS. Windows contributors should use the
[PowerShell source setup](docs/DEPENDENCIES.md#windows), including the Tauri
C++ and WebView2 prerequisites.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python scripts/setup_runtime.py --profile base
python -m pip install -r requirements-dev.txt
python run_tests.py --contract
cd desktop
npm ci
npm run desktop:dev
```

This quick start selects the `base` profile for library management, existing-audio
playback, and source inspection. Both Document and Audiobook conversion produce
audio and require a local TTS runtime. Cloud cleaning does not provide TTS.
From the repository root, install the appropriate local runtime below before
converting or building its sidecar:

```bash
# macOS 14+ on arm64 Apple Silicon; no PyTorch
python scripts/setup_runtime.py --profile mlx \
  --group tts-mlx --group cleaner-mlx

# Linux with a supported NVIDIA GPU and driver; see the Windows PowerShell setup above
python scripts/setup_runtime.py --profile cuda \
  --group tts-cpu-cuda --group cleaner-cuda
```

CUDA is opt-in and requires a supported NVIDIA GPU and driver. Use trusted
model artifacts only. Run either command with `--dry-run` first to inspect its
fixed install and download commands.

The in-app downloader is intentionally available only while running a debug
desktop build from this source checkout. It uses the checkout's fixed
`.venv` interpreter and `scripts/setup_runtime.py`; browser sessions and
packaged releases cannot run a dynamic installer. Release artifacts must be
built with the intended `base`, `mlx`, or `cuda` profile already baked into
their sidecar. A packaged base build supports source inspection, library
management, and playback of existing audio; conversion requires a build with TTS.

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

Release candidates also require isolated installs of every shipped runtime:

```bash
python run_tests.py --verify-profile base --verify-profile mlx
```

Run the CUDA equivalent on a Linux/Windows NVIDIA host. Requested but
unavailable hardware profiles exit 2 as `skipped`, not passed. Every profile
requires a clean audit unless a finding has an exact, time-bounded reviewed
exception; accepted risk is never labeled passed. See
[docs/DEPENDENCIES.md](docs/DEPENDENCIES.md#clean-environment-release-gate).

To produce and replace the installed macOS app:

```bash
cd desktop
npm run desktop:release
```

`desktop:release` builds a fresh sidecar and frontend before packaging. It
bundles `ORPHEUS_RUNTIME_PROFILE` when set, otherwise the target default
(`mlx` on Apple Silicon and `base` elsewhere), so install that profile first.
To package a `base`-only app instead:

```bash
cd desktop
ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build
npm run desktop:install
```

PowerShell uses
`$env:ORPHEUS_RUNTIME_PROFILE = "base"; npm run desktop:build`.
See the platform examples in
[docs/RUN_APPLICATION.md](docs/RUN_APPLICATION.md#build-desktop-sidecar-for-production-packaging).

## Common Tasks

| Task | Command | Notes |
| --- | --- | --- |
| Run desktop app (dev) | `cd desktop && npm run desktop:dev` | Tauri launches Vite, Rust shell, and Python backend on an ephemeral port. |
| Run API only | `python -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0` | Loopback FastAPI server; stdout emits one `{"event":"ready",...}` JSON line. |
| Run benchmark | `python -m orpheus bench parse path/to/book.pdf` | See [docs/TESTING.md](docs/TESTING.md#empirical-benchmark-suite) for `clean`, `full`, and `suite` modes. |
| Build base desktop bundle | `cd desktop && ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build` | Tauri freshly builds the base sidecar and frontend before packaging. Use `mlx` or `cuda` only after installing that profile. |
| Focused tests | `python -m pytest --no-cov <paths>` | Parser, cleaner, TTS, API, storage, and desktop matrices in [docs/TESTING.md](docs/TESTING.md#focused-checks). |
| Python/core gate | `python run_tests.py --contract` | Default merge gate; excludes property, benchmark, slow, and UI suites. |
| Clean profile gate | `python run_tests.py --verify-profile base --verify-profile mlx` | Fresh install, import, dependency, prohibited-package, and exact-environment audit checks. Run CUDA on NVIDIA hardware. |

For packaging, install, and release verification, use [docs/RUN_APPLICATION.md](docs/RUN_APPLICATION.md).
For unsigned macOS DMG qualification and manual GitHub publishing, use
[docs/RELEASING_MACOS.md](docs/RELEASING_MACOS.md).
For where to change parsers, cleaners, TTS, the conversion form, API payloads, or storage schema,
see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#where-to-change-x).

## GitHub Repository Notes

Commit source, docs, tests, `requirements*.txt`, `desktop/package-lock.json`,
and `desktop/src-tauri/Cargo.lock`. Do not commit generated runtime/build
outputs such as `.venv/`, `.env.local`, `data/*.db`, `output/`, `temp/`,
`desktop/node_modules/`, `desktop/dist/`, `desktop/src-tauri/target/`, or
`desktop/src-tauri/binaries/`; these are ignored in `.gitignore`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, verification gates, and PR
conventions, and [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## License

The source code is licensed under the [MIT License](LICENSE). Packaged
application builds bundle third-party components under other licenses,
including AGPL-3.0 (PyMuPDF, EbookLib) and GPL-3.0 (espeak-ng) — see
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) before redistributing
binaries.
