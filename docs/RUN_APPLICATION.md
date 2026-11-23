# Running the Application

This guide covers running Orpheus from source, the first-launch setup, where
data is kept, and how to package and install the desktop app. For the
component design, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Setup

Install the prerequisites and the Python environment as described in
[DEPENDENCIES.md](DEPENDENCIES.md). macOS and Linux commands use a POSIX shell.
Windows commands use PowerShell, the interpreter at `.\.venv\Scripts\python.exe`,
and `npm.cmd`, so they work even when script execution is disabled.

The `base` profile supports source inspection, library management, and
playback of existing audio. Both conversion modes (Audiobook and Document)
need a local TTS runtime from the `mlx` or `cuda` profile; the API cleaner
does not replace TTS. See
[runtime profiles](DEPENDENCIES.md#runtime-profiles) for the install commands.

Then start the app:

```bash
cd desktop
npm ci
npm run desktop:dev
```

This starts the Vite dev server, the Tauri shell, and the Python backend,
which Rust launches and supervises.

## First Launch and Models

The Setup screen checks the runtime, asks you to choose the Kokoro voice model
and an optional text cleaner with its precision, downloads only those models,
and then verifies that audio creation works. CUDA hardware is checked for the
local cleaner; narration falls back to CPU when CUDA is unavailable.
**Use library only** skips model downloads. A download can be cancelled; while
the worker stops, **Stopping download…** lets you return to the library.

Cleaner choices:

- **CUDA:** Qwen 2.5 1.5B or 3B. **4-bit**, **8-bit**, and **FP16** share one
  download because quantization is applied when the model loads. 4-bit uses
  the least GPU memory and FP16 the most; the download size is the same. The
  1.5B model is Apache-2.0; the 3B model is under the Qwen Research License,
  which allows non-commercial use only.
- **MLX:** Llama 3.2 3B or 1B, supplied as 4-bit models.

### Managing models

Use the **Voice & models** section of **Settings** to change the runtime or
the selected models.

- **Save runtime settings** reuses models already on the device and keeps the
  selection across restarts. Changing a cached model or precision needs no
  download. The saved model and precision become the conversion defaults.
- Switching to library-only mode does not delete downloaded models; switching
  back reuses them.
- The **Models** list shows each model's status and size on disk.
  **Download** appears for missing models and **Repair** for incomplete ones;
  neither changes the active selection. **Check runtime** retries discovery
  after a temporary startup or device failure and then shows the result and
  the time of the check, for example *Runtime ready.*
- The **Text cleaner · runtime model** group picks the cleaner model the
  runtime is set up with. The cleaner each book uses is chosen under
  **Text cleaning**.
- **Delete** removes that model's cached files after confirmation; other
  models, books, and audio are kept. Deleting Kokoro makes audio creation
  unavailable until you download it again or choose library-only mode.
  Damaged caches stay visible so you can delete them, and a partial deletion
  reports leftover files and the bytes actually removed. While a conversion,
  library maintenance, a model download, or a voice preview is running,
  **Delete** is disabled and gives the reason.
- You can keep several cleaners downloaded but use one at a time. The
  conversion form lists installed local models; download another supported
  one from **Voice & models**.

## Library Maintenance

The **Library** section of **Settings** has the maintenance actions. Restoring,
cleaning up, and removing folders ask for confirmation first, and **Cancel**
leaves everything as it is.

- **Check library** reports what a cleanup would remove and what it would keep,
  with the reason.
- **Back up** saves a dated copy of the library database. Backups are listed
  below with a **Restore** button; restoring replaces the current database
  after keeping a safety copy, and then reports any restored books whose audio
  or source file is missing.
- **Clean up** removes leftover temporary files. A database backup is kept
  when the cleanup changes the library, and finished audio is kept. After
  Orpheus replaces a damaged library database, Clean up keeps the old
  library's files until you restore a backup.
- **Other folders** appears when the output folder holds folders that no book
  in the library owns. They are kept unless you choose **Remove…** and confirm,
  which deletes those folders and any audio inside. This cannot be undone.

## Data Locations

The desktop app keeps `data/audiobooks.db`, `output/`, `temp/`, and
`runtime-setup.json` in its app data directory:

| OS | Desktop app data directory |
| --- | --- |
| macOS | `~/Library/Application Support/com.orpheus.desktop/` |
| Windows | `%APPDATA%\com.orpheus.desktop\` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/com.orpheus.desktop/` |

**Settings → Storage** shows the resolved paths, opens either folder, and
shows the log path once the log exists. The backend log is
`data/logs/backend.log` in the app data directory; attach it when you report a
problem. Models use the shared Hugging Face cache (normally
`~/.cache/huggingface/hub`, also on Windows); set `HF_HOME` or `HF_HUB_CACHE`
to move it.

When the backend runs outside Tauri, it uses repository-local `data/`,
`output/`, and `temp/`. Standalone `scripts/setup_runtime.py` keeps
`runtime-setup.json` in the desktop app data directory listed above, so the
desktop app sees the result. If `AUDIOBOOK_DATA_DIR` is set, setup state goes
in its parent directory; the desktop shell sets this automatically. State that
earlier builds wrote to `~/Library/Application Support/Orpheus/`,
`%LOCALAPPDATA%\Orpheus\`, or `${XDG_DATA_HOME:-~/.local/share}/orpheus/` is
still read until the next setup write; it is never modified or removed.

## Running the Backend Directly

```bash
python -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0
```

- `--host` accepts loopback addresses only (`127.0.0.1` or `localhost`).
- `--port` accepts `0`–`65535`; `0` picks an ephemeral port.
- After binding, stdout prints exactly one ready line:
  `{"event":"ready","host":"127.0.0.1","port":54321,"token":"...","runtime_profile":"development"}`.
  Send the token as a bearer token on `/api/*` requests. A packaged sidecar
  reports its bundled profile (`base`, `mlx`, or `cuda`) as `runtime_profile`.
- Without `AUDIOBOOK_*` overrides, the library lives in the checkout's `data/`,
  `output/`, and `temp/` folders, which Git ignores.
- The process shuts down cleanly on `SIGTERM`.

The full startup handshake and auth rules are in
[ARCHITECTURE.md](ARCHITECTURE.md#desktop-runtime).

## Packaging

### Build the desktop bundle

`npm run desktop:build` is the release build. Do not prebuild the sidecar.
Choose the runtime profile through the environment so Tauri's build hook
inherits it, and install that profile first:

```bash
# POSIX shell
cd desktop
ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build   # or mlx / cuda
```

```powershell
# PowerShell (the build hook calls `python`, so put the checkout's .venv first on PATH)
$env:Path = (Resolve-Path .\.venv\Scripts).Path + ";" + $env:Path
Set-Location desktop
$env:ORPHEUS_RUNTIME_PROFILE = "base" # or "mlx" / "cuda"
npm.cmd run desktop:build
Remove-Item Env:ORPHEUS_RUNTIME_PROFILE
```

When `ORPHEUS_RUNTIME_PROFILE` is unset, a rebuild keeps the profile recorded
in the previous sidecar's `runtime-profile.json`, so rebuilding the UI cannot
silently drop the local runtime. A first build defaults to `mlx` for an Apple
Silicon target and `base` elsewhere. Only `base`, `mlx`, and `cuda` are
accepted.

Tauri's `beforeBuildCommand` runs
`node scripts/run-python.mjs ../scripts/build_desktop_backend.py` and then
`npm run build`. The launcher uses the checkout's `.venv` Python when it
exists and otherwise `python3` or `python` on `PATH`. The sidecar build:

- validates only the selected profile's dependencies, plus PyInstaller and the
  system binaries the sidecar needs;
- builds `apps.launchers.desktop_backend` with `desktop/backend-sidecar.spec`
  into the PyInstaller onedir folder
  `desktop/src-tauri/binaries/orpheus-desktop-backend/`;
- copies the resolved `ffmpeg` and `ffprobe` into the sidecar's `bin/` folder
  and fails if either is missing;
- removes stale sidecar files from `desktop/src-tauri/binaries/`.

Tauri exports the Rust target as `TAURI_ENV_TARGET_TRIPLE`. The sidecar
builder reads it before `CARGO_BUILD_TARGET` or `TARGET` and rejects a host and
target OS or architecture mismatch before probing dependencies, so a
wrong-architecture PyInstaller executable cannot enter the bundle. Tauri then
embeds `binaries/` as a resource, and the packaged app runs that sidecar
instead of a repository interpreter.

To diagnose the sidecar builder on its own, run it directly. An explicit
`--runtime-profile` overrides the environment:

```bash
python scripts/build_desktop_backend.py --runtime-profile base
python scripts/build_desktop_backend.py --validate-existing
```

### Windows installer

The Windows installer bundles the `cuda` profile and is built with Inno Setup 7,
which handles the long paths in the PyTorch runtime. From the repository root,
after installing the CUDA profile and the build prerequisites:

```powershell
$env:Path = (Resolve-Path .\.venv\Scripts).Path + ";" + $env:Path
$env:ORPHEUS_RUNTIME_PROFILE = "cuda"
Set-Location desktop
npm.cmd ci
npm.cmd run desktop:build -- --no-bundle
& "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe" windows-installer.iss
Get-FileHash .\release\Orpheus_1.0.0_x64-setup.exe -Algorithm SHA256
```

Adjust the compiler path if Inno Setup is installed elsewhere. The installer
script validates the CUDA sidecar before compiling. Publish the installer with
its SHA-256 checksum. The Rust executable alone is not a portable app; it needs
the matching `binaries/orpheus-desktop-backend/` folder.

Installer behavior:

- It installs per user into `%LOCALAPPDATA%\Orpheus` and installs WebView2 if
  it is missing. Upgrades and uninstalls keep the app data directory and the
  shared model cache.
- Only one installer runs at a time. An upgrade stages the new app and backend
  and keeps the previous versions until setup succeeds. A failed activation
  restores both, reports the failure, and does not offer to launch the app. If
  setup is interrupted during activation, running it again restores the
  previous app and backend before trying again.
- `/EXTRACTONLY=1 /DIR=<empty temporary directory>` extracts the payload
  without registering an install, creating shortcuts, or launching the app.
  Check the exit code and compare the files with the build output. This does
  not exercise installation or WebView2 setup.

For a realistic launch check, open the installer and the installed app from
Windows Explorer. A process started by another packaged (MSIX) Windows
application can inherit that application's file-system virtualization and see
a different app data folder at the same path. If Orpheus then reports a
storage ownership mismatch, keep the library and its ownership markers and
retry from Explorer; do not reset or force-adopt the data.

### macOS install

```bash
cd desktop
npm run desktop:install   # copy the last build into /Applications
npm run desktop:release   # build, then replace /Applications/Orpheus.app
```

`desktop:release` runs the same Tauri build (fresh sidecar, frontend, bundle),
replaces `/Applications/Orpheus.app`, and removes the build-output `.app` so
Spotlight shows only the installed copy. Prefix it with
`ORPHEUS_RUNTIME_PROFILE=base|mlx|cuda` to choose a profile.

The bundle keeps a stable name and identifier, `Orpheus.app` and
`com.orpheus.desktop`, at version `1.0.0`. Dragging from the `.dmg` or running
`desktop:install` replaces the same app instead of adding a second
Applications entry.

### Unsigned macOS releases

Published Apple Silicon DMGs are unsigned and unnotarized. Users install them
with [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md). Maintainers
follow [RELEASING_MACOS.md](RELEASING_MACOS.md), which runs the full local
release gate (`scripts/release_qualification.py`). Source CI
(`.github/workflows/source-validation.yml`) validates source only and does not
publish artifacts.

## Troubleshooting

If a conversion fails, check:

- the **Event log** in the conversion details panel;
- `data/logs/backend.log` in the app data directory;
- the desktop devtools console and network log when running `desktop:dev`;
- `output/` for generated files and `temp/` for intermediate files;
- **Settings → Storage** for the resolved data paths.

To check an installed macOS build, launch and quit it:

```bash
open "/Applications/Orpheus.app"
osascript -e 'tell application "Orpheus" to quit'
```

The bundled `orpheus-desktop-backend` should start with the app and stop when
it quits normally. If the app is force-killed, the backend notices that its
parent is gone and exits; if one is ever left behind, stop it with:

```bash
pkill -f "Orpheus.app/Contents/Resources/binaries/orpheus-desktop-backend"
```

Automated packaged-app smoke checks are in
[TESTING.md](TESTING.md#packaged-app-smoke-macos).
