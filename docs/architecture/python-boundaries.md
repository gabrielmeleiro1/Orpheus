# Python Package Boundaries

Inventory for Workstream 1 of `docs/CODEBASE_CLEANUP_PLAN.md`. Generated from
static import analysis (`ast` walk of `modules/**/*.py`) plus spot reads of
entry points. No production code was changed.

## Layered Dependency Map

Intended flow (top → bottom):

```
web → app → pipeline → {ingestion, tts, audio}
                  ↘ storage
```

Cross-cutting root modules (not under the seven named packages):

| Module | Role | Imported by |
|--------|------|-------------|
| `modules/errors.py` | Shared exceptions, filename sanitization | `ingestion`, `tts`, `app` |
| `modules/async_bridge.py` | Sync/async bridge for cleaner paths | `tts`, `pipeline` |
| `modules/openai_compat.py` | OpenAI HTTP helpers | `tts.cleaner_api`, `app.voice_recommender` |
| `modules/text/document_normalizer.py` | Post-parse normalization/filtering | `pipeline.stages.ingestion`, `scripts/benchmark_nagel_pipeline.py` |

`modules/text` is not listed in the cleanup plan but is a real boundary between
raw parser output and pipeline ingestion. Treat it as part of the ingestion
story or extract it into `modules/ingestion` in a later pass.

### Package → Package edges (from `from modules.X` imports)

| Source | Imports |
|--------|---------|
| `app` | `app`, `errors`, `ingestion`, `openai_compat`, `pipeline`, `storage`, `tts` |
| `web` | `app`, `pipeline`, `tts`, `web` |
| `pipeline` | `async_bridge`, `audio`, `ingestion`, `pipeline`, `text`, `tts` |
| `tts` | `async_bridge`, `audio`, `errors`, `openai_compat`, `pipeline`, `tts` |
| `ingestion` | `errors` |
| `audio` | `audio` (internal only) |
| `storage` | `storage` (internal only) |
| `text` | `ingestion` |

### Cycles and Near-Cycles

| Relationship | Mechanism | Risk |
|--------------|-----------|------|
| **`pipeline` ↔ `tts`** | `pipeline` imports `tts` at module level (`models.py`, stages, workers). `tts.device_memory` lazily imports `pipeline.cleaner_memory.estimate_cleaner_cuda_bytes` inside a function (line ~481). | Near-cycle. Memory estimation logic is split across packages; changes to either side can break worker sizing silently. |
| **`app` → `pipeline` → `tts` → (lazy) `pipeline`** | App builds `PipelineConfig` from `ConversionRuntimeOptions`; pipeline workers deserialize TTS/cleaner state. | Acceptable orchestration coupling today, but `PipelineConfig` and `ConversionRuntimeOptions` duplicate field sets (drift risk). |
| **Internal self-imports** | `app`, `pipeline`, `tts`, `web`, `audio`, `storage` use lazy `__getattr__` re-exports that import their own submodules. | Not true cycles; keep lazy exports when splitting modules. |

No hard import cycle was found among `ingestion`, `audio`, or `storage`.

---

## `modules/app`

**Ownership:** Application façade, job lifecycle, library CRUD, runtime capability
probes, voice recommendation orchestration. Does not parse documents or run TTS
directly.

### Public entry points

| Symbol | Location | Consumers |
|--------|----------|-----------|
| `AppController` | `controller.py` | `modules/web`, tests, desktop via API |
| `AppConfig` | `config.py` | Controller, services |
| `ConversionRuntimeOptions` | `services/conversion_runtime.py` (re-exported via `services/conversion_service.py` and `app.__init__`) | Web routes, conversion start/repair |
| `ConversionCallbacks`, event types | `controller.py`, `events.py` | Web SSE bridge |
| `LibraryService`, `ConversionService`, `VoiceService`, `RuntimeCapabilityService` | `services/` | Controller only (intended) |

### Private helpers

- `services/conversion_paths.py` — path resolution against DB workspace roots
- `services/conversion_commands.py` — start/repair/cancel command objects
- `services/conversion_result_adapter.py` — pipeline ↔ storage result mapping
- `services/progress_math.py` — progress snapshot math for SSE
- `services/tts_session.py` — shared TTS engine session for sequential runs
- `voice_recommender.py` — LLM voice suggestion (uses `ingestion.models.Document`)

### Cleanup candidates

| File | Lines | Issue |
|------|-------|-------|
| `controller.py` | ~597 | Façade + job registry + capability passthrough; split delegate surface from job state |
| `services/conversion_service.py` | ~719 | **>700 lines** — mixes threading, pipeline invocation, storage updates, events |
| `services/library_service.py` | ~601 | Upload limits, preflight, folder open, CRUD in one module |
| `services/runtime_capability_service.py` | ~407 | Duplicates cleaner/TTS probe logic also exposed on controller |
| `ConversionRuntimeOptions` vs web `ConversionRuntimeOptionsRequest` | — | Parallel schema definitions (Workstream 2) |

Constants duplicated elsewhere:

- `SUPPORTED_UPLOAD_SUFFIXES`, `MAX_UPLOAD_BYTES` in `library_service.py` (also referenced from `web/routes/uploads.py`)
- Default voice `am_adam` in `conversion_runtime.py` and web route models

---

## `modules/web`

**Ownership:** FastAPI HTTP surface, desktop auth middleware, SSE event broker,
request/response serializers. No conversion business logic beyond delegation to
`AppController`.

### Public entry points

| Symbol | Location | Consumers |
|--------|----------|-----------|
| `create_app` | `server.py` | `apps/launchers/desktop_backend.py`, tests |
| Route modules | `routes/*.py` | Mounted by `server.py` |
| `EventBroker` | `events.py` | Injected into conversion callbacks |
| `desktop_auth` middleware | `desktop_auth.py` | Desktop shell bearer + query tokens |

### Private helpers

- `deps.py` — FastAPI dependency injection (`ControllerDep`, `call_with_http_errors`)
- `serializers.py` — book/job dict shaping
- `source_handles.py` — upload handle → filesystem path resolution

### Cleanup candidates

- Pydantic request models in `routes/conversions.py` and `routes/options.py`
  mirror `ConversionRuntimeOptions` field-for-field — centralize (Workstream 2).
- `routes/options.py` imports `modules.tts` directly for cleaner metadata;
  could go through `RuntimeCapabilityService` to keep web thinner.
- `MAX_BOOK_TITLE_LENGTH` imported from `pipeline.book_workspace` in
  conversions route — title policy belongs in a shared contract module.

---

## `modules/pipeline`

**Ownership:** End-to-end conversion orchestration: workspace setup, chapter
dispatch, stage execution, parallel workers, repair, progress, result validation.

### Public entry points

| Symbol | Location | Consumers |
|--------|----------|-----------|
| `ConversionPipeline` | `orchestrator.py` | `app.services.conversion_service`, benchmarks, tools |
| `PipelineConfig`, `ConversionResult`, stage enums | `models.py` | App, workers, benchmarks |
| `ParallelConfig`, `SharedWorkerPoolManager`, etc. | `parallel.py`, `pool_manager.py` | Controller, capability service |
| `worker_recommendation` | `worker_recommendation.py` | App warmup endpoints |

### Private helpers

- `chapter_processor.py` (**1027 lines — exceeds 1k rule**), `chapter_runners.py`, `chapter_dispatch.py`
- `pipeline_stages.py`, `stages/*` — per-stage implementations
- `worker_runtime.py`, `worker_config.py`, `worker_errors.py` — multiprocessing workers
- `book_workspace.py`, `conversion_lifecycle.py`, `repair.py`, `retry.py`
- `memory_policy.py`, `cleaner_memory.py`, `model_lifecycle.py`, `progress.py`

### Stage → downstream imports

| Stage | External packages |
|-------|-------------------|
| `stages/ingestion.py` | `ingestion` parsers + `text.document_normalizer` |
| `stages/cleaning.py` | `tts.cleaner_backends` |
| `stages/synthesis.py`, `overlap_synthesis.py` | `tts` (lazy: chunker, speech_normalizer, strategies) |
| `stages/encoding.py` | `audio.encoder` (lazy) |
| `stages/packaging.py` | `audio.packager`, `audio.document_packager` (lazy) |

### Cleanup candidates

| File | Lines | Issue |
|------|-------|-------|
| `chapter_processor.py` | 1027 | **Must decompose** — chunk/clean/synth/encode orchestration |
| `stages/synthesis.py` | 819 | Stage + profiling + overlap logic |
| `orchestrator.py` | 652 | Run lifecycle + parallel fallback branches |
| `pool_manager.py` | 691 | Worker pool + VRAM bookkeeping |
| `models.py` | — | Embeds TTS runtime resolution; blurs pipeline vs tts contract |
| `cleaner_memory` vs `tts.device_memory` | — | Split ownership causes `pipeline` ↔ `tts` near-cycle |

---

## `modules/ingestion`

**Ownership:** Source file parsing, preflight inspection, content filtering,
PDF artifact normalization. Outputs `ingestion.models.Document` / `Chapter`.

### Public entry points

| Symbol | Location | Consumers |
|--------|----------|-----------|
| `PDFParser`, `EPUBParser`, `TextParser`, `MarkdownParser` | `__init__` lazy exports | Pipeline ingestion stage, benchmarks |
| `Document`, `Chapter` | `models.py` | Pipeline, text normalizer, voice recommender |
| `preflight_source` | `preflight.py` | `app.library_service`, API `/api/sources/preflight` |

### Private helpers

- `pdf_parser.py`, `epub_parser.py`, `epub_sections.py` (776 lines)
- `pdf_artifact_normalizer.py` (608 lines), `pdf_preflight.py`
- `content_filter.py`, `text_encoding.py`

### Cleanup candidates

- `SUPPORTED_PREFLIGHT_SUFFIXES` in `preflight.py` duplicates
  `library_service.SUPPORTED_UPLOAD_SUFFIXES` (`.pdf`, `.epub`, `.txt`, `.md`).
- `epub_sections.py` size — candidate for split by concern (TOC vs body vs metadata).
- PDF-specific paths leak into generic error types via `modules.errors` — document
  which errors are PDF-only vs shared.

---

## `modules/tts`

**Ownership:** Text cleaning (MLX/CUDA/API/rules), Kokoro TTS runtimes, chunking,
voice catalog, runtime spec/options, device memory policies, API rate limiting.

### Public entry points

| Symbol | Location | Consumers |
|--------|----------|-----------|
| `TTSEngineFactory`, `create_tts_engine` | `factory.py` | Pipeline, controller, benchmarks |
| `TextCleaner`, `CleanerConfig` | `cleaner.py`, `cleaner_config.py` | Pipeline stages, workers |
| `TTSRuntimeSpec`, `TTSRuntimeOptions`, `resolve_tts_runtime` | `runtime_spec.py`, `runtime_options.py` | Pipeline models, app runtime |
| `cleaner_backends` registry + metadata | `cleaner_backends/` | Web options, app capabilities |
| `TextChunker`, `ChunkConfig` | `chunker.py` | Pipeline, benchmarks |
| Legacy `TTSEngine` | `engine.py` | Older call sites; strategy pattern preferred |

### Private helpers

- `runtimes/` — MLX vs package Kokoro implementations
- `strategies/` — `KokoroTTSStrategy` and base protocol
- `cleaner_cuda.py`, `cleaner_mlx.py`, `cleaner_api.py`, `cleaner_rules.py`
- `vram_monitor.py` (906 lines), `device_memory.py` (551 lines)
- `engine_runtime.py`, `model_cache.py`, `speech_normalizer.py`

### Cleanup candidates

| File | Lines | Issue |
|------|-------|-------|
| `vram_monitor.py` | 906 | Oversized; split probe vs policy |
| `engine.py` | 767 | Legacy engine path coexists with strategies |
| `runtime_options.py` | 501 | Quantization aliases + resolution + validation |
| `cleaner_api.py` | 511 | API transport + model listing |
| Lazy import of `pipeline.cleaner_memory` from `device_memory.py` | — | **Break near-cycle** by moving memory estimation to `tts` or a shared `runtime_memory` module |

---

## `modules/audio`

**Ownership:** FFmpeg invocation, WAV/MP3 encoding, M4B and document-audio
packaging, buffer pooling.

### Public entry points

| Symbol | Location | Consumers |
|--------|----------|-----------|
| `AudioEncoder` | `encoder.py` | Pipeline encoding stage |
| `M4BPackager`, `DocumentAudioPackager` | `packager.py`, `document_packager.py` | Pipeline packaging stage |
| `AudioProcessor` | `processor.py` | Tests, legacy paths |
| `resolve_ffmpeg_binary` | `ffmpeg.py` | `scripts/build_desktop_backend.py` |
| `concatenate_audio_files` | `concat.py` | `tts.engine` |

### Private helpers

- `buffer_pool.py` — optional pooled buffers (`BUFFER_POOL_AVAILABLE` gate)

### Cleanup candidates

- Packaging stage lazy-imports keep audio decoupled (good); ensure sidecar build
  (`build_desktop_backend.py`) stays aligned with `ffmpeg.py` resolution logic.
- No cycles; package is a healthy leaf.

---

## `modules/storage`

**Ownership:** SQLite persistence, migrations, path maintenance, repository
interface. No business rules about conversion options.

### Public entry points

| Symbol | Location | Consumers |
|--------|----------|-----------|
| `IBookRepository`, `SQLiteRepository` | `repository.py`, `sqlite_repo.py` | App controller/services |
| Model dataclasses | `models.py` | App, web serializers, pipeline result adapter |
| `backup_database`, `audit_storage_state`, `prune_stale_records_and_files` | `maintenance.py` | Controller maintenance APIs |
| `resolve_stored_path`, `workspace_root_for_db` | `maintenance.py` | App path services |

### Private helpers

- `migrations.py` — schema versioning
- `sqlite_repo.py` (644 lines) — SQL implementation details

### Cleanup candidates

- `ConversionResult` exists in both `storage.models` and `pipeline.models` —
  adapter in `conversion_result_adapter.py` maps between them; rename or namespace
  to prevent mistaken imports.
- `maintenance.py` (453 lines) — split audit vs prune vs path helpers if growing.

---

## Recommended Import Rules (for later enforcement)

1. `ingestion`, `audio`, `storage` must not import `app`, `web`, or `pipeline`.
2. `web` must not import `pipeline` except shared constants (today: `book_workspace`;
   move to contract module).
3. `tts` must not import `pipeline` (resolve `device_memory` / `cleaner_memory` split).
4. `pipeline` may import `ingestion`, `tts`, `audio`, `text`; not `web` or `app`.
5. Benchmarks and scripts should prefer package `__init__` exports or documented
   script entry points over deep internal imports.

---

## Files Over 1000 Lines (presumptive decomposition targets)

| File | Lines |
|------|-------|
| `modules/pipeline/chapter_processor.py` | 1027 |

Additional files between 700–999 lines are listed per package above; prioritize
`chapter_processor.py` before any other pipeline split.
