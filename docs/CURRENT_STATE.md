# Current State (How It Works Now)

Architecture reference: `docs/ARCHITECTURE.md`

## Runtime Model

The app's primary interactive UI is the desktop shell:
- Development: `cd desktop && npm run desktop:dev`
- Release/install: `cd desktop && npm run desktop:release`
- Installed app: `/Applications/Orpheus.app`

In development, runtime state can live under the repository-local `data/`,
`output/`, and `temp/` directories. When the Tauri shell launches the backend,
it injects app-data paths for those directories and for `audiobooks.db`.

## Execution Flow

1. Tauri launches the Python desktop backend and receives a ready payload with host, port, and token.
2. React creates an authenticated API client for that local backend.
3. Source file is selected in the desktop UI.
4. UI boot reads `/api/system/capabilities` and aligns cleaner defaults with the real runtime probe state before a run starts.
5. `AppController.start_conversion(...)` validates runtime and starts a background job.
6. `ConversionPipeline.convert(...)` executes ingest -> chunk -> clean -> synthesize -> encode/package.
7. Progress/log/state events stream via SSE (`/api/events/stream`).
8. React updates state/progress/logs and library playback in real-time, including weighted stage progress and ETA when stable enough to show.
9. Library loads automatically prune rows whose original `source_path` is missing, and library actions can open the selected book folder or delete the book plus its related files.
10. Packaged playback uses tokenized local audio URLs with desktop media headers.
11. Library metadata persists in SQLite under the configured local app data directory.

## Key Modules

See [ARCHITECTURE.md](ARCHITECTURE.md#where-to-change-x) for where to edit parsers,
cleaners, TTS runtime, the conversion form, API payloads, and storage schema.
Package-level maps: [architecture/python-boundaries.md](architecture/python-boundaries.md),
[architecture/desktop-boundaries.md](architecture/desktop-boundaries.md).

- Desktop UI:
  - `desktop/src/App.tsx`
  - `desktop/src/api/client.ts`
  - `desktop/src/features/*`
  - `desktop/src-tauri/src/*`
- Backend API:
  - `modules/web/server.py`
  - `modules/web/events.py`
  - `modules/web/routes/*`
  - `modules/web/desktop_auth.py`
  - `modules/web/source_handles.py`
- Controller + config:
  - `modules/app/controller.py`
  - `modules/app/config.py`
  - `modules/app/events.py`
  - `modules/app/services/runtime_capability_service.py`
  - `modules/app/services/conversion_service.py`
- Pipeline + engines:
  - `modules/pipeline/orchestrator.py`
  - `modules/pipeline/book_workspace.py`
  - `modules/pipeline/chapter_dispatch.py`
  - `modules/pipeline/progress.py`
  - `modules/tts/*`
  - `modules/audio/*`
  - `modules/ingestion/*`
- Shared provider helpers:
  - `modules/openai_compat.py`
  - `modules/tts/cleaner_transport.py`
  - `modules/tts/cleaner_trace.py`
  - `modules/tts/engine_runtime.py`
  - `modules/tts/engine_output.py`
- Persistence:
  - `modules/storage/*`

## Commands

Desktop frontend checks:
```bash
cd desktop
npm run typecheck
npm run build
```

Python/core regression gate:
```bash
python run_tests.py --contract
```

Fast local smoke:
```bash
python run_tests.py
```
