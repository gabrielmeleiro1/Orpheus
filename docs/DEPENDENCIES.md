# Dependencies

Every direct dependency is declared in a committed manifest. Install from
these manifests rather than with ad hoc package installs. The JavaScript and
Rust trees are fully locked; the Python manifests pin direct dependencies
(mostly exactly, a few within a major-version range) and leave transitive
packages to the resolver.

| Layer | Declared in | Locked by |
| --- | --- | --- |
| Python base runtime | `requirements.txt` (includes `requirements/base.txt`) | Pins in the manifest (direct dependencies only) |
| Python runtime profiles | `requirements/mlx.txt`, `requirements/cuda.txt` | Pins in the manifest (direct dependencies only) |
| Python development tools | `requirements-dev.txt` (includes `requirements.txt`) | Pins in the manifest (direct dependencies only) |
| Desktop frontend | `desktop/package.json` | `desktop/package-lock.json` |
| Tauri shell | `desktop/src-tauri/Cargo.toml` | `desktop/src-tauri/Cargo.lock` |
| Browser test harness | `tools/frontend_stress/package.json` | `tools/frontend_stress/package-lock.json` |

## System Prerequisites

All platforms need Git, Python 3.12, Node.js 22 or newer
(`desktop/package.json` declares `engines.node >=22`; `.node-version` pins 22),
Rust 1.88 or newer (`rust-version` in `Cargo.toml`), and FFmpeg with
`ffprobe`. Release builds copy `ffmpeg` and `ffprobe` into the packaged
sidecar.

### macOS

```bash
xcode-select --install
brew install python@3.12 node rust ffmpeg
```

Apple Silicon is the primary platform; the local MLX runtime needs it. Without
Homebrew, install equivalent versions and make sure `python3`, `node`, `npm`,
`cargo`, `ffmpeg`, and `ffprobe` are on `PATH`.

### Windows

Use 64-bit Windows 10 or newer and PowerShell. Tauri also needs the
[Microsoft C++ Build Tools](https://v2.tauri.app/start/prerequisites/#microsoft-c-build-tools)
with the **Desktop development with C++** workload, and Microsoft Edge
WebView2 (already present on current Windows 10 and 11).

Install the command-line prerequisites with `winget`:

```powershell
winget install --exact --id Git.Git
winget install --exact --id Python.Python.3.12
winget install --exact --id OpenJS.NodeJS.LTS
winget install --exact --id Rustlang.Rustup
winget install --exact --id Gyan.FFmpeg
```

Open a new PowerShell session, then confirm that `git`, `py`, `node`, `npm`,
`rustc`, `cargo`, `ffmpeg`, and `ffprobe` are on `PATH`.

### Linux

Linux is source-only; no Linux desktop package is published. Install the
[Tauri v2 Linux prerequisites](https://v2.tauri.app/start/prerequisites/#linux)
for your distribution, and Rust through [rustup](https://rustup.rs/).

Ubuntu 24.04:

```bash
sudo apt update
sudo apt install git python3.12 python3.12-venv ffmpeg nodejs npm \
  build-essential libwebkit2gtk-4.1-dev libssl-dev librsvg2-dev \
  libayatana-appindicator3-dev libxdo-dev pkg-config curl wget file
```

Fedora:

```bash
sudo dnf install git python3.12 ffmpeg-free nodejs npm gcc gcc-c++ make \
  webkit2gtk4.1-devel openssl-devel librsvg2-devel \
  libappindicator-gtk3-devel libxdo-devel pkgconf-pkg-config curl wget file
```

Distribution packages can be older than required, so check
`node --version` (22+) and `rustc --version` (1.88+). Local audio on Linux
needs an NVIDIA GPU with a driver that supports CUDA 12.6; other machines can
use the `base` profile for the library only.

## Python

### Source setup

macOS and Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python scripts/setup_runtime.py --profile base
python -m pip install -r requirements-dev.txt
```

Windows PowerShell, from a fresh clone:

```powershell
git clone https://github.com/gabrielmeleiro1/Orpheus.git
Set-Location Orpheus
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe scripts/setup_runtime.py --profile base --dry-run
.\.venv\Scripts\python.exe scripts/setup_runtime.py --profile base
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe run_tests.py --contract
Set-Location desktop
npm.cmd ci
npm.cmd run desktop:dev
```

Calling the virtual environment's interpreter directly avoids running
`Activate.ps1`, and `npm.cmd` works even when the execution policy blocks
`npm.ps1`. Use `npm.cmd` for every npm command in these docs when you are in
PowerShell.

The `--dry-run` step prints the fixed operations before anything is installed.
`requirements-dev.txt` already includes `requirements.txt`, so do not install
both. Use `requirements.txt` alone if you will not run tests, static analysis,
or sidecar packaging. Local model weights are optional and are not downloaded
by the base setup.

### Runtime profiles

`scripts/setup_runtime.py` installs one allowlisted profile, validates platform
compatibility, downloads the selected model groups, and records the selection.
Prefer it over installing a manifest by hand.

| Profile | Manifest | Platform | Provides |
| --- | --- | --- | --- |
| `base` | `requirements/base.txt` | Any | Library, playback of existing audio, source inspection |
| `mlx` | `requirements/mlx.txt` | macOS 14+ on arm64 | Kokoro on MLX and the MLX cleaner; no PyTorch |
| `cuda` | `requirements/cuda.txt` | Windows or Linux | Kokoro on CUDA or CPU, and the CUDA cleaner |

Model groups are passed with `--group` (`--list-groups` on
`scripts/download_models.py` prints them all):

| Group | Model |
| --- | --- |
| `tts-mlx` | `mlx-community/Kokoro-82M-bf16` |
| `tts-cpu-cuda` | `hexgrad/Kokoro-82M` |
| `cleaner-mlx` | `mlx-community/Llama-3.2-3B-Instruct-4bit` |
| `cleaner-mlx-small` | `mlx-community/Llama-3.2-1B-Instruct-4bit` |
| `cleaner-cuda` | `Qwen/Qwen2.5-1.5B-Instruct` |
| `cleaner-cuda-large` | `Qwen/Qwen2.5-3B-Instruct` |

A non-base profile needs a Kokoro voice group. Examples:

```bash
# macOS 14+ on Apple Silicon
python scripts/setup_runtime.py --profile mlx --group tts-mlx --group cleaner-mlx

# Linux with a supported NVIDIA GPU and driver
python scripts/setup_runtime.py --profile cuda --group tts-cpu-cuda --group cleaner-cuda
```

On Windows, run the same from the repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts/setup_runtime.py --profile cuda --group tts-cpu-cuda --group cleaner-cuda
```

The `cuda` profile checks the NVIDIA driver before installing. The Kokoro CPU
path needs no GPU; the CUDA cleaner needs a supported NVIDIA GPU with a CUDA
12.6 driver. `python -m pip install -r requirements/<profile>.txt` installs the
packages alone but does not mark setup as ready; for `cuda` it also misses the
CUDA PyTorch wheel (`torch==2.13.0+cu126`) that the setup script requests from
the PyTorch index.

The base profile deliberately leaves out spaCy, Thinc, and the English spaCy
model, which only local TTS uses. Both local profiles pin the official
`en-core-web-sm==3.8.0` wheel by URL and SHA-256.

### Setup from the desktop app

A debug desktop build run from a source checkout can install runtime profiles
from the Setup screen. The Tauri shell first verifies the fixed
`scripts/setup_runtime.py` and the checkout's own `.venv`, and it never accepts
an interpreter, script, model path, or shell command from the frontend.
Packaged releases and browser sessions cannot install Python dependencies;
they can still download missing models.

A packaged build contains exactly one profile. To change it, build a new
artifact from a trusted checkout; a packaged app never reaches back into a
build machine's checkout. See
[RUN_APPLICATION.md](RUN_APPLICATION.md#packaging) for
`ORPHEUS_RUNTIME_PROFILE` and the build commands.

### CUDA profile security

The `cuda` profile is opt-in. It adds `torch`, `transformers`, `accelerate`,
and `bitsandbytes` (4-bit and 8-bit quantization through
`CleanerConfig.cuda_quantization`). An OS match only makes the option
selectable; readiness still requires a successful NVIDIA GPU and driver probe.

`torch==2.13.0` resolved the previously accepted CVE-2025-3000 finding, and the
CUDA manifest had no known auditable vulnerabilities when it was pinned on
2026-07-18. Model artifacts are still executable input, so:

- load models only from the curated allowlist or another source you fully
  trust;
- never deserialize untrusted pickle files, checkpoints, or arbitrary local
  model paths;
- keep PyTorch out of the `base` and `mlx` profiles;
- run `pip-audit -r requirements/cuda.txt` for every release. A new finding
  blocks the release until it is fixed or reviewed and recorded.

### Clean-environment release gate

Before a source or packaged release, verify each shipped runtime in a new
virtual environment. The verifier installs only that profile's manifest, runs
`pip check`, imports the runtime without downloading models, checks that
packages from other profiles are absent, and audits the exact environment with
the host's pinned `pip-audit`:

```bash
# Apple Silicon release matrix
python run_tests.py --verify-profile base --verify-profile mlx

# On a Linux or Windows NVIDIA release host
python run_tests.py --verify-profile base --verify-profile cuda

# Direct form with machine-readable output
python scripts/verify_runtime_profiles.py --profile base --profile mlx --json
```

- The verifier runs exactly the profiles requested; it does not add `base`.
  For a release, request both `base` and the platform profile you ship.
- `mlx` needs arm64 macOS 14 or newer. `cuda` needs Linux or Windows and a
  passing `nvidia-smi` check. An unavailable profile is reported as `skipped`
  and exits 2; it is never reported as passed. `--allow-skipped` (or
  `run_tests.py --allow-skipped-profiles`) changes that exit code to 0 for
  cross-platform automation and still reports the skip. Failures exit 1.
- Every audit must report zero known vulnerabilities unless a finding has an
  exact, time-bounded entry in `security/runtime-audit-exceptions.json`.
  Accepted risk is never labeled passed. Expired, malformed, cross-profile,
  wrong-package, and new advisories fail the gate.
- The audit runs in strict mode: any collection error or unaudited
  distribution fails it. No audit tooling is installed into the target
  environment.
- The pinned `en-core-web-sm` wheel is a data artifact, not an exception. Its
  version, source URL, archive hash, and every hashed `RECORD` file are
  verified first; only that verified artifact is left out of a temporary
  audit view.
- `--work-dir PATH --keep-env` keeps the environments for diagnosis; otherwise
  they are removed.

### Local model assets

Python packages provide the libraries; model weights are downloaded
separately. The Setup screen handles this in the app. From the command line:

```bash
# Default: the package Kokoro model (hexgrad/Kokoro-82M)
python scripts/download_models.py

# Curated groups
python scripts/download_models.py --group tts-mlx --group cleaner-mlx
python scripts/download_models.py --group tts-cpu-cuda --group cleaner-cuda
```

Downloads go to the normal Hugging Face cache. On Windows without symlink
privileges, `huggingface_hub` copies files instead, so downloads work without
administrator rights. `--list-groups` prints the allowlist, `--dry-run` prints
the model IDs without downloading, and `--json` emits JSON Lines. Explicit
`--model` arguments are allowed and are deduplicated with group models in
first-seen order. The helper never accepts or stores API keys or arbitrary
download URLs.

To keep a copy inside the workspace, use `--local-dir`:

```bash
python scripts/download_models.py --local-dir .artifacts/models/kokoro
```

With several models, each snapshot goes in its own deterministic subfolder.
Point local runs at the copy in `.env.local`:

```bash
TTS_PROVIDER=cpu
TTS_MODEL_NAME=.artifacts/models/kokoro
```

## Environment

Copy the example file when you need local overrides. `AppConfig` loads
`.env.local` without overriding variables that are already set.

```bash
cp .env.example .env.local
```

[ENVIRONMENT.md](ENVIRONMENT.md) documents every variable. Never commit
`.env`, `.env.local`, or API keys.
