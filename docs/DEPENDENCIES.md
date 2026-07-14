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

Install everything needed for development, tests, and desktop packaging:

```bash
python3.12 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes `requirements.txt`, so do not install both
separately.

### CUDA cleaner backend

The ``cuda`` cleaner backend shares the committed PyTorch stack in
``requirements.txt``:

- ``torch`` — CUDA runtime and model execution
- ``transformers`` — Hugging Face model loading and generation
- ``bitsandbytes`` — optional 4-bit / 8-bit quantization (``CleanerConfig.cuda_quantization``)

These packages are installed with the normal development workflow above. A CUDA-capable
NVIDIA driver and GPU are required at runtime; the desktop sidecar build on macOS
does not bundle CUDA wheels.

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
Windows non-admin symlink failure. To keep a copied model directory inside the
workspace, use:

```bash
python scripts/download_models.py --local-dir .artifacts/models/kokoro
```

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
source venv/bin/activate
cd desktop
npm ci
npm run desktop:dev
```

Build and install the macOS app:

```bash
source venv/bin/activate
cd desktop
npm ci
npm run desktop:release
```
