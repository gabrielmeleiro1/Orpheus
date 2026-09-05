# Current State (How It Works Now)

Last verified against the code: 2026-07-27.

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

1. Tauri launches the packaged Python backend (sidecar) and receives a single
   ready payload with host, port, and token; startup is generation-fenced so a
   restart can never accept a stale readiness event.
2. React creates an authenticated API client for that local backend.
3. Sources arrive by file selection, upload, or pasted text; pasted text is
   auto-queued on Start.
4. UI boot reads `/api/system/capabilities` and aligns cleaner defaults with
   the real runtime probe state before a run starts.
5. `AppController.start_conversion(...)` validates runtime and starts a
   background job; direct starts and queued starts are serialized so the same
   task cannot launch twice.
6. `ConversionPipeline.convert(...)` executes ingest -> chunk -> clean ->
   synthesize -> encode/package, publishing the final audiobook atomically.
7. Progress/log/state events stream via SSE (`/api/events/stream`); every
   event carries job ownership, so late events from an old job cannot update a
   newer one.
8. React updates state/progress/logs in real time, including weighted stage
   progress and ETA when stable enough to show.
9. The library list is paged on demand (50 books per page with incremental
   load-more); actions can open the selected book folder, repair, or delete
   the book plus its related files. Pruning stale rows/artifacts is an
   explicit maintenance action with backups, never an automatic side effect
   of loading.
10. Packaged playback uses tokenized local audio URLs with desktop media
    headers; playback position saves are scoped to the owning book and client
    generation.
11. Library metadata persists in SQLite under the configured local app data
    directory.

## Key Modules

See [ARCHITECTURE.md](ARCHITECTURE.md#where-to-change-x) for where to edit parsers,
cleaners, TTS runtime, the conversion form, API payloads, and storage schema.

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
npm test -- --run
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

The full verification matrix (property/benchmark/slow lanes, Rust, packaged
artifact) is documented in [TESTING.md](TESTING.md).
