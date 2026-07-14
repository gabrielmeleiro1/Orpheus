# Dependencies

This repository is intended to be cloneable from GitHub using the committed
dependency manifests. Prefer the manifests below over ad hoc package installs.

## System Prerequisites

Install these once on a macOS development machine:

```bash
xcode-select --install
brew install python@3.12 node rust ffmpeg
```

- macOS on Apple Silicon is the primary supported runtime. The local MLX stack
  is Apple Silicon oriented. Headless NVIDIA GPU text cleaning uses the
  ``cuda`` cleaner backend and does not require Apple Silicon.
- Python 3.12 is recommended for the desktop sidecar build and is the version
  used by the packaged runtime dependencies.
- Node.js is used by the React/Vite/Tauri frontend.
- Rust 1.88 or newer is required by the Tauri shell; crate versions are resolved
  through `desktop/src-tauri/Cargo.lock`.
- `ffmpeg` and `ffprobe` are required for audio encoding and are copied into the
  packaged sidecar during release builds.

If Homebrew is not used, install equivalent versions through your preferred
package manager and ensure `python3`, `node`, `npm`, `cargo`, `ffmpeg`, and
`ffprobe` are on `PATH`.

## Python

Runtime packages are listed in:

```bash
requirements.txt
```

Contributor and packaging tools are listed in:

```bash
requirements-dev.txt
```

Install the shared application and contributor dependencies first:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes `requirements.txt`, so do not install both
separately.

### Source setup versus packaged profiles

Dynamic profile installation from the desktop setup page is a development
feature for a source checkout. The Tauri debug shell verifies the fixed
`scripts/setup_runtime.py` file and the checkout-owned `.venv` before enabling
install, cancel, or setup-restart actions. It never accepts an interpreter,
script, model path, or shell command from the frontend.

Packaged releases and browser sessions report dynamic setup as unavailable.
Their sidecar must already contain the profile selected at build time with
`scripts/build_desktop_backend.py --runtime-profile base|mlx|cuda`. A packaged
`base` build still supports document ingestion, conversion workflows that do
not require local models, library management, and playback. To change a
packaged runtime profile, build a new artifact from a trusted source checkout;
the packaged app does not reach back to a build-machine checkout path.

Local ML runtimes are opt-in profiles. Install exactly one when local synthesis
or cleaning is needed:

```bash
# macOS 14+ on arm64 Apple Silicon; contains no PyTorch
python -m pip install -r requirements/mlx.txt

# Linux/Windows with a supported NVIDIA GPU and driver
python -m pip install -r requirements/cuda.txt
```

The base profile deliberately excludes spaCy, Thinc, and the English spaCy
model because they are used only by local TTS. Both local profiles pin the
official `en-core-web-sm==3.8.0` wheel by URL and SHA256. During release
verification, that data artifact is not treated as a vulnerability exception:
its installed version, source URL, archive hash declaration, and every hashed
`RECORD` file are verified first. Only that exact verified artifact is removed
from a temporary metadata-only audit view; all other installed distributions
must remain covered by strict `pip-audit`.

The desktop sidecar builder follows the same boundary. It defaults to `mlx` for
an `aarch64-apple-darwin` target and to `base` elsewhere. CUDA packaging must be
requested explicitly:

```bash
python scripts/build_desktop_backend.py --runtime-profile cuda
```

### CUDA cleaner backend and accepted PyTorch risk

The `cuda` profile is an explicit opt-in. It contains:

- ``torch`` — CUDA runtime and model execution
- ``transformers`` — Hugging Face model loading and generation
- ``bitsandbytes`` — optional 4-bit / 8-bit quantization (``CleanerConfig.cuda_quantization``)

Operating-system compatibility only makes the CUDA option selectable. Actual
readiness still requires a successful NVIDIA GPU and driver probe.

As of 2026-07-14, `torch==2.10.0` is affected by PYSEC-2026-139 / CVE-2026-4538
and CVE-2025-3000, and upstream publishes no fixed release. This risk is accepted
only for the opt-in CUDA profile with these controls:

- Load model artifacts only from the curated project allowlist or another source
  the user fully trusts.
- Never deserialize untrusted pickle files, checkpoints, or arbitrary local model
  paths. Treat model files as executable input.
- Keep PyTorch absent from the base and MLX profiles.
- Run `pip-audit -r requirements/cuda.txt` at every release and upgrade as soon as
  upstream provides a fixed compatible version. A newly available fix blocks the
  release until the pin and clean-environment verification are updated.

This is a scoped acceptance, not a claim that the advisories are fixed.

### Clean-environment release gate

Before publishing a source or packaged release, verify each intended runtime in
a brand-new virtual environment. The verifier installs only that profile's
manifest, runs `pip check`, performs imports without downloading models, checks
that prohibited cross-profile packages are absent, and audits the exact target
environment with the host's pinned `pip-audit`:

```bash
# Primary Apple Silicon release matrix
python run_tests.py --verify-profile base --verify-profile mlx

# Run on a Linux/Windows NVIDIA release host
python run_tests.py --verify-profile base --verify-profile cuda

# Direct form with machine-readable output
python scripts/verify_runtime_profiles.py \
  --profile base --profile mlx --json
```

The verifier runs exactly the profiles requested; it does not add `base`
implicitly. A release command must request both `base` and the platform profile
that will ship. MLX requires arm64 macOS 14 or newer. CUDA requires Linux or
Windows and a successful `nvidia-smi` GPU/driver preflight. A requested
unavailable profile is reported as `skipped` and exits with status 2; it is
never reported as passed. `--allow-skipped` (or
`run_tests.py --allow-skipped-profiles`) changes that exit code to zero for
cross-platform automation, while retaining the skipped result in the report.
Failures exit 1.

The base and MLX audits must report zero known vulnerabilities. CUDA's two
unfixed PyTorch findings may produce `accepted_risk` only while their exact,
time-bounded entries in `security/runtime-audit-exceptions.json` remain valid.
Accepted risk can satisfy the gate but is deliberately never labeled passed.
Expired, malformed, cross-profile, wrong-package, and newly discovered
advisories fail the gate. No audit tooling is installed into the target
environment, so the inventory remains the profile plus its pinned packaging
tools. The audit runs in strict path mode; any dependency collection error or
skipped/unaudited distribution fails the gate. Use `--work-dir PATH --keep-env`
only for developer diagnosis; otherwise all clean environments are removed.

### Local model assets

Python requirements install the libraries needed to run local models, but model
weights are downloaded separately. The package-backed Kokoro runtime uses the
Hugging Face model declared in `modules.tts.runtime_spec.KOKORO_PACKAGE_DEFAULT_MODEL`.

Download the required local model snapshots after installing Python packages:

```bash
python scripts/download_models.py
```

By default this downloads `hexgrad/Kokoro-82M` into the normal Hugging Face
cache with Hugging Face cache symlinks disabled, which avoids the common
Windows non-admin symlink failure.

Choose one or more curated groups when configuring an optional local runtime:

```bash
# Apple Silicon
python scripts/download_models.py --group tts-mlx --group cleaner-mlx

# NVIDIA CUDA / package-backed CPU TTS
python scripts/download_models.py --group tts-cpu-cuda --group cleaner-cuda
```

`--list-groups` prints the stable allowlist and its exact Hugging Face model
IDs. Add `--json` for machine-readable setup output. Explicit `--model`
arguments remain supported for advanced users and are deduplicated with group
models in first-seen order. Model IDs are reported before any download begins;
the helper never accepts or persists API keys or arbitrary download URLs.

To keep a copied model directory inside the workspace, use:

```bash
python scripts/download_models.py --local-dir .artifacts/models/kokoro
```

When multiple models are selected with `--local-dir`, each snapshot is placed
in its own deterministic, filesystem-safe subdirectory so model files cannot
overwrite one another.

Then point local runs at that model directory in `.env.local`:

```bash
TTS_PROVIDER=cpu
TTS_MODEL_NAME=.artifacts/models/kokoro
```

Release packaging can place the same snapshot into a bundle directory:

```bash
python scripts/download_models.py --local-dir desktop/src-tauri/binaries/models/kokoro
```

## Desktop Frontend

JavaScript packages are declared in `desktop/package.json` and locked in
`desktop/package-lock.json`.

```bash
cd desktop
npm ci
```

Use `npm ci` for GitHub clones so installs follow the lockfile exactly.

## Tauri and Rust

Rust dependencies are declared in `desktop/src-tauri/Cargo.toml` and locked in
`desktop/src-tauri/Cargo.lock`. They are fetched automatically by Cargo during:

```bash
cd desktop
npm run desktop:build
```

## Environment

Copy the example environment file when local overrides are needed. `AppConfig`
loads `.env.local` without overriding already exported environment variables.

```bash
cp .env.example .env.local
```

Never commit `.env`, `.env.local`, API keys, generated databases, generated
media, `desktop/node_modules`, `desktop/dist`, `desktop/src-tauri/target`, or
`desktop/src-tauri/binaries`. These are covered by `.gitignore`.

## Common Workflows

Run the desktop app from source:

```bash
source .venv/bin/activate
cd desktop
npm ci
npm run desktop:dev
```

Build and install the macOS app:

```bash
source .venv/bin/activate
cd desktop
npm ci
npm run desktop:release
```
