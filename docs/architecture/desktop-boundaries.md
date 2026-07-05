# Desktop and Frontend Boundaries

Inventory for Workstream 1 of `docs/CODEBASE_CLEANUP_PLAN.md`. Covers
`desktop/src`, `desktop/src-tauri`, and how the React shell talks to the Python
backend. No production code was changed.

## High-Level Shape

```
┌─────────────────────────────────────────────────────────────┐
│  desktop/src (React + TypeScript + Vitest)                  │
│  - UI features, hooks, api/client.ts                        │
│  - All business operations via HTTP JSON API                │
└──────────────────────────┬──────────────────────────────────┘
                           │ fetch + Bearer / query token
┌──────────────────────────▼──────────────────────────────────┐
│  Python backend (apps/launchers/desktop_backend.py)         │
│  → modules.web.server.create_app()                          │
└─────────────────────────────────────────────────────────────┘
         ▲
         │ spawn dev python or sidecar binary
┌────────┴────────────────────────────────────────────────────┐
│  desktop/src-tauri (Rust)                                   │
│  - Process lifecycle, file picker, open app data folder     │
└─────────────────────────────────────────────────────────────┘
```

The desktop shell is intentionally thin: Tauri does not implement conversion
logic. It launches the backend, exposes a few OS integrations, and the frontend
drives everything through `ApiClient`.

---

## `desktop/src-tauri`

**Ownership:** Backend process lifecycle, loopback-only binding contract, sidecar
path resolution, minimal native commands.

### Public entry points (Tauri commands)

| Command | File | Purpose |
|---------|------|---------|
| `backend_status` | `main.rs` | Poll lifecycle + error from `BackendManager` |
| `backend_endpoint` | `main.rs` | Return `{ host, port, token, base_url }` when ready |
| `choose_source_files` | `main.rs` | Native multi-select file dialog |
| `open_app_data_folder` | `main.rs` | Reveal app data dir in OS file manager |

Registered in `invoke_handler` in `main.rs`. No other `#[tauri::command]` handlers exist.

### Private helpers

- `backend.rs` — `BackendManager`, `RunningBackend`, dev vs sidecar launch configs,
  ready-payload JSON parsing from stdout or ready file
- `paths.rs` — `DesktopRuntimePaths`, env vars for DB/output/temp roots
- `backend_tests.rs` — Rust unit tests (sidecar naming, ready payload)

### Backend launch paths

| Mode | Trigger | Command |
|------|---------|---------|
| Dev Python | Debug build, `AUDIOBOOK_DESKTOP_USE_SIDECAR` unset | `python -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0` |
| Sidecar | Release or `AUDIOBOOK_DESKTOP_USE_SIDECAR=1` | `orpheus-desktop-backend` binary from `src-tauri/binaries/` |

Sidecar build is owned by `scripts/build_desktop_backend.py` (see tooling doc).

### Cleanup candidates

- `backend.rs` (~640 lines) — process shutdown + sidecar path candidate list is
  dense; extract path resolution table from signal handling.
- File dialog filter in `choose_source_files` hardcodes extensions — must stay in
  sync with backend upload/preflight suffix lists (drift risk; see below).
- `project_root_dir()` in `backend.rs` assumes repo layout (`src-tauri` → `desktop` → repo);
  fragile if directory layout changes.

---

## `desktop/src` — API Layer

**Ownership:** Typed HTTP client, shared API types, backend connection bootstrap.

### Public entry points

| Symbol | Location | Role |
|--------|----------|------|
| `createApiClient`, `createApiClientFromDesktopEndpoint` | `api/client.ts` | Factory for all REST calls |
| `ApiClient` interface | `api/client.ts` | Complete backend surface used by UI |
| Type definitions | `types/api.ts` | Request/response shapes (~514 lines) |

### Connection bootstrap

`App.tsx` + `features/dashboard/dashboardHelpers.ts`:

1. `invoke("backend_status")` / `invoke("backend_endpoint")` poll until ready
2. `createApiClientFromDesktopEndpoint(endpoint)` with bearer token
3. Periodic `/health` checks (`BACKEND_HEALTH_CHECK_INTERVAL_MS = 2000`)

### Private helpers

- `resolveUrl`, `createHeaders`, `normalizeErrorText` in `client.ts`
- `isTauriRuntime()`, `waitForDesktopEndpoint()` in `dashboardHelpers.ts`

### Cleanup candidates

- `api/client.ts` (~460 lines) — monolithic; split by domain (library, conversion,
  maintenance) if it grows past ~500 lines again.
- `types/api.ts` duplicates backend Pydantic models manually — Workstream 2 should
  generate or centrally document field names.

---

## `desktop/src` — Feature Modules

### `features/conversion/` (largest feature area, ~3950 lines total)

**Ownership:** Conversion form, option state, payload construction, queue, preflight.

| File | Lines | Role |
|------|-------|------|
| `ConversionPanel.tsx` | — | Container wiring hooks |
| `ConversionPanelView.tsx` | 707 | Presentational UI (**largest component**) |
| `useConversionSourceQueue.ts` | 420 | Drag/drop, Tauri file picker, paste upload |
| `useCleanerOptions.ts` | 341 | Cleaner backend/model/API key state |
| `useTtsOptions.ts` | 275 | Engine, voice, quantization |
| `useConversionOptions.ts` | 192 | Aggregates option hooks |
| `useConversionSubmitPayload.ts` | 76 | Builds `StartConversionRequest` |
| `cleanerBackendHelpers.ts` | 245 | Normalizes API cleaner options → payload fields |
| `conversionOptionsShared.ts` | 117 | localStorage settings persistence |

**Public hooks exported to panel:** `useConversionJob`, `useConversionPanelState`,
`useConversionQueueRunner`, `useParallelWorkers`, etc.

**Private helpers:** `conversionPanelHelpers.ts`, `conversionPanelModel.ts`,
`conversionPanelStateTypes.ts`, `TermSelect.tsx`.

### Other features

| Directory | Ownership |
|-----------|-----------|
| `features/library/` | Library list, book detail, playback hooks |
| `features/player/` | Audio element, analyser, playback position sync |
| `features/progress/` | SSE-driven progress display |
| `features/events/` | EventSource transport, terminal state waiters |
| `features/logs/` | Log panel from SSE |
| `features/dashboard/` | Backend status, clock, waveform canvas |

### `navigation/tabs.tsx`

Dashboard tab config; maps tabs to conversion sub-panels.

### Cleanup candidates

- **`ConversionPanelView.tsx` (707 lines)** — split by section (source queue,
  options, actions) before adding more UI.
- **`useConversionSourceQueue.ts` (420 lines)** — mixes Tauri drag/drop, API
  preflight, and queue state; extract transport-specific code.
- Multiple hooks duplicate default resolution logic also present in
  `useTtsOptions` / `useCleanerOptions` / backend capabilities endpoint.

---

## Tauri vs API Responsibility Split

| Concern | Layer | Notes |
|---------|-------|-------|
| Start/stop Python API | Tauri `AppState::start_backend` | Frontend never starts backend directly |
| Conversion, library, repair | `ApiClient` HTTP | |
| File picker (native) | Tauri `choose_source_files` | Returns absolute paths; frontend then calls `preflightSource` |
| Drag-and-drop files | Frontend listens to Tauri `tauri://drag-drop` | `useConversionSourceQueue.ts` |
| Paste text upload | `ApiClient.uploadSourceText` | No Tauri involvement |
| Open app data folder | Tauri `open_app_data_folder` | |
| Audio playback URL | `ApiClient.getLibraryAudioUrl` | Adds desktop query token for auth |
| SSE event stream | `features/events/useEventStreamTransport.ts` | Query token via `createDesktopQueryToken` |

---

## Duplicated Frontend/Backend Assumptions (Drift Risks)

These constants or shapes exist in more than one layer without a single source of truth.

### Source file extensions

| Location | Allowed extensions |
|----------|-------------------|
| `src-tauri/main.rs` `choose_source_files` | `pdf`, `epub`, `txt`, `md`, `markdown` |
| `modules/app/services/library_service.py` | `.pdf`, `.epub`, `.txt`, `.md` |
| `modules/ingestion/preflight.py` | `.pdf`, `.epub`, `.txt`, `.md` |
| `ConversionPanelView.tsx` UI copy | document mode: all four; audiobook mode: `.pdf` or `.epub` only |
| `tools/feedback_loop_runner.py` | `.pdf`, `.epub` only |

**Risk:** Tauri allows `markdown` extension alias; backend normalizes to `.md`.
Audiobook mode UI restricts formats but API does not enforce mode-specific suffixes
at upload time.

### Upload size limit

| Location | Value |
|----------|-------|
| `library_service.MAX_UPLOAD_BYTES` | `500 * 1024 * 1024` |
| `web/routes/uploads.py` | imports `MAX_UPLOAD_BYTES` from library service (good) |
| Frontend | No client-side size check before upload |

### Conversion runtime payload

Three parallel definitions of the same option bag:

| Layer | Type |
|-------|------|
| Frontend | `ConversionRuntimeOptions`, `StartConversionRequest` in `types/api.ts` |
| API | `ConversionRuntimeOptionsRequest` in `modules/web/routes/conversions.py` |
| App | `ConversionRuntimeOptions` dataclass in `modules/app/services/conversion_runtime.py` |
| Pipeline | Fields folded into `PipelineConfig` in `modules/pipeline/models.py` |

Notable mismatches to track:

- API route accepts `tts_provider`, `tts_device` at top level; frontend sends
  `tts_options: { quantization }` from `useConversionSubmitPayload.ts` but not
  provider/device (backend fills via `resolve_tts_runtime`).
- API supports nested `runtime_options` override with `model_fields_set` merge logic;
  frontend always sends flat `runtime_options` object.
- Default voice `am_adam` in API Pydantic models and `ConversionRuntimeOptions`.

### Cleaner backend metadata

| Layer | Behavior |
|-------|----------|
| Backend | `list_cleaner_backend_options()` → `cleaner_backend_options` array |
| Frontend | `cleanerBackendHelpers.resolveCleanerBackendOptions` maps `name` → `id` |
| Frontend | `buildCleanerRuntimePayloadFields` branches on `is_local`, `requires_api_key`, CUDA fields |

CUDA field presence is inferred on the frontend via `default_device` or
`supported_quantizations` (`cleanerBackendUsesCudaFields`) — must match backend
registry metadata.

### Runtime capabilities / defaults

| Field | Backend source | Frontend consumer |
|-------|----------------|-------------------|
| `default_tts_engine` | `AppController.get_system_capabilities()` reads `self.config.tts_engine` directly | `useTtsOptions`, `conversionOptionsShared` |
| `default_tts_quantization` | `AppController.get_system_capabilities()` reads `self.config.tts_quantization` directly | `buildQuantizationOptions` adds `bf16` fallback if empty |
| `cleaner_runtime_availability` | Runtime capability checks assembled by controller | `deriveCleanerRuntimeStatus` |
| `parallel_worker_recommendation` | Controller capability response | `useParallelWorkers` |

Frontend adds **`bf16` quantization fallback** when API returns no values —
backend may disagree with this default.

### Progress stage names

Backend progress emits `PipelineStage` values from `modules/pipeline/models.py`
(`ingesting`, `synthesizing`, etc.). Frontend `ProgressStageName` remains
`ingestion | chunking | cleaning | synthesis | encoding | packaging`; the explicit
mapping lives in `desktop/src/features/events/progressStageModel.ts`.

### OpenAI models URL

`api/client.ts` no longer sends a models URL. It posts only the pasted API key to
`/api/options/openai/models`; the backend route always calls
`fetch_openai_model_ids(..., base_url=DEFAULT_API_BASE_URL)` from
`modules/tts/cleaner_config.py`.

---

## Test Boundaries

| Area | Location |
|------|----------|
| API client + App helpers | `desktop/src/App.test.ts` |
| Cleaner helpers | `desktop/src/features/conversion/cleanerBackendHelpers.test.ts` |
| Event stream model | `desktop/scripts/test-event-stream-model.mjs` |
| Tauri backend contract | `desktop/src-tauri/src/backend_tests.rs` |
| Python desktop contract | `tests/test_desktop_runtime_contract.py`, `test_desktop_backend_launcher.py` |

Pre-push hook runs `npm run typecheck`, `npm test -- --run`, and `cargo test` in
`desktop/src-tauri` (see tooling doc).

---

## Cleanup Priorities (ordered)

1. Centralize conversion option schema (frontend types ↔ FastAPI ↔ dataclass ↔
   `PipelineConfig`) — highest drift risk.
2. Single `SUPPORTED_SOURCE_SUFFIXES` constant consumed by Tauri (codegen or
   shared JSON), Python, and UI copy.
3. Split `ConversionPanelView.tsx` before further UI work.
4. Document or enforce audiobook-vs-document source extension rules at API layer,
   not only in UI text.
5. Keep Tauri command surface minimal — resist adding business logic commands.
