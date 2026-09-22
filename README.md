# Orpheus

Orpheus is a local-first desktop audiobook converter for macOS and Windows. It turns PDF, EPUB, Markdown, and text sources into spoken audio using a Tauri + React interface, a Python/FastAPI conversion engine, and a local SQLite library.

New conversions announce each chapter or section number and title, pause for
three seconds, then read the body. A three-second closing pause marks the end of
each chapter. These cues are embedded in the exported audio; existing audio must
be converted again to include them.

## Platform Support

| Platform | Support |
| --- | --- |
| macOS 14+ on Apple Silicon | Primary release platform. Unsigned DMG builds use the local MLX runtime. |
| Windows 10/11 | Source development is supported and checked in CI, with base and NVIDIA CUDA runtime options. |
| Linux | Desktop builds are source-only. Core development and NVIDIA CUDA runtimes are supported; no packaged desktop release is published. |

### Windows 11: Smart App Control

> [!WARNING]
> Orpheus is unsigned, so Windows may block it with **Smart App Control**.
> If blocked, open **Windows Security → App & browser control → Smart App
> Control settings → Off**, then launch Orpheus again.
>
> This turns off Smart App Control **for all apps**. Only do this for a build
> you trust, and read Windows' warning before confirming.
> [Microsoft's guidance](https://support.microsoft.com/en-us/windows/security/threat-malware-protection/smart-app-control-frequently-asked-questions).

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

| Area | Paths | Purpose |
| --- | --- | --- |
| Desktop app | `desktop/` | Tauri shell, React UI, TypeScript, Rust, and desktop packaging. |
| Backend entry points | `apps/`, `orpheus/` | Desktop backend launcher and command-line tools. |
| Application and domain code | `modules/` | API, orchestration, ingestion, TTS, audio, storage, and shared contracts. |
| Tests and benchmarks | `tests/`, `benchmarks/` | Regression, integration, performance, and stress coverage. |
| Developer tooling | `scripts/`, `tools/` | Setup, builds, release qualification, diagnostics, and maintenance utilities. |
| Configuration | `config/`, `requirements/`, `security/` | Release settings, runtime profiles, and reviewed audit exceptions. |
| Documentation | `docs/` | Architecture, setup, testing, runtime, and release guides. |
| Local runtime data | `data/`, `output/`, `temp/` | Local database, generated audio, and temporary work; generated contents are ignored. |

## Quick Start

Install [system prerequisites](docs/DEPENDENCIES.md#system-prerequisites) first,
including Git, Python 3.12, Node 20+, Rust, and FFmpeg. Clone the repository and
run these commands from its root. macOS and Linux use a POSIX shell:

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

Windows PowerShell (including [C++ Build Tools and WebView2](docs/DEPENDENCIES.md#windows)):

```powershell
winget install --exact --id Git.Git
winget install --exact --id Gyan.FFmpeg
# Open a new terminal after installing prerequisites.
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python scripts\setup_runtime.py --profile base
python -m pip install -r requirements-dev.txt
cd desktop
npm ci
npm run desktop:dev
```

For local audio on Windows, run this from the repository root with the venv
active, or select CUDA in Setup:

```powershell
python scripts\setup_runtime.py --profile cuda --group tts-cpu-cuda --group cleaner-cuda
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

The Setup screen can download missing managed model assets in packaged builds.
Installing Python runtime dependencies from the app is available only in a
debug desktop build from this source checkout, using the checkout's fixed
`.venv` interpreter and `scripts/setup_runtime.py`. Browser sessions and
packaged releases cannot run that dependency installer. Release artifacts must
include their intended `base`, `mlx`, or `cuda` runtime in the sidecar. A packaged
base build supports source inspection, library management, and playback of
existing audio; conversion requires a build with TTS.

### First launch

Setup checks the runtime, lets you choose the required Kokoro voice model and
an optional cleaner, downloads those models, then verifies that audio creation
is ready. CUDA hardware is checked before installation. Library-only mode skips
model downloads. Later, use **Settings → Manage models** to install, repair, or
delete models; **Settings → Storage** shows and opens the actual data folders.
See [data locations](docs/RUN_APPLICATION.md#data-locations) for each OS.

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
bundles `ORPHEUS_RUNTIME_PROFILE` when set, otherwise retaining the previous
sidecar's profile. A first build uses the target default (`mlx` on Apple
Silicon and `base` elsewhere), so install that profile first.
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
