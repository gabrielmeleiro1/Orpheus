# Architecture

This document describes the current runtime architecture, backend surface, and local verification expectations for Orpheus.

## Repository Shape

- Launchers: `apps/launchers/desktop_backend.py`
- Desktop UI: `desktop/*`
- API/interface layer: `modules/web/*`
- Application layer: `modules/app/*`
- Processing layer: `modules/pipeline/*`, `modules/tts/*`, `modules/audio/*`, `modules/ingestion/*`
- Persistence layer: `modules/storage/*`
- Documentation: `docs/*`
- Local runtime state: `data/`, `output/`, `temp/`

## System Overview

Orpheus is desktop-first:
- Desktop app: `Tauri shell` -> `Python desktop backend` -> `React UI`

The React UI calls the local FastAPI routes in `modules/web/*`. Those routes call
into `AppController`, which owns job lifecycle, conversion orchestration, and
library operations.

Desktop runtime handshake contract:
1. Tauri launches the Python desktop backend process.
2. After bind, Python emits one machine-readable JSON line on stdout:
   `{"event":"ready","host":"127.0.0.1","port":54321,"token":"..."}`
3. Python also writes the same payload to the app-data ready file (`backend-ready.json`) for packaged-app recovery.
4. React reads the endpoint/token from Tauri and calls the local API on that socket.

Python continues to own conversion orchestration and local storage, including SQLite.
Tauri/Rust owns desktop shell duties: window lifecycle, process lifecycle, app paths,
and packaging.

## Desktop Runtime Topology (Tauri Release)

Desktop runtime modes:
- Development: `cd desktop && npm run desktop:dev`
  - Rust launches `python -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0`
  - Rust injects runtime paths through env:
    - `AUDIOBOOK_DATA_DIR`
    - `AUDIOBOOK_OUTPUT_DIR`
    - `AUDIOBOOK_TEMP_DIR`
    - `AUDIOBOOK_DB_PATH`
    - `AUDIOBOOK_DESKTOP_READY_FILE`
  - `AUDIOBOOK_DESKTOP_PYTHON` can override the Python executable.
  - `AUDIOBOOK_DESKTOP_USE_SIDECAR=1` forces the sidecar path in debug builds.
- Production package: `cd desktop && npm run desktop:build`
  - Tauri launches bundled sidecar from `Contents/Resources/binaries/orpheus-desktop-backend/`
  - Sidecar must already exist under `desktop/src-tauri/binaries/`
  - Sidecar is produced by `python scripts/build_desktop_backend.py` as a PyInstaller onedir resource
- Release install: `cd desktop && npm run desktop:release`
  - rebuilds the sidecar
  - builds the `.dmg`
  - replaces `/Applications/Orpheus.app`
  - removes the build-output `.app` so Spotlight only sees the installed app

Desktop auth and API boundary:
- Per-session token is emitted by Python ready JSON.
- `GET /health` remains open for readiness.
- In desktop mode, `/api/*` routes require a bearer token unless a route explicitly accepts a short-lived query token.
- SSE/audio media clients may use query tokens minted by `POST /api/desktop/query-token` on:
  - `GET /api/events/stream?token=...`
  - `GET /api/library/{book_id}/audio?token=...`
- Desktop CORS allows packaged WebView origins matching `http://tauri.localhost`,
  `https://tauri.localhost`, or `tauri://localhost`, plus the Vite development
  origin `http://localhost:5173`.

Desktop process lifecycle contract:
- Normal app quit should stop bundled backend sidecar.
- Forced process kill of the app binary can leave an orphan backend process.
- Current mitigation is explicit manual cleanup during diagnostics.

## Layered Design

1. Interface Layer
- `desktop/src/*` (primary React UI)
- `modules/web/server.py` (FastAPI routes)
- `modules/web/events.py` (SSE broker + stream framing)
- UI must call controller APIs; do not call pipeline internals directly.

2. Application Layer
- `modules/app/controller.py` owns:
  - conversion start/cancel lifecycle
  - runtime validation cache
  - preview engine reuse
  - model/voice/capability exposure for UI
- `modules/app/events.py` defines typed app events.

3. Domain/Processing Layer
- `modules/pipeline/orchestrator.py` coordinates ingest -> chunk -> clean -> synthesize -> encode -> package.
- `modules/pipeline/stages/*` contains stage-focused helpers used by orchestrator composition:
  - `cleaning.py`
  - `synthesis.py`
  - `encoding.py`
  - `packaging.py`
- `modules/ingestion/*`, `modules/tts/*`, `modules/audio/*` implement stage logic.

4. Persistence Layer
- `modules/storage/*` persists books/chapters/jobs.
- Controller mediates persistence access for API consumers.

## Web API Surface

Core routes:
- `GET /health`
- `GET /api/library`
- `GET /api/library/{book_id}`
- `GET /api/library/{book_id}/chapters/{chapter_number}/cleaning-preview`
- `PATCH /api/library/{book_id}`
- `PUT /api/library/{book_id}/playback-position`
- `POST /api/library/{book_id}/open-folder`
- `DELETE /api/library/{book_id}`
- `GET /api/library/{book_id}/audio`
- `HEAD /api/library/{book_id}/audio`
- `GET /api/maintenance/diagnostics`
- `POST /api/maintenance/backup`
- `POST /api/maintenance/cleanup`
- `GET /api/options/models`
- `GET /api/options/voices`
- `POST /api/options/openai/models`
- `POST /api/options/parallel/warmup`
- `GET /api/system/capabilities`
- `POST /api/voices/preview`
- `POST /api/voices/suggest`
- `POST /api/sources/preflight`
- `POST /api/uploads/source`
- `POST /api/uploads/text`
- `GET /api/events/stream`
- `POST /api/desktop/query-token`
- `POST /api/conversions`
- `GET /api/conversions/active`
- `POST /api/conversions/repair/{book_id}`
- `POST /api/conversions/{job_id}/cancel`

`GET /api/options/voices` accepts an optional `engine=<id>` query parameter.

SSE payload contract now includes additive envelope fields:
- `event_id`
- `timestamp`
while preserving event-specific fields (`event_type`, `state`, `progress`, etc.).

## TTS Plugin Contract

- Base contract: `modules/tts/strategies/base.py`
  - `TTSStrategy`
  - `TTSVoice`
  - `TTSCapabilities`
- Factory registry: `modules/tts/factory.py`
- Current default engine: `kokoro`
- Kokoro is the only supported app TTS engine.

## Internal Additive Contracts

- `modules.audio.packager.M4BPackager.create_m4b(...)` accepts optional
  `chapter_durations_ms: list[int] | None = None`.
  - When provided, packaging uses supplied durations and skips per-chapter probe calls.
  - The historical `mp3_files` parameter is now format-neutral: normal pipeline
    packaging passes synthesized WAV masters when available, falling back to MP3
    chapter artifacts only for legacy/incomplete-master paths.
- `modules.pipeline.orchestrator.ChapterResult.wav_path` is the lossless chapter
  master path used by final M4A/M4B packaging. MP3 chapter files remain generated
  as compatibility artifacts and for chapter-level playback fallback.
- `modules.tts.cleaner.TextCleaner.clean_batch_with_trace_async(...)` is a native async
  API-cleaner path that returns full trace objects.
  - `clean_batch_async(...)` remains as a compatibility wrapper and returns cleaned strings.
- `desktop/src/*` hosts the primary UI. `modules/web/*` is now API-only backend
  surface for that desktop app.

## Performance Model

- Runtime preflight validation is cached per `TTSRuntimeSpec.cache_key()`.
- Preview engine is reused across requests for matching runtime keys.
- TTS batch synthesis reuses executor pools and uses VRAM-pressure adaptive pauses.
- API cleaner batch path is native async with bounded concurrency (`api_batch_concurrency`)
  and trace return support.
- Final M4A/M4B outputs encode once from PCM/WAV chapter masters when those masters
  are present, avoiding MP3-to-AAC transcoding in normal conversions.
- Packaging can reuse known chapter durations from pipeline results to avoid redundant probe I/O.
- Runtime counters are exposed through `system capabilities` and TTS engine metrics.

## Boundaries and Rules

- Keep UI state/interaction logic in `desktop/src/*`, not in controller/pipeline internals.
- Keep storage logic out of web modules.
- Keep heavy ML imports lazy where possible.
- Prefer additive, backward-compatible API changes.
- Treat `data/`, `output/`, and `temp/` as local runtime state rather than source-controlled architecture artifacts.
- Treat library source-file existence as the truth signal for stale-entry cleanup during library loads.

## Test Contract

- Python/core regression gate: `python run_tests.py --contract`
- Contract command: `python -m pytest tests -m "not property and not benchmark and not slow and not ui"`
- Excluded from that default contract: property, benchmark, slow, and UI tests.
- Also outside that command: desktop TypeScript build, Tauri/Rust checks, packaged
  sidecar validation, release install, and installed-app user-flow smoke.
- `python run_tests.py` is smoke-only and must not be treated as merge, CI, or
  release readiness.

## Verification Matrix

For non-trivial changes:
1. Update tests near changed behavior.
2. Run `python run_tests.py --contract` for the Python/core regression gate.
3. Run focused web checks:
   - `python -m pytest tests/test_web_api.py tests/test_web_events.py tests/test_web_desktop_auth.py tests/test_desktop_backend_launcher.py tests/test_desktop_runtime_contract.py -q -o addopts=`
4. Optionally run `python run_tests.py` for a faster local smoke pass while iterating.
5. For desktop or release confidence, additionally run:
   - `cd desktop && npm run typecheck && npm run build`
   - `cd desktop/src-tauri && cargo test && cargo check`
   - `python scripts/build_desktop_backend.py && cd desktop && npm run desktop:build`
   - `cd desktop && npm run desktop:install`, followed by installed-app smoke
6. Update docs if architecture boundaries changed.
