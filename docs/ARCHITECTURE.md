# Architecture

Orpheus is a desktop app with three parts:

- a **Tauri shell** in Rust (`desktop/src-tauri/`) that owns the window, app
  paths, and the backend process;
- a **React UI** in TypeScript (`desktop/src/`);
- a **Python backend** (`apps/`, `modules/`) that serves a local FastAPI API
  and owns conversion and storage, including the SQLite library.

This document describes how those parts fit together, the contracts between
them, and where to make common changes.

## Contents

- [Where to change X](#where-to-change-x)
- [Code map](#code-map)
- [Desktop runtime](#desktop-runtime)
- [Request flow](#request-flow)
- [Web API surface](#web-api-surface)
- [Conversion pipeline](#conversion-pipeline)
- [Jobs and concurrency](#jobs-and-concurrency)
- [Library storage](#library-storage)
- [Performance](#performance)
- [Design rules](#design-rules)

## Where to Change X

Check this table before editing. When a change crosses layers, update the
matching types and run the checks in
[TESTING.md](TESTING.md#focused-checks).

| Change | Primary locations | Related contracts |
| --- | --- | --- |
| Parser behavior (PDF, EPUB, Markdown, text) | `modules/ingestion/pdf_parser.py`, `epub_parser.py`, `markdown_parser.py`, `text_parser.py`; `modules/ingestion/preflight.py` | `modules/pipeline/stages/ingestion.py`, `modules/text/document_normalizer.py`; tests `tests/test_*_parser.py` |
| Cleaner policy and backends | `modules/tts/cleaner.py`, `cleaner_config.py`, `cleaner_mlx.py`, `cleaner_cuda.py`, `cleaner_api.py`, `cleaner_backends/` (registry, `basic`) | `modules/pipeline/stages/cleaning.py`; env `CLEANER_BACKEND`; tests `tests/test_cleaner*.py` |
| TTS runtime options | `modules/tts/runtime_spec.py`, `runtime_options.py`, `factory.py`, `strategies/`, `runtimes/` | `modules/pipeline/models.py` (`PipelineConfig`); `modules/app/services/runtime_capability_service.py`; tests `tests/test_tts*.py` |
| Frontend conversion form | `desktop/src/features/conversion/` (`ConversionPanel.tsx`, `ConversionPanelView.tsx`, `conversionPanelModel.ts`, `conversionPanelStateTypes.ts`, `useConversionOptions.ts`, `useConversionSubmitPayload.ts`, `useTtsOptions.ts`, `useCleanerOptions.ts`, `useParallelWorkers.ts`) | Types under `desktop/src/types/`, re-exported by `desktop/src/types/api.ts`; capabilities from `GET /api/system/capabilities` |
| API request and response payloads | `modules/web/routes/conversions.py`, `options.py`, `uploads.py`, `library.py`; `modules/web/serializers.py` | `modules/contracts/conversion_runtime_options.py` (`ConversionRuntimeOptions`); `modules/web/schemas/conversions.py` (HTTP validation); `desktop/src/types/api.ts` and `desktop/src/api/client.ts` |
| Storage schema and library paths | `modules/storage/migrations.py`, `models.py`, `sqlite_repo.py`, `managed_roots.py`, `library_relocation.py`, `maintenance.py` | `modules/app/services/library_service.py`, `conversion_paths.py`; tests `tests/test_storage_*.py`, `tests/test_library_service.py`, `tests/test_managed_storage_relocation.py` |

Keep these cross-layer values aligned:

- **Source suffixes** derive from `modules/contracts/source_extensions.py`. The
  Tauri file picker and the conversion UI copy must match it.
- **Conversion runtime options** flow from `desktop/src/types/api.ts` through
  the FastAPI request schemas to `ConversionRuntimeOptions`
  (`modules/contracts/conversion_runtime_options.py`) and `PipelineConfig`.
- **Progress stage names:** pipeline events emit `PipelineStage` values from
  `modules/pipeline/models.py` (`ingesting`, `synthesizing`, and so on).
  `desktop/src/features/events/progressStageModel.ts` maps them to the
  frontend `ProgressStageName` values (`ingestion`, `synthesis`, and so on).

## Code Map

| Layer | Paths | Responsibility |
| --- | --- | --- |
| Desktop shell | `desktop/src-tauri/src/` | Window and process lifecycle, app paths, setup supervision, packaging |
| Desktop UI | `desktop/src/` (`App.tsx`, `api/client.ts`, `features/*`) | All user-facing state and interaction |
| Launcher | `apps/launchers/desktop_backend.py` | Starts the API on loopback and announces readiness |
| API | `modules/web/` (`server.py`, `routes/`, `schemas/`, `events.py`, `desktop_auth.py`, `source_handles.py`) | HTTP routes, validation, SSE streaming, and desktop auth |
| Application | `modules/app/` (`controller.py`, `config.py`, `events.py`, `services/`) | Composes services for conversion, job lifecycle, library, setup, and capabilities |
| Processing | `modules/pipeline/`, `modules/ingestion/`, `modules/tts/`, `modules/audio/`, `modules/text/` | Ingest, chunk, clean, synthesize, encode, and package |
| Persistence | `modules/storage/` | SQLite schema, repository, managed roots, and maintenance |
| Shared contracts | `modules/contracts/` | Frozen dataclasses and constants shared across layers |
| Runtime setup | `modules/runtime_setup/`, `scripts/setup_runtime.py` | Profile install, model catalog, download receipts, and setup state |

Shared values live in `modules/contracts/`. Conversion options are plain
frozen dataclasses. Source types are re-exported by storage for compatibility.
Book identity owns output-directory naming. HTTP validation belongs in
`modules/web/schemas/`, and cleaner memory estimates belong in `modules/tts/`.
`tests/test_core_import_boundaries.py` stops shared contracts from importing
storage or processing code, and stops storage from importing the pipeline.

## Desktop Runtime

### Startup handshake

1. Tauri launches the Python backend. In development this is
   `python -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0` from
   the checkout's `.venv`; release builds run the bundled sidecar.
2. After binding, Python prints one JSON line on stdout:
   `{"event":"ready","host":"127.0.0.1","port":54321,"token":"...","runtime_profile":"development"}`.
   A packaged sidecar reports its bundled profile instead of `development`.
3. Python also writes the same payload to `backend-ready.json` in the app data
   directory, so the shell can recover it if stdout is lost.
4. The shell passes the endpoint and token to React, which creates an
   authenticated API client. Startup is generation-fenced: after a restart,
   the shell never accepts a stale readiness event.

The shell injects the runtime paths through `AUDIOBOOK_DATA_DIR`,
`AUDIOBOOK_OUTPUT_DIR`, `AUDIOBOOK_TEMP_DIR`, `AUDIOBOOK_DB_PATH`, and
`AUDIOBOOK_DESKTOP_READY_FILE`. See [ENVIRONMENT.md](ENVIRONMENT.md) for every
variable the shell and backend read.

### Development and release modes

- **Development** (`cd desktop && npm run desktop:dev`): Vite serves the UI and
  Rust starts the backend from the repository `.venv`. `PATH` and environment
  variables cannot select a different interpreter.
  `AUDIOBOOK_DESKTOP_USE_SIDECAR=1` makes a debug build use the packaged
  sidecar instead.
- **Packaged** (`cd desktop && npm run desktop:build`): Tauri's
  `beforeBuildCommand` builds a fresh PyInstaller onedir sidecar, then the
  frontend, then the bundle. The sidecar lives under
  `binaries/orpheus-desktop-backend/` in the app resources (on macOS,
  `Contents/Resources/binaries/orpheus-desktop-backend/`).
  `ORPHEUS_RUNTIME_PROFILE=base|mlx|cuda` selects the bundled runtime. When it
  is unset, the previous sidecar's profile is kept; a first build uses the
  target default. Tauri supplies `TAURI_ENV_TARGET_TRIPLE`, and the sidecar
  builder rejects a host/target architecture mismatch before probing
  dependencies.
- **macOS install** (`cd desktop && npm run desktop:release`): builds as above,
  replaces `/Applications/Orpheus.app`, and removes the build-output `.app` so
  Spotlight shows only the installed copy.

Build and installer details are in
[RUN_APPLICATION.md](RUN_APPLICATION.md#packaging).

### Authentication

- Each launch generates a session token and returns it in the ready payload.
- `GET /health` is open so the shell can check readiness.
- In desktop mode, every `/api/*` route requires the bearer token, unless the
  route explicitly accepts a short-lived query token.
- Media and SSE clients that cannot send headers use query tokens minted by
  `POST /api/desktop/query-token` on
  `GET /api/events/stream?token=...` and
  `GET /api/library/{book_id}/audio?token=...`. Audio query tokens are scoped
  to the selected library epoch.
- CORS allows the packaged WebView origins `http://tauri.localhost`,
  `https://tauri.localhost`, and `tauri://localhost`. Debug builds also allow
  the Vite origin `http://localhost:5173`.
- The launcher rejects any request whose `Host` header is not a loopback name
  with `421` and code `invalid_host`, so a DNS-rebound page gets no answer.
- An unexpected server error returns `500` with code `internal_error` inside
  the CORS layer, so the WebView can read it; details stay in the backend log.

### Process lifecycle

- A normal quit terminates the backend process group
  (`desktop/src-tauri/src/backend.rs`).
- If the app dies without running its exit handler, the backend notices that
  its parent (`ORPHEUS_DESKTOP_PARENT_PID`) is gone and shuts down. On Windows,
  a job object ends it.
- Backend stdout and stderr go to `data/logs/backend.log` in the app data
  directory, rotated at 5 MiB to `backend.log.1`.
- Only one app instance runs at a time; a second launch focuses the existing
  window.
- The window opens at 1200×860 and cannot be made smaller than 960×640
  (`desktop/src-tauri/tauri.conf.json`).
- Worker-pool shutdown waits are bounded, including during worker startup.
  Startup probes observe cancellation, and admission stays closed until
  teardown confirms the workers are gone, even if the wait times out.

## Request Flow

1. Sources arrive by file selection, upload, or pasted text. Pasted text is
   queued automatically when you press Start.
2. On boot, the UI reads `GET /api/system/capabilities` and aligns cleaner
   defaults with the real runtime probe before a run starts.
3. `AppController.start_conversion(...)` validates the runtime and starts a
   background job through the conversion service and `JobCoordinator`.
4. `ConversionPipeline.convert(...)` runs ingest, chunk, clean, synthesize,
   encode, and package, then publishes the final audio file atomically.
5. Progress, log, and state events stream over SSE (`/api/events/stream`).
   Every event carries job ownership, so a late event from an old job cannot
   update a newer one. Events also carry `event_id` and `timestamp`.
6. React shows weighted stage progress under a `Chapter k/N · stage`
   headline, and an ETA once it is stable. Cancelling the running book of a
   queue stops that book only; the queue continues with the next one.
7. The library loads 100 books per page. From the library you can play a book,
   open its folder, repair it, or delete it with its files. When the backend
   connection is lost, the books already loaded stay visible.
8. Playback uses tokenized local audio URLs and continues while you browse
   other books. Playback-position saves are scoped to the book, the client
   generation, the library ID, and the library epoch.
9. If the backend restarts during a conversion, the UI reports the job as
   interrupted on reconnect; the finished chapters are kept for repair.

### Ambiguous starts

Desktop starts carry a `client_request_id`, which is kept on the job and
returned in start responses, active-state reads, and conversion events. If a
start response is lost, the desktop reconciles that ID through bounded
active-state reads and the reconnect state; it never re-posts the start. New
starts stay blocked while it checks. If confirmation is still unavailable after
30 seconds, the UI asks for a manual status refresh and retry.

## Web API Surface

Routes are defined in `modules/web/routes/` and
`modules/web/desktop_auth.py`, except `POST /api/desktop/shutdown`, which is
registered in `modules/web/server.py`.

| Area | Routes |
| --- | --- |
| Health | `GET /health` |
| Library | `GET /api/library`, `GET /api/library/count`, `GET /api/library/{book_id}`, `PATCH /api/library/{book_id}`, `DELETE /api/library/{book_id}`, `GET /api/library/{book_id}/cover`, `GET`/`HEAD /api/library/{book_id}/audio`, `PUT /api/library/{book_id}/playback-position`, `POST /api/library/{book_id}/open-folder`, `GET /api/library/{book_id}/chapters/{chapter_number}/cleaning-preview` |
| Conversions | `POST /api/conversions`, `GET /api/conversions/active`, `POST /api/conversions/repair/{book_id}`, `POST /api/conversions/{job_id}/cancel` |
| Sources and uploads | `POST /api/sources/preflight`, `POST /api/uploads/source`, `POST /api/uploads/text`, `DELETE /api/uploads/{handle}` |
| Options and voices | `GET /api/options/models`, `GET /api/options/voices` (optional `engine=<id>`), `POST /api/options/openai/models`, `POST /api/voices/preview`, `POST /api/voices/suggest` |
| System | `GET /api/system/capabilities`, `GET /api/system/storage`, `POST /api/system/storage/{folder}/open` |
| Setup and models | `GET /api/setup/status`, `POST /api/setup/adopt`, `GET /api/setup/models`, `DELETE /api/setup/models/{group_id}`, `GET`/`POST /api/setup/models/download`, `POST /api/setup/models/download/cancel`, `DELETE /api/setup/runtime` |
| Maintenance | `GET /api/maintenance/diagnostics`, `POST /api/maintenance/backup`, `GET /api/maintenance/backups`, `POST /api/maintenance/backups/{backup_id}/restore`, `POST /api/maintenance/cleanup`, `POST /api/maintenance/delete-staging/open-folder` |
| Events | `GET /api/events/stream` |
| Desktop | `POST /api/desktop/query-token`, `POST /api/desktop/shutdown` |

`GET /api/library/{book_id}/cover` returns the EPUB cover or a PDF first-page
thumbnail, or `204` when the source has none. Prefer additive,
backward-compatible changes to these payloads.

Payload details the UI depends on:

- **Preflight** (`POST /api/sources/preflight`) returns
  `estimated_audio_seconds` and `blocking_error`. `blocking_error` is `null`,
  or `{code, message, details?}` when the source cannot be converted at all.
  Its codes are `source_empty`, `source_unreadable`, `pdf_password_protected`,
  `pdf_damaged`, `epub_damaged`, and `epub_drm_protected`
  (`modules/ingestion/preflight.py`, `source_problems.py`). Preflight and
  upload responses also list `existing_books`, the library books already made
  from the same source; converting again stays allowed.
- **Books** carry `completed_chapters` and `audio_available`
  (`modules/web/serializers.py`).
- **Models** (`GET /api/setup/models`): each entry has `deletable` and
  `delete_blocked`, which is `null` or the `{code, message}` that a delete
  request would return now.
- **Storage** (`GET /api/system/storage`) returns `output_path`,
  `app_data_path`, and `log_path`; `log_path` is `null` when no backend log
  file exists.
- **Diagnostics** (`GET /api/maintenance/diagnostics`) includes
  `cleanup_preview`, the same per-category summary that cleanup reports, and
  `checked_at`. The preview lists `removable_books`: books with no audio and no
  source file whose library entry a cleanup would remove. The cleanup report
  names them again as `removed_books`.
- **Backups** (`GET /api/maintenance/backups`): each item has a `kind`
  (`backup` or `safety_copy`) and a display `label`.
- **Restore** returns `restored_id`, `safety_copy_path`, and a `summary` with
  `books_restored`, `missing_audio`, `missing_source`, and
  `unowned_output_folders`.
- **Cleanup** (`POST /api/maintenance/cleanup`) takes `confirm` and an optional
  `remove_unowned_folders` list of folder names (at most 100). The report has
  a `summary` (`changed`, `removable`, `removed`, `kept`); when folders were
  named, it also has `removed_unowned_folders` and `unknown_unowned_folders`.

Limits and errors:

- Titles and authors accept at most 200 characters and tags 500
  (`modules/contracts/limits.py`).
- A JSON request body over 1 MiB is refused with `413`. The two upload routes
  have their own limit, 500 MiB (`modules/web/routes/uploads.py`).
- A `422` validation error lists each problem's location and message and never
  echoes the submitted value, which could be an API key.
- Typed errors use `{"detail": {"code", "message", ...}}`, including the `500`
  `internal_error` and `421` `invalid_host` responses described under
  [Authentication](#authentication).

## Conversion Pipeline

`modules/pipeline/orchestrator.py` coordinates ingest, chunk, clean,
synthesize, encode, and package. Per-chapter work is split across:

- `chapter_dispatch.py`: sequential or parallel routing;
- `chapter_processor.py`: the per-chapter state machine;
- `chapter_narration.py`: the spoken chapter announcement and pauses;
- `model_lifecycle.py`: engine and cleaner reuse;
- `pool_manager.py` and `worker_*.py`: process-pool workers;
- `memory_policy.py`: memory limits per worker.

Stage helpers live in `modules/pipeline/stages/`: `ingestion.py`,
`cleaning.py`, `synthesis.py`, `encoding.py`, and `packaging.py`. Audiobook
mode packages an M4B with chapter markers; Document mode packages a single M4A
(`modules/audio/document_packager.py`).

Behavior worth knowing before changing a stage:

- **Oversized text is split, not rejected.** A chapter over the word cap is
  split at paragraph boundaries, then sentences, then line breaks; text with
  none of these is cut into word windows
  (`modules/text/document_normalizer.py`).
- **Chapter pauses.** Each chapter is the spoken announcement, a 1.5-second
  pause, the body, and a 1.5-second pause
  (`CHAPTER_HEADING_PAUSE_SECONDS` and `CHAPTER_END_PAUSE_SECONDS` in
  `chapter_narration.py`).
- **Loudness and peaks.** Encoding applies the gain that brings each chapter
  to the loudness target (`target_dbfs`, default -16), followed by a lookahead
  limiter that holds sample peaks at -2 dBFS (`PEAK_LIMIT_DBFS` in
  `stages/encoding.py`). The limiter acts only on peaks, so the decoded AAC
  stays below about -1 dBTP.
- **Tags and cover.** The M4B carries title, artist (the author, omitted when
  there is none), album, genre, composer (the voice's display name), and a
  comment. It embeds the source's cover when there is one: the EPUB cover or
  the first PDF page (`stages/packaging.py`, `modules/audio/packager.py`).
- **Parallel workers.** The requested worker count is an upper bound. The
  worker plan and the memory policy may lower it, and the conversion form
  shows the recommended and the highest safe count for the machine.

### TTS engines

- Base contract: `modules/tts/strategies/base.py` (`TTSStrategy`, `TTSVoice`,
  `TTSCapabilities`).
- Registry: `modules/tts/factory.py`. Kokoro is the only supported engine and
  the default.
- Provider runtimes live in `modules/tts/runtimes/`. `kokoro_mlx.py` wraps the
  MLX `engine.py` facade; `kokoro_package.py` adapts the upstream
  `kokoro.KPipeline` for CUDA and CPU.
- How a provider is chosen per platform is described in
  [RUNTIME_SELECTION.md](RUNTIME_SELECTION.md).

### Text cleaner

- Facade: `modules/tts/cleaner.py` (`TextCleaner`).
- Backend protocol: `modules/tts/cleaner_backends/base.py` (`CleanerBackend`).
- Registry: `modules/tts/cleaner_backends/registry.py`
  (`register_cleaner_backend`, `get_cleaner_backend`,
  `validate_cleaner_backend`, `is_local_cleaner_backend`,
  `requires_api_key_cleaner_backend`).
- Registered backends:
  - `basic`: rule-based cleanup with no model
    (`modules/tts/cleaner_backends/basic.py`);
  - `mlx`: local MLX-LM on Apple Silicon (`modules/tts/cleaner_mlx.py`);
  - `cuda`: local Hugging Face and PyTorch on NVIDIA GPUs
    (`modules/tts/cleaner_cuda.py`);
  - `api`: an OpenAI-compatible remote API (`modules/tts/cleaner_api.py`).
- The backend comes from `CleanerConfig.backend` or `CLEANER_BACKEND`, and
  saved setup can replace it.
- Local backends (`mlx`, `cuda`) clean each chunk synchronously and cache the
  model between chunks. The API backend batches asynchronously with bounded
  concurrency (`api_batch_concurrency`) and can return traces.
- When Kokoro and a local cleaner both use the GPU, worker planning forces a
  single worker. When both use CUDA on the same device,
  `CombinedDeviceMemoryPolicy` (`modules/tts/device_memory.py`) also merges
  VRAM sampling and worker estimates for the pair.

### Internal contracts

- `M4BPackager.create_m4b(...)` (`modules/audio/packager.py`) accepts an
  optional `chapter_durations_ms: list[int] | None`. When provided, packaging
  uses those durations and skips per-chapter probes. Despite its name, the
  `mp3_files` parameter is format-neutral.
- `select_packaging_audio_inputs` (`modules/pipeline/result_validation.py`)
  chooses packaging inputs: per-chapter AAC when every chapter has it, then
  WAV masters, then MP3, only when every chapter has that format. Audiobook
  packaging stream-copies AAC chapters into the M4B; Document mode re-encodes
  in one pass so section seams stay gapless.
- `ChapterResult` (`modules/pipeline/models.py`) carries `aac_path`,
  `wav_path`, and `mp3_path`. Chapters are encoded to AAC while synthesis
  continues (`enable_chapter_aac_packaging`, on by default). Per-chapter MP3s
  are written only with `AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS`, or when
  repairing a book whose existing chapters are MP3s, so the repaired chapters
  match (`RepairRunCommand` in `modules/app/services/conversion_commands.py`);
  books from older versions may still have them. In AAC-only runs, `mp3_path`
  and `chapters.mp3_path` hold the chapter `.m4a`.
- `TextCleaner.clean_batch_with_trace_async(...)` is the native async API path
  that returns full trace objects. `clean_batch_async(...)` remains as a
  wrapper that returns cleaned strings.

## Jobs and Concurrency

`JobCoordinator` owns job admission, cancellation, terminal state, and
maintenance reservations.

- When both locks are needed, the conversion job lock is taken before the
  repository lock, never the reverse.
- Repair and book deletion reserve through `JobCoordinator` before taking a
  repository library snapshot. Restore excludes these reservations, and repair
  checks the repository generation again at launch.
- Book deletion reserves the managed files and output directories it may
  remove. Conversion admission checks those paths, and deletion checks the
  active conversion's input, including pending jobs without a book ID. This
  also protects generated Markdown chosen for reconversion from an older
  book's output.
- Restore, backup, model deletion, storage cleanup, and book or upload cleanup
  are refused while a cancelled job's worker pool is still shutting down. A
  terminal job status alone does not reopen admission; worker shutdown must be
  confirmed.

## Library Storage

### Identity and managed roots

SQLite `library_metadata` stores `library_id` and `library_epoch`. Library
snapshots hold the repository lock across identity checks and the related
operations. Desktop mutations carry the expected epoch, and audio authorization
and playback checkpoints are scoped to the library identity. Restore repairs
missing identity in private staging and always rotates the epoch before
publishing. It keeps a valid backup ID, or the live ID when the backup has none.

Managed source, output, and temporary roots each carry
`.orpheus-storage-owner.json` with the owning database path and `library_id`.
You can move the whole library folder or rename its database. On the first
open after a move, absolute paths under the old managed roots, and pending
deletion inventories, are rebased before the ownership markers are refreshed.
Paths outside those roots stay unchanged. Markers keep root history. Backups
record their layout in the `managed_roots` entry of `library_metadata`, and
restore remaps from that layout into the current roots; older backups without
a layout use marker history. Case-only aliases on case-insensitive volumes
identify the same library. Hard-linked database names are refused as
managed-storage aliases.

Two sequences are unsupported: moving again after an interrupted adoption, and
moving right after a failed post-restore marker refresh. In either case, open
the library in place first to complete the transition.

### Reconciliation and cleanup

Maintenance moves referenced uploads into durable `data/sources` storage. It
syncs the copy, records its SHA-256 digest in
`data/sources/.orpheus-source-relocations.json`, and commits every owner's new
pointer before deleting the upload. Restore and cleanup use this history to
repair missing source references only when the destination digest matches.
Cleanup never deletes a durable source that a missing reference could still
belong to.

The startup deletion sweep and explicit cleanup share one staging policy under
a repository library snapshot. Each artifact is decided on its own:

- bytes referenced at their original paths are restored;
- bytes referenced at staged paths are kept, and staged references win over
  original-path references;
- unreferenced committed artifacts are removed;
- ambiguous transactions are preserved, and an uncommitted transaction with a
  staged reference is kept whole.

Pruning stale rows and artifacts happens only in explicit maintenance, after a
backup. Loading the library never prunes it. Cleanup reports, per category,
what it removed and what it kept and why; when nothing changed it keeps no
backup. Its reports are written to `reports/` in the data directory.

If a corrupt database was quarantined (`audiobooks.db.quarantine.json` beside
it), the empty replacement references none of the old library's files, so they
may be the only copy. Until a backup is restored, cleanup keeps orphan durable
sources, orphan output folders, and stale uploads, and reports them as kept
under `quarantined_library_files` (`modules/storage/maintenance.py`).
Restoring a backup removes the status file.

Restore never deletes files. It reports the restored books whose audio or
source is missing on disk and the output folders that no restored book owns.
Such a folder is removed only when a cleanup request names it, through the
same staged deletion as a book.

### Interrupted conversion recovery

Application-owned conversions register an incomplete library book before
synthesis. `modules/pipeline/recovery.py` keeps the original source in the
reserved output directory's `.orpheus-source/` and writes
`.orpheus-recovery.json` with ordered chapter identities and intended audio
filenames. The application service saves the conversion options and reserves
the book under the job lock. The recovery source and inventory are durably
published, and the book is committed, before the intermediate upload is
deleted.

Missing chapter paths stay `NULL` in SQLite. An atomically published chapter
file is also a recovery checkpoint if the process exits before SQLite is
updated. Repair reads the inventory, validates retained audio, restores missing
durations, and regenerates missing or unreadable chapters; success updates the
same book. Library deletion and storage maintenance use the durable source to
find the exact reserved directory, even when titles repeat.

Keep these contracts covered in `tests/test_conversion_recovery.py` and
`tests/test_repair.py` when changing synthesis, naming, cleanup, or
persistence.

## Performance

- Runtime preflight validation is cached per `TTSRuntimeSpec.cache_key()`, and
  the voice-preview engine is reused across requests with the same runtime key.
- Chapters are encoded to AAC while synthesis continues, and the M4B
  stream-copies them, so audiobook packaging does not re-encode audio.
- Packaging reuses known chapter durations to avoid extra probe I/O.
- Runtime counters are exposed through system capabilities and TTS engine
  metrics.
- Desktop library pages, counts, and book details have 15-second request
  deadlines; diagnostics, backup lists, and cleaning previews have 30-second
  deadlines. A timeout releases the loading state for retry, and a failed count
  keeps the fetched page and the last known total.

## Design Rules

- Keep UI state and interaction logic in `desktop/src/`. The UI calls the API;
  it never reaches into controller or pipeline internals.
- Keep storage logic out of `modules/web/`.
- Keep heavy ML imports lazy.
- Prefer additive, backward-compatible API changes.
- Treat `data/`, `output/`, and `temp/` as local runtime state, never as
  source.
- Before removing records or files, cleanup must consider playback, chapters,
  recovery inventories, and possible relocated sources.

For the test tiers and the checks to run after a change, see
[TESTING.md](TESTING.md).
