# Running the Application

Architecture reference: `docs/ARCHITECTURE.md`

## 1. Setup

Install the system prerequisites listed in [DEPENDENCIES.md](DEPENDENCIES.md)
first.

Use the platform-specific source setup in
[DEPENDENCIES.md](DEPENDENCIES.md#system-prerequisites): macOS commands use a
POSIX shell, while Windows commands use PowerShell and
`.\.venv\Scripts\Activate.ps1`.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python scripts/setup_runtime.py --profile base
python -m pip install -r requirements-dev.txt
```

The `base` profile supports source inspection, library management, and playback
of existing audio. Both conversion modes require a local TTS runtime; cloud
cleaning is not a replacement for TTS. Base does not install local models. `requirements-dev.txt`
adds contributor and PyInstaller tooling on top of those base dependencies.
Use `requirements.txt` instead when you do not plan to run tests, static
analysis, or desktop sidecar packaging.

Select and install a local runtime before using local TTS/cleaning or building
a sidecar that bundles it:

```bash
# macOS 14+ on arm64 Apple Silicon
python scripts/setup_runtime.py --profile mlx \
  --group tts-mlx --group cleaner-mlx

# Linux with a supported NVIDIA GPU and driver; Windows uses the PowerShell dependency guide
python scripts/setup_runtime.py --profile cuda \
  --group tts-cpu-cuda --group cleaner-cuda
```

The CUDA profile is opt-in. Load only trusted model artifacts. To install
dependencies without downloading models, omit the `--group` arguments. The equivalent fixed
manifests are `requirements/base.txt`, `requirements/mlx.txt`, and
`requirements/cuda.txt`; prefer `setup_runtime.py` because it validates platform
compatibility and records the selected profile.

Packaged builds download any missing managed models from the Setup screen. The
download can be cancelled there; cancellation is cooperative and may wait for
the current Hugging Face transfer call to return.

The first page shows the voice model (Kokoro), one optional text cleaner, and its
precision. CUDA offers Qwen 2.5 1.5B and 3B; MLX offers Llama 3.2 3B and 1B.
Only the selected models are installed. CUDA's **4-bit**, **8-bit**, and **FP16**
choices share one model download: quantization is applied when the model loads.
4-bit uses the least GPU memory; FP16 uses the most. The download size does not
shrink with lower precision. MLX cleaners are supplied as 4-bit models.

After setup, use **Settings → Runtime setup** to change the runtime or selected
models. **Save runtime settings** reuses models already on this device and keeps
the selection across restarts. Switching to library-only mode does not delete
downloaded models; switching back reuses them. Missing models offer **Install**.
Use **Check runtime** to retry discovery after a temporary startup or device
failure. The cleaner backend, model, and CUDA device can be changed directly in
Settings when that backend is available.

**Models on this device** shows each managed model's installation status and
cache size. **Delete** removes that model's shared cached files after confirmation;
other models, books, and audio are preserved. **Install again** downloads it
without changing the active runtime selection. Select the models you want to use
and save the runtime settings separately. Deleting the last selected model leaves
the app in library-only mode. Model deletion is blocked during conversions or
another setup operation.

Keep multiple cleaners downloaded, but select one at a time. Changing a cached
model or precision uses **Save runtime settings** and needs no download. The
saved model and precision become the defaults for conversion, including after
a restart. Conversion's model selector lists installed local models; use
**Runtime setup** to install another supported model.

## 2. Validate Python/Core

```bash
python run_tests.py --contract
```

This validates the Python/core regression gate only. It excludes desktop
TypeScript, Tauri/Rust, packaged sidecar, release install, and property,
benchmark, slow, and UI suites.

Optional faster smoke pass:
```bash
python run_tests.py
```

## 3. Troubleshooting

If conversion fails, inspect:
- desktop devtools/network logs when running the Tauri dev shell
- server logs in terminal
- `output/` for generated artifacts
- `temp/` for transient artifacts
- `data/audiobooks.db` for repository-local library state
- the app data folder for Tauri-managed runtime state

## 4. Desktop Runtime Contract

The desktop flow uses this runtime contract:

1. Tauri launches a desktop backend process:
   - development: `python -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0`
   - production: bundled sidecar executable (`orpheus-desktop-backend`)
2. Once bound to local host/port, Python prints one machine-readable startup JSON line:
   `{"event":"ready","host":"127.0.0.1","port":54321,"token":"..."}`
3. Python also writes the payload to the app-data ready file (`backend-ready.json`)
   so the shell can recover it if stdout is unavailable.
4. React reads `host`/`port`/`token` from Tauri and calls the local API on the
   announced socket.

Python continues to own conversion orchestration and local storage through the
configured data, output, temp, and SQLite paths.

### Desktop backend launcher

Start the desktop backend directly:

```bash
python -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0
```

- `--host` accepts loopback addresses only (`127.0.0.1` or `localhost`).
- `--port` accepts `0-65535`; `0` requests an ephemeral port.
- On successful bind, stdout emits exactly one JSON ready line:
  `{"event":"ready","host":"127.0.0.1","port":54321,"token":"..."}`
- Process handles `SIGTERM` cleanly for desktop lifecycle control.
- Desktop mode enables CORS for the Vite dev server at `http://localhost:5173`
  and packaged WebView origins.

### Build desktop sidecar for production packaging

The release path is `npm run desktop:build`; do not prebuild a sidecar first.
Select the installed runtime profile through the environment so Tauri's fresh
sidecar hook inherits it:

```bash
# POSIX shell
cd desktop
ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build
ORPHEUS_RUNTIME_PROFILE=mlx npm run desktop:build
ORPHEUS_RUNTIME_PROFILE=cuda npm run desktop:build
```

```powershell
# PowerShell
Set-Location desktop
$env:ORPHEUS_RUNTIME_PROFILE = "base" # or "mlx" / "cuda"
npm run desktop:build
Remove-Item Env:ORPHEUS_RUNTIME_PROFILE
```

When `ORPHEUS_RUNTIME_PROFILE` is unset, a rebuild retains the profile recorded
in the previous sidecar's `runtime-profile.json`, so rebuilding the UI cannot
silently remove the local runtime. A first build defaults to `mlx` for an
Apple Silicon target and `base` elsewhere. Only `base`, `mlx`, and `cuda` are
accepted. An explicit standalone `--runtime-profile` takes precedence over the
environment and is useful for diagnosing the sidecar builder directly:

```bash
python scripts/build_desktop_backend.py --runtime-profile base
```

The sidecar build:
- validates only the selected profile's runtime dependencies, plus PyInstaller
  and system binaries needed by the sidecar build
- builds `apps.launchers.desktop_backend` via `desktop/backend-sidecar.spec`
- writes the PyInstaller onedir sidecar to:
  `desktop/src-tauri/binaries/orpheus-desktop-backend/`
- copies the exact resolved `ffmpeg` and `ffprobe` executables into the sidecar
  `bin/` directory and fails if those packaged runtime resources are missing
- removes stale target-triple sidecar files and directories from
  `desktop/src-tauri/binaries/`

`desktop:build` is the Tauri release build command. Tauri's
`beforeBuildCommand` first runs `python ../scripts/build_desktop_backend.py`
to create a fresh architecture-matched sidecar, then runs `npm run build` for
the frontend before packaging. The diagnostic `--validate-existing` mode is
available when you need to inspect an already-produced sidecar, but it is not
part of the release path.

Tauri exports the selected Rust target as `TAURI_ENV_TARGET_TRIPLE`; the
sidecar builder uses it before `CARGO_BUILD_TARGET` or `TARGET`, and rejects a
host/target OS or architecture mismatch before dependency or PyInstaller
probes. This prevents a native PyInstaller executable for the wrong
architecture from entering the bundle.

During `desktop:build`, Tauri embeds the `binaries/` resource declared in `tauri.conf.json`.
The production app launches the executable inside that bundled onedir resource instead of
the repo-local Python interpreter.

To install the built app into `/Applications` by replacement:

```bash
cd desktop
npm run desktop:install
```

To rebuild the sidecar, rebuild the `.dmg`, replace `/Applications/Orpheus.app`,
and remove the temporary build-output `.app` so Spotlight shows the installed app only:

```bash
cd desktop
npm run desktop:release
```

`desktop:release` delegates to the same Tauri build hook (fresh sidecar,
frontend, then bundle) before installing the resulting app. Prefix it with
`ORPHEUS_RUNTIME_PROFILE=base|mlx|cuda` in a POSIX shell, or set the equivalent
`$env:ORPHEUS_RUNTIME_PROFILE` in PowerShell, to select a non-default profile.

The desktop bundle keeps the stable app name and bundle identifier:
`Orpheus.app` / `com.orpheus.desktop`. The current release metadata is `1.0.0`,
so dragging from the `.dmg` or using `desktop:install` replaces the same app bundle rather
than creating a second Applications entry.

### Desktop dev launch (full shell + backend)

```bash
cd desktop
npm ci
npm run desktop:dev
```

This launches:
- Vite frontend dev server
- Tauri shell
- Python desktop backend process managed by Rust

### Release-Readiness Command Matrix

Run these commands in order before treating a desktop build as release-ready:

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
cd desktop
npm run desktop:build
npm run desktop:install
```

The first command is the Python/core gate. The remaining commands cover the
desktop UI, Tauri shell, sidecar packaging, release bundle, and installed-app
path.

### Unsigned macOS release install

Published Apple Silicon DMGs are **unsigned** and **unnotarized**. End users
should follow [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md): verify
`SHA256SUMS` with `shasum -a 256` before first open, then use right-click
**Open** or System Settings **Open Anyway**.

Maintainers producing a GitHub Release must run the full local gate in
[RELEASING_MACOS.md](RELEASING_MACOS.md) (`scripts/release_qualification.py`).
Source CI (`.github/workflows/source-validation.yml`) remains source validation
only and does not publish release artifacts.

### Packaged app smoke (manual/ops)

Launch and close the installed packaged app:

```bash
open "/Applications/Orpheus.app"
osascript -e 'tell application "Orpheus" to quit'
```

Expected:
- app process starts
- bundled `orpheus-desktop-backend` starts
- normal app quit stops backend sidecar

To exercise sidecar startup plus packaged `ffmpeg`/`ffprobe` under a Finder-like
reduced `PATH` without running an ML conversion:

```bash
python scripts/desktop_runtime_smoke.py --backend sidecar --reduced-path --check-ffmpeg-runtime
```

Known behavior:
- If the app process is force-killed (not normal quit), backend sidecar can remain running and should be manually cleaned up:

```bash
pkill -f "Orpheus.app/Contents/MacOS/orpheus-desktop-backend"
```
