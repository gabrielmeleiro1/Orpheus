# Running the Application

Architecture reference: `docs/ARCHITECTURE.md`

## 1. Setup

Install the system prerequisites listed in [DEPENDENCIES.md](DEPENDENCIES.md)
first.

```bash
python3.12 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

Use `requirements.txt` instead when you only need runtime dependencies and do
not plan to run tests, static analysis, or desktop sidecar packaging.

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

Build the backend sidecar into Tauri's bundled resource directory:

```bash
python scripts/build_desktop_backend.py
```

This command:
- validates required ML/audio runtime dependencies, PyInstaller, and system
  binaries needed by the sidecar build
- builds `apps.launchers.desktop_backend` via `desktop/backend-sidecar.spec`
- writes the PyInstaller onedir sidecar to:
  `desktop/src-tauri/binaries/orpheus-desktop-backend/`
- copies the exact resolved `ffmpeg` and `ffprobe` executables into the sidecar
  `bin/` directory and fails if those packaged runtime resources are missing
- removes stale one-file target-triple sidecars from `desktop/src-tauri/binaries/`

Then build the desktop app bundle:

```bash
cd desktop
npm run desktop:build
```

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
python scripts/build_desktop_backend.py
cd desktop
npm run desktop:build
npm run desktop:install
```

The first command is the Python/core gate. The remaining commands cover the
desktop UI, Tauri shell, sidecar packaging, release bundle, and installed-app
path.

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
