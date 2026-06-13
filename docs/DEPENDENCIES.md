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
- Rust is used by the Tauri shell and is resolved through `desktop/src-tauri/Cargo.lock`.
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
