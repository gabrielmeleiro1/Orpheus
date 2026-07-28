# Architecture Modernization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Orpheus easier to understand, change, type-check, and optimize while preserving its HTTP, SQLite, artifact, desktop-lifecycle, and documented CLI behavior.

**Architecture:** Keep the Tauri + React + local FastAPI topology. Modernize it incrementally by freezing observable behavior, enforcing the package graph, replacing dynamic/manual boundary contracts, extracting explicit state owners, consolidating execution and memory policy, and deleting compatibility machinery only after all repository callers migrate.

**Tech Stack:** Python 3.12, FastAPI/Pydantic, pytest, Ruff, mypy, React 18, TypeScript 5.6, Vitest, Vite, Tauri 2, Rust 1.88, SQLite, FFmpeg, OpenAPI.

---

## Working rules

- Read `docs/plans/2026-07-28-architecture-modernization-design.md` before starting.
- Preserve user-facing HTTP payloads, persisted SQLite data, generated artifacts,
  desktop process behavior, and documented CLI commands.
- Internal imports and test-only shims may change once all in-repository callers
  migrate.
- Keep every commit releasable. Do not combine a state-owner extraction with
  unrelated behavior changes.
- Use characterization tests before changing concurrency, persistence, TTS,
  memory, setup, or artifact behavior.
- For pure moves, require an unchanged focused test result. For behavior changes,
  use red-green-refactor.
- Record before/after benchmark evidence for parser, TTS, worker, setup, frontend
  request, or packaging hot paths.
- Do not use file length as the acceptance criterion. Use ownership, dependency,
  testability, and explainability.
- Do not weaken the backend generation fences, operation ownership checks,
  stage-then-commit deletion, database migrations, or path-containment checks.
- Before every commit, run `git status --short`, stage only the exact task-owned
  paths, and inspect `git diff --cached --name-only`. A directory path in a
  sample `git add` command is safe only when that directory contains no
  unrelated work; otherwise enumerate the files.

## Delivery map

| Phase | Tasks | Outcome | Estimate | Risk |
| --- | --- | --- | ---: | --- |
| 0. Baseline and enforcement | 1-2 | Frozen behavior and executable dependency graph | 3-5 days | Low |
| 1. Contracts | 3-5 | Static Python contracts, generated TS, one source manifest | 5-8 days | Medium |
| 2. Application state | 6-8 | Explicit operation/job ownership and focused library services | 8-12 days | High |
| 3. Setup boundary | 9-10 | Production no longer imports CLIs; release omits dev supervisor | 5-7 days | High |
| 4. Execution policy | 11-13 | Typed failures, no near-cycle, one worker policy, cohesive pool/synthesis state | 10-15 days | High |
| 5. Runtime ownership | 14-16 | Direct provider runtimes, owned cleaner state, simplified memory | 10-15 days | Very high |
| 6. Frontend and ingestion | 17-18 | Reducer-based library state, source registry, and cohesive EPUB ingestion | 7-10 days | High |
| 7. Tooling and ratchets | 19-21 | Importable benchmark core, one test runner, typing/docs cleanup | 5-8 days | Medium |

Expected single-engineer duration: roughly 11-17 weeks including review,
hardware/profile checks, and benchmark stabilization. Tasks 17 and 19 can run
in parallel after Tasks 1-5; Task 9 starts only after Task 6 supplies the shared
operation coordinator. Tasks 11-13 must precede Tasks 14-16.

## Phase 0: Freeze behavior and enforce architecture

### Task 1: Record the modernization baseline

**Files:**
- Create: `docs/architecture/modernization-baseline.md`
- Create: `tools/architecture_baseline.py`
- Create: `tests/test_architecture_characterization.py`
- Create: `tests/test_architecture_baseline_tool.py`
- Create: `desktop/src/features/library/libraryPerformance.test.tsx`
- Modify: `docs/TESTING.md`
- Reference: `docs/plans/2026-07-24-complete-release-readiness-audit.md`

**Step 1: Record the exact source baseline**

Write the branch, commit SHA, Python/Node/Rust versions, supported runtime
profiles, current test counts, current coverage, and focused benchmark commands
to `docs/architecture/modernization-baseline.md`.

Run:

```bash
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
python --version
node --version
rustc --version
```

Expected: the document identifies the exact commit used for all comparisons.
Do not claim a gate passed until it has been run on that commit.

**Step 2: Add representative public-contract characterization tests**

Add tests that capture:

```python
def test_conversion_routes_keep_legacy_job_fields(client):
    response = client.post("/api/conversions", json=valid_conversion_payload())
    assert set(response.json()) >= {"job_id", "status", "book_id", "error"}


def test_active_conversion_keeps_active_and_job_envelope(client):
    assert set(client.get("/api/conversions/active").json()) == {"active", "job"}


def test_library_page_keeps_paging_envelope(client):
    assert set(client.get("/api/library").json()) == {
        "items", "count", "limit", "offset"
    }
```

Reuse existing fixtures from `tests/test_web_api.py`; do not duplicate backend
setup.

**Step 3: Add lifecycle characterization**

Cover the externally visible transition and conflict matrix:

```text
idle -> running -> completed
idle -> running -> failed
idle -> running -> cancelled
active job + new start -> rejected
active job + global cleanup -> rejected
same-book repair + delete -> rejected
different-book delete + repair -> allowed only when current behavior allows it
```

Assert event ordering, final job payload, and reservation release behavior.

**Step 4: Add one repeatable measurement harness**

`tools/architecture_baseline.py` must fail on missing inputs rather than treating
a manifest directory as a source. Give it five subcommands:

- `source`: discover concrete `.epub/.pdf/.txt/.md` files under `--books-dir`,
  verify them against the stress manifest, and invoke the existing benchmark
  core for parse and clean/gate modes;
- `runtime`: for a profile with a local ML runtime, run a bounded full conversion
  repeatedly and record time to first audio, RTF, total TTS/package time, peak
  RSS/device memory, output duration, metadata, and artifact validity; reject
  the library-only `base` profile rather than manufacturing a conversion;
- `worker`: record startup, reuse, incompatible restart, cancellation shutdown,
  final shutdown, leak checks, and 1/2/4-worker throughput;
- `package`: record `.app` and sidecar sizes, launch the packaged executable with
  an isolated app-data directory, measure process start to authenticated ready
  payload, then request and verify graceful shutdown. For `base`, also verify
  health and library APIs work while conversion/setup report their exact
  expected profile-baked unavailable status and payload.
- `compare`: match baseline/current result categories and input hashes, compute
  per-sample medians/spread and percentage deltas, enforce the configured 5%
  control-path ceiling, and emit JSON plus Markdown. Missing, skipped, or
  mismatched inputs are reported explicitly and never treated as passing.

Every JSON result includes the commit, platform/toolchain, command arguments,
input SHA-256, repetitions, individual samples, median, and spread. The harness
uses existing benchmark/application APIs; it does not create a second
conversion implementation. Unit-test corpus discovery, missing-profile skips,
metric serialization, and packaged-process cleanup.

The focused library Vitest scenario uses mocked transport and a render counter
to write initial/search/page/detail API request counts, render counts, abort
handling, and final state to `ORPHEUS_PERF_OUTPUT`. It writes only when that
environment variable is set.

**Step 5: Produce the baseline with concrete inputs**

Run exactly:

```bash
mkdir -p .artifacts/architecture-modernization/baseline
python scripts/download_stress_corpus.py --books-dir .artifacts/architecture-modernization/corpus
python tools/architecture_baseline.py source --books-dir .artifacts/architecture-modernization/corpus --manifest benchmarks/stress_corpus/manifest.tsv --modes parse,gate --output .artifacts/architecture-modernization/baseline/source.json
python tools/architecture_baseline.py runtime --source tests/fixtures/benchmark_suite/short_chapter.md --profile mlx --repetitions 3 --chapter-limit 1 --output .artifacts/architecture-modernization/baseline/runtime-mlx.json
python tools/architecture_baseline.py runtime --source tests/fixtures/benchmark_suite/short_chapter.md --profile cuda --repetitions 3 --chapter-limit 1 --output .artifacts/architecture-modernization/baseline/runtime-cuda.json
python tools/architecture_baseline.py worker --source .artifacts/architecture-modernization/corpus/frankenstein.epub --workers 1,2,4 --repetitions 3 --output .artifacts/architecture-modernization/baseline/worker.json
(cd desktop && ORPHEUS_PERF_OUTPUT=../.artifacts/architecture-modernization/baseline/library.json npm test -- --run src/features/library/libraryPerformance.test.tsx)
(cd desktop && npm run typecheck && npm test -- --run && npm run build)
(cd desktop/src-tauri && cargo test --locked -- --test-threads=1 && cargo check --locked)
(cd desktop && ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build)
python tools/architecture_baseline.py package --bundle-root desktop/src-tauri/target/release/bundle --expected-profile base --output .artifacts/architecture-modernization/baseline/package-base.json
python run_tests.py --contract
```

The source command must report the concrete discovered file list and fail when
zero files are found. For an unavailable MLX/CUDA profile or a host that cannot
launch the packaged app, write `SKIPPED` with the probed reason; never convert a
skip to a pass. `docs/architecture/modernization-baseline.md` links every JSON
artifact and records the exact command and status.

**Step 6: Commit**

```bash
git add docs/architecture/modernization-baseline.md docs/TESTING.md tools/architecture_baseline.py tests/test_architecture_characterization.py tests/test_architecture_baseline_tool.py desktop/src/features/library/libraryPerformance.test.tsx
git commit -m "test(architecture): freeze modernization baseline"
```

### Task 2: Enforce the complete Python dependency graph

**Files:**
- Create: `tests/test_architecture_boundaries.py`
- Modify: `tests/test_web_import_boundaries.py`
- Modify: `docs/architecture/python-boundaries.md`

**Step 1: Write an AST-based boundary test with a debt allowlist**

The test must scan every `modules/**/*.py` import, normalize relative imports,
and compare violations with an explicit temporary allowlist:

```python
ALLOWED_DEBT = {
    ("modules.tts.device_memory", "modules.pipeline.cleaner_memory"),
    ("modules.contracts.conversion_runtime_options", "pydantic"),
    ("modules.contracts.source_extensions", "modules.storage.models"),
    ("modules.storage.maintenance_reconcile", "modules.pipeline.book_workspace"),
    ("modules.app.services.model_download_service", "scripts.setup_runtime"),
    ("modules.app.services.model_download_service", "scripts.download_models"),
    ("modules.web.routes.setup", "scripts.setup_runtime"),
    ("modules.web.routes.setup", "scripts.download_models"),
    ("modules.web.setup_status_cache", "scripts.download_models"),
    ("modules.web.server", "scripts.setup_runtime"),
}

FOUNDATIONAL_IMPORT_ALLOWLIST = {
    "modules.contracts": ("modules.contracts",),
    "modules.runtime_failures": (
        "modules.runtime_failures",
        "modules.contracts",
    ),
    "modules.setup": (
        "modules.setup",
        "modules.contracts",
        "modules.runtime_profiles",
    ),
}

FORBIDDEN_PREFIXES = {
    "modules.contracts": (
        "modules.app",
        "modules.web",
        "modules.pipeline",
        "modules.tts",
        "modules.ingestion",
        "modules.audio",
        "modules.storage",
        "scripts",
        "tools",
    ),
    "modules.web": (
        "modules.pipeline",
        "modules.tts",
        "modules.storage",
        "scripts",
        "tools",
    ),
    "modules.app": ("modules.web", "scripts", "tools"),
    "modules.pipeline": ("modules.app", "modules.web", "modules.storage", "scripts", "tools"),
    "modules.tts": ("modules.app", "modules.web", "modules.pipeline", "modules.storage", "scripts", "tools"),
    "modules.ingestion": ("modules.app", "modules.web", "modules.pipeline", "modules.storage", "scripts", "tools"),
    "modules.audio": ("modules.app", "modules.web", "modules.pipeline", "modules.storage", "scripts", "tools"),
    "modules.storage": ("modules.app", "modules.web", "modules.pipeline", "modules.tts", "scripts", "tools"),
    "modules.text": ("modules.app", "modules.web", "modules.pipeline", "modules.storage", "scripts", "tools"),
    "modules.setup": (
        "modules.app",
        "modules.web",
        "modules.pipeline",
        "modules.tts",
        "modules.ingestion",
        "modules.audio",
        "modules.storage",
        "scripts",
        "tools",
    ),
    "modules.runtime_failures": (
        "modules.app",
        "modules.web",
        "modules.pipeline",
        "modules.tts",
        "modules.ingestion",
        "modules.audio",
        "modules.storage",
        "scripts",
        "tools",
    ),
}
```

Assert `actual_violations == ALLOWED_DEBT`, not merely that no unexpected
violation exists. This makes removal of known debt fail until the allowlist is
shrunk.

**Step 2: Run the new test and verify its first failure**

Run:

```bash
python -m pytest tests/test_architecture_boundaries.py -q -o addopts=
```

Expected: FAIL until relative imports and the exact current debt are normalized
correctly.

**Step 3: Implement import normalization**

Use `ast.Import`, `ast.ImportFrom`, `importlib.util.resolve_name`, and the
source file's package path. For `from package import name`, also resolve
`package.name` when that candidate is an importable module/package in the
repository; this is required for imports such as
`from scripts import setup_runtime`, whose AST `module` field is only
`scripts`. Record both ordinary imports and alias-resolved submodule imports
once, and include useful failure output showing importer, imported module, and
rule.

Apply `FOUNDATIONAL_IMPORT_ALLOWLIST` as a positive rule before the general
denylist. A foundational module may import only the standard library
(`sys.stdlib_module_names` plus `__future__`), itself, and the explicitly listed
internal prefixes. Third-party packages, `scripts`, `tools`, `modules.text`,
`modules.setup` from runtime failures, and any future/root `modules.*` package
are rejected unless the allowlist is deliberately changed in architectural
review. `ALLOWED_DEBT` temporarily overrides the positive rule only for the
enumerated current edge.

**Step 4: Fold the narrow web test into the complete rule**

Keep `tests/test_web_import_boundaries.py` only if it tests web-specific
semantics not covered by the new test. Otherwise migrate its assertions and
delete it in the same commit.

**Step 5: Run focused checks**

```bash
python -m pytest tests/test_architecture_boundaries.py tests/test_web_import_boundaries.py -q -o addopts=
python -m ruff check tests/test_architecture_boundaries.py
```

Expected: PASS with exactly ten documented debt edges: one TTS near-cycle, two
foundational inversions, one temporary Pydantic dependency in contracts, and
six setup layering edges.

**Step 6: Commit**

```bash
git add tests/test_architecture_boundaries.py tests/test_web_import_boundaries.py docs/architecture/python-boundaries.md
git commit -m "test(architecture): enforce package dependency graph"
```

## Phase 1: Make cross-layer contracts static

### Task 3: Replace runtime-generated conversion DTOs

**Files:**
- Modify: `modules/contracts/conversion_runtime_options.py:1`
- Create: `modules/web/schemas/__init__.py`
- Create: `modules/web/schemas/conversions.py`
- Modify: `modules/web/routes/conversions.py:26`
- Modify: `modules/web/routes/options.py`
- Modify: `modules/contracts/conversion_runtime.py`
- Modify: `modules/app/services/conversion_runtime.py`
- Modify: `tests/test_conversion_runtime_options_contract.py`
- Modify: `tests/test_architecture_boundaries.py`

**Step 1: Write a failing static-contract test**

Require an explicit dataclass and explicit Pydantic model:

```python
def test_conversion_runtime_options_are_static_classes():
    assert dataclasses.is_dataclass(ConversionRuntimeOptions)
    assert ConversionRuntimeOptions.__module__ == (
        "modules.contracts.conversion_runtime_options"
    )
    source = inspect.getsource(ConversionRuntimeOptions)
    assert "@dataclass" in source


def test_api_and_application_runtime_option_fields_match():
    app_fields = {field.name for field in dataclasses.fields(ConversionRuntimeOptions)}
    api_fields = set(ConversionRuntimeOptionsRequest.model_fields)
    assert api_fields == app_fields
```

Also assert defaults and Pydantic constraints for `speed` and
`max_parallel_chapters`.

**Step 2: Run the test and verify it fails**

```bash
python -m pytest tests/test_conversion_runtime_options_contract.py -q -o addopts=
```

Expected: FAIL because both classes are currently created at runtime.

**Step 3: Replace the dynamic dataclass**

Define the complete static DTO:

```python
@dataclass(frozen=True, slots=True)
class ConversionRuntimeOptions:
    voice: str = "am_adam"
    speed: float = 1.0
    tts_engine: str | None = None
    tts_quantization: str | None = None
    tts_provider: str | None = None
    tts_device: str | None = None
    cleaner_model: str | None = None
    cleaner_backend: str | None = None
    cleaner_cuda_device: str | None = None
    cleaner_cuda_quantization: str | None = None
    cleaner_api_model: str | None = None
    cleaner_api_key: str | None = None
    tts_options: Mapping[str, object] | None = None
    parallel_worker_mode: str | None = None
    enable_parallel: bool | None = None
    max_parallel_chapters: int | None = None
```

Remove the Pydantic `Field` dependency and `dataclasses.make_dataclass` from
`modules/contracts`, then remove
`("modules.contracts.conversion_runtime_options", "pydantic")` from the
architecture debt allowlist.

**Step 4: Define static web request models**

Put `_InfNanSafeModel`, `ConversionRuntimeOptionsRequest`,
`StartConversionRequest`, and `RepairConversionRequest` in
`modules/web/schemas/conversions.py`. Keep `to_app_options()` explicit.

Do not generate a Pydantic model from field metadata. The API and application
models represent different boundaries; the sync test is the mechanical guard.

**Step 5: Preserve nested override behavior**

Run and expand tests for:

- flat options;
- nested `runtime_options`;
- `model_fields_set` merge semantics;
- NaN/infinity rejection;
- defaults;
- repair's persisted-option merge.

**Step 6: Run focused and contract tests**

```bash
python -m pytest \
  tests/test_conversion_runtime_options_contract.py \
  tests/test_web_api.py \
  tests/test_conversion_service.py \
  tests/test_repair_runtime.py \
  tests/test_architecture_boundaries.py \
  -q -o addopts=
python run_tests.py --contract
```

Expected: all pass; public request/response JSON remains unchanged.

**Step 7: Commit**

```bash
git add modules/contracts/conversion_runtime_options.py modules/contracts/conversion_runtime.py modules/web/schemas modules/web/routes/conversions.py modules/web/routes/options.py modules/app/services/conversion_runtime.py tests/test_conversion_runtime_options_contract.py tests/test_architecture_boundaries.py
git commit -m "refactor(contracts): make conversion options static"
```

### Task 4: Add typed API responses and generated TypeScript transport types

**Files:**
- Create: `modules/web/schemas/common.py`
- Create: `modules/web/schemas/conversions.py`
- Create: `modules/web/schemas/library.py`
- Create: `modules/web/schemas/maintenance.py`
- Create: `modules/web/schemas/options.py`
- Create: `modules/web/schemas/sources.py`
- Create: `modules/web/schemas/setup.py`
- Create: `modules/web/schemas/voices.py`
- Create: `scripts/export_openapi_schema.py`
- Create: `desktop/src/types/generated/openapi.ts`
- Create: `desktop/src/types/transport.ts`
- Modify: `modules/web/routes/conversions.py`
- Modify: `modules/web/routes/health.py`
- Modify: `modules/web/routes/library.py`
- Modify: `modules/web/routes/maintenance.py`
- Modify: `modules/web/routes/options.py`
- Modify: `modules/web/routes/setup.py`
- Modify: `modules/web/routes/sources.py`
- Modify: `modules/web/routes/uploads.py`
- Modify: `modules/web/routes/voices.py`
- Modify: `modules/web/serializers.py`
- Modify: `desktop/src/types/api.ts`
- Modify: `desktop/src/types/backend.ts`
- Modify: `desktop/src/types/conversion.ts`
- Modify: `desktop/src/types/library.ts`
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Create: `tests/test_openapi_contract.py`
- Modify: `.github/workflows/source-validation.yml`

**Step 1: Add failing response-model tests**

For every JSON route, including `/health`, require a FastAPI response model or a
deliberately documented streaming/file exception:

```python
def test_json_routes_declare_response_models(app):
    missing = []
    for route in app.routes:
        if not (
            getattr(route, "path", "").startswith("/api/")
            or route.path == "/health"
        ):
            continue
        if (
            route.path.endswith("/audio")
            or route.path.endswith("/events/stream")
            or route.path == "/api/voices/preview"
        ):
            continue
        if getattr(route, "response_model", None) is None:
            missing.append(route.path)
    assert missing == []
```

Add field-level tests for conversion, active job, library page, setup status,
maintenance, and options payloads.

**Step 2: Run and verify failure**

```bash
python -m pytest tests/test_openapi_contract.py -q -o addopts=
```

Expected: FAIL listing currently untyped routes.

**Step 3: Add static response schemas**

Create Pydantic response types with `ConfigDict(from_attributes=True)` where
useful. Keep serializers as mappings from storage/application objects into
those types. Do not move application behavior into schema validators.

**Step 4: Export a deterministic OpenAPI schema**

`scripts/export_openapi_schema.py` must:

1. construct the app without starting a backend process;
2. call `app.openapi()`;
3. recursively sort object keys;
4. write UTF-8 JSON with a trailing newline;
5. accept `--output`.

Test two exports for byte equality.

**Step 5: Generate TypeScript**

Add `openapi-typescript` as a dev dependency and a package script:

```json
"api:generate": "python ../scripts/export_openapi_schema.py --output ../.artifacts/openapi.json && openapi-typescript ../.artifacts/openapi.json -o src/types/generated/openapi.ts"
```

In `desktop/src/types/transport.ts`, create readable aliases:

```ts
import type { components } from "./generated/openapi";

export type StartConversionRequest =
  components["schemas"]["StartConversionRequest"];
export type StartConversionResponse =
  components["schemas"]["StartConversionResponse"];
```

`desktop/src/types/api.ts`, `backend.ts`, `conversion.ts`, and `library.ts` may
re-export readable aliases but must stop redefining HTTP payloads manually.
Keep desktop lifecycle types, UI-only unions, event payloads, and view models
outside generated code.

**Step 6: Add the generated-file drift gate**

In source validation:

```bash
(cd desktop && npm run api:generate && git diff --exit-code -- src/types/generated/openapi.ts)
```

**Step 7: Run all cross-language contract checks**

```bash
python -m pytest tests/test_openapi_contract.py tests/test_web_route_contract.py -q -o addopts=
(cd desktop && npm run api:generate && npm run typecheck && npm test -- --run && npm run build)
git diff --exit-code -- desktop/src/types/generated/openapi.ts
```

Expected: generated file is stable and the frontend compiles without duplicate
transport interfaces.

**Step 8: Commit**

```bash
git add modules/web/schemas modules/web/routes/conversions.py modules/web/routes/health.py modules/web/routes/library.py modules/web/routes/maintenance.py modules/web/routes/options.py modules/web/routes/setup.py modules/web/routes/sources.py modules/web/routes/uploads.py modules/web/routes/voices.py modules/web/serializers.py scripts/export_openapi_schema.py tests/test_openapi_contract.py desktop/src/types/api.ts desktop/src/types/backend.ts desktop/src/types/conversion.ts desktop/src/types/library.ts desktop/src/types/generated/openapi.ts desktop/src/types/transport.ts desktop/package.json desktop/package-lock.json .github/workflows/source-validation.yml
git commit -m "refactor(api): generate desktop transport contracts"
```

### Task 5: Centralize source-type metadata across Python, React, and Rust

**Files:**
- Create: `config/source_types.json`
- Create: `modules/contracts/source_types.py`
- Modify: `modules/contracts/source_extensions.py`
- Modify: `modules/ingestion/preflight.py`
- Modify: `modules/app/services/library_service.py`
- Modify: `modules/web/routes/options.py`
- Modify: `desktop/src-tauri/src/main.rs`
- Modify: `desktop/src/features/conversion/ConversionPanelView.tsx`
- Modify: `desktop/src/features/conversion/useConversionPanelState.ts`
- Modify: `desktop/src/features/conversion/ConversionPanelView.test.tsx`
- Modify: `tools/feedback_loop_runner.py`
- Create: `tests/test_source_type_manifest.py`
- Modify: `tests/test_architecture_boundaries.py`
- Modify: `desktop/backend-sidecar.spec`
- Modify: `desktop/src-tauri/src/backend_tests.rs`
- Modify: `tests/test_desktop_runtime_contract.py`

**Step 1: Write a manifest-sync test**

The manifest has explicit entries:

```json
{
  "schema_version": 1,
  "sources": [
    {"id": "pdf", "suffixes": [".pdf"], "label": "PDF", "audiobook": true, "document_audio": true},
    {"id": "epub", "suffixes": [".epub"], "label": "EPUB", "audiobook": true, "document_audio": true},
    {"id": "text", "suffixes": [".txt"], "label": "Text", "audiobook": true, "document_audio": true},
    {"id": "markdown", "suffixes": [".md"], "label": "Markdown", "audiobook": true, "document_audio": true}
  ]
}
```

Test schema version, unique IDs/suffixes, normalized leading dots, and agreement
with API capabilities and Rust's parsed filter.

**Step 2: Verify current drift**

Run:

```bash
python -m pytest tests/test_source_type_manifest.py -q -o addopts=
```

Expected: FAIL because tool restrictions, mode metadata, and UI/Rust copies are
not derived from one checked-in contract.

**Step 3: Add the canonical Python loader**

`modules/contracts/source_types.py` returns immutable `SourceTypeDefinition`
objects and derived suffix sets. `source_extensions.py` becomes a compatibility
re-export for one phase, then is deleted in Task 21.

The contract owns its own string source IDs and must not import the storage
`SourceType` enum. Remove
`("modules.contracts.source_extensions", "modules.storage.models")` from the
architecture debt allowlist in this task.

**Step 4: Make API validation authoritative**

Upload and preflight validate through the manifest. Include source definitions
in `/api/system/capabilities` or an options response so React renders accepted
formats from backend data.

Do not rely on the file picker or UI copy for security or correctness. Preserve
the currently accepted suffix set in this task. The additive `.markdown` alias
lands atomically with parser/pipeline support in Task 18 and normalizes to the
existing persisted `md` source type; it never requires a new SQLite enum value
or migration.

Remove the hard-coded `BROWSER_SOURCE_ACCEPT` value from
`useConversionPanelState.ts`. Derive the browser input's `accept` string from
the backend source definitions, with the current Task 5 manifest as its sole
fallback. Test stable suffix ordering and the initial/loading state. Task 18
then changes the manifest and expectation in the same commit as parser support.

**Step 5: Make Rust consume the same checked-in manifest**

Use `include_str!("../../../config/source_types.json")` through a small typed
Rust parser. The picker strips leading dots and passes the suffix list to
`rfd::FileDialog`.

If compile-time path resolution differs in Cargo, adjust the relative path and
cover it with a Rust unit test. Do not copy the list into a second Rust constant.

**Step 6: Bundle the manifest into the Python sidecar**

Add `config/source_types.json` to `desktop/backend-sidecar.spec` data files and
make the Python loader resolve both source-checkout and PyInstaller
`sys._MEIPASS` locations. Extend the sidecar contract test to inspect the
collected files and launch the frozen loader without a repository checkout.
Missing or invalid packaged manifests must fail startup with a clear diagnostic,
not silently fall back to another list.

**Step 7: Run focused and packaged checks**

```bash
python -m pytest tests/test_source_type_manifest.py tests/test_source_preflight.py tests/test_web_upload_limits.py -q -o addopts=
python -m pytest tests/test_architecture_boundaries.py tests/test_desktop_runtime_contract.py -q -o addopts=
(cd desktop && npm run typecheck && npm test -- --run)
(cd desktop/src-tauri && cargo test --locked && cargo check --locked)
(cd desktop && ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build)
```

Expected: source checkout and packaged sidecar load the same manifest, and all
layers agree on suffixes and mode support.

**Step 8: Commit**

```bash
git add config/source_types.json modules/contracts/source_types.py modules/contracts/source_extensions.py modules/ingestion/preflight.py modules/app/services/library_service.py modules/web/routes/options.py desktop/backend-sidecar.spec desktop/src-tauri/src/main.rs desktop/src/features/conversion/ConversionPanelView.tsx desktop/src/features/conversion/useConversionPanelState.ts desktop/src/features/conversion/ConversionPanelView.test.tsx tools/feedback_loop_runner.py tests/test_source_type_manifest.py tests/test_architecture_boundaries.py tests/test_desktop_runtime_contract.py desktop/src-tauri/src/backend_tests.rs
git commit -m "refactor(sources): centralize source type metadata"
```

## Phase 2: Give application state explicit owners

### Task 6: Extract synchronized job state and the operation coordinator

**Files:**
- Create: `modules/app/jobs.py`
- Create: `modules/app/operation_coordinator.py`
- Modify: `modules/app/controller.py:57`
- Modify: `modules/app/services/conversion_service.py:59`
- Modify: `modules/app/services/library_service.py:698`
- Create: `tests/app/test_jobs.py`
- Create: `tests/app/test_operation_coordinator.py`
- Modify: `tests/test_conversion_service.py`
- Modify: `tests/test_library_service.py`

**Step 1: Write the job-state transition tests**

Test an explicit one-way state machine:

```python
@pytest.mark.parametrize(
    ("terminal_method", "expected"),
    [
        ("complete", JobStatus.COMPLETED),
        ("fail", JobStatus.FAILED),
        ("mark_cancelled", JobStatus.CANCELLED),
    ],
)
def test_job_terminal_state_cannot_return_to_running(terminal_method, expected):
    job = ConversionJob.create("job-1")
    job.begin_running()
    transition_to_terminal(job, terminal_method)
    with pytest.raises(InvalidJobTransition):
        job.begin_running()
    assert job.snapshot().status is expected
```

Also test that result/book ID/error and terminal status become visible in one
synchronized snapshot.

**Step 2: Write the operation conflict matrix**

`OperationCoordinator` tests must cover:

| Existing reservation | New reservation | Expected |
| --- | --- | --- |
| active/pending job | job | reject |
| storage maintenance | job | reject |
| book maintenance | storage maintenance | reject |
| job for book A | book A maintenance | reject |
| active repair for book A | book B maintenance | allow |
| terminal job for book A | book A maintenance | allow |
| book A maintenance | repair job A | reject |
| book A maintenance | new conversion without a stored book ID | allow |
| book A maintenance | book B maintenance | allow |

Test stale-token release, wrong-owner maintenance release, and duplicate release.

**Step 3: Run and verify failure**

```bash
python -m pytest tests/app/test_jobs.py tests/app/test_operation_coordinator.py -q -o addopts=
```

Expected: FAIL because the new state owners do not exist.

**Step 4: Implement `ConversionJob` in `modules/app/jobs.py`**

Use a private lock, `threading.Event`, and immutable `JobSnapshot`. Public
methods are:

```python
class ConversionJob:
    @classmethod
    def create(cls, job_id: str, *, book_id: int | None = None, ...) -> Self: ...
    def begin_running(self, thread: threading.Thread | None = None) -> None: ...
    def request_cancel(self) -> bool: ...
    def attach_pipeline(self, pipeline: CancellablePipeline) -> bool: ...
    def detach_pipeline(self) -> None: ...
    def set_result(self, result: StoredConversionResult) -> None: ...
    def complete(self, book_id: int | None) -> None: ...
    def fail(self, message: str, result: StoredConversionResult) -> None: ...
    def mark_cancelled(self, message: str) -> None: ...
    def snapshot(self) -> JobSnapshot: ...
    def wait(self, timeout: float | None = None) -> bool: ...
```

Keep compatibility properties (`status`, `book_id`, `error`) only until routes
and tests consume `snapshot()`.

**Step 5: Implement `OperationCoordinator`**

Use one lock and opaque generation tokens. Job completion may occur on a worker
thread, so a job token is validated by identity/generation rather than thread
ID. Storage/book maintenance tokens retain owner-thread checks.

The coordinator stores the most recent job so `/active` can preserve its current
terminal payload while `has_active_job()` returns false.

**Step 6: Wire the controller and services**

Construct one coordinator in `AppController.__init__` and inject it into
conversion and library/maintenance services. Remove lock fields and cleanup
reservation dictionaries from `ConversionService`.

Do not yet extract execution logic; Task 7 does that after state ownership is
stable.

**Step 7: Run concurrency-focused tests**

```bash
python -m pytest \
  tests/app/test_jobs.py \
  tests/app/test_operation_coordinator.py \
  tests/test_conversion_service.py \
  tests/test_library_service.py \
  tests/test_web_api.py \
  -q -o addopts=
```

Repeat race-sensitive tests:

```bash
for run in {1..20}; do
  python -m pytest tests/app/test_operation_coordinator.py tests/test_conversion_service.py -q -o addopts= || exit 1
done
```

Expected: all 20 iterations pass without a hang or leaked worker.

**Step 8: Commit**

```bash
git add modules/app/jobs.py modules/app/operation_coordinator.py modules/app/controller.py modules/app/services/conversion_service.py modules/app/services/library_service.py tests/app tests/test_conversion_service.py tests/test_library_service.py
git commit -m "refactor(app): centralize operation and job ownership"
```

### Task 7: Extract conversion execution from conversion submission

**Files:**
- Create: `modules/app/services/conversion_job_runner.py`
- Modify: `modules/app/services/conversion_commands.py`
- Modify: `modules/app/services/conversion_service.py`
- Modify: `modules/app/services/tts_session.py`
- Modify: `modules/app/controller.py:365`
- Create: `tests/app/test_conversion_job_runner.py`
- Split: `tests/test_conversion_service.py`

**Step 1: Define and test public execution protocols**

Add protocols/dataclasses:

```python
class ConversionCommand(Protocol):
    job: ConversionJob
    runtime_options: ConversionRuntimeOptions
    conversion_mode: ConversionMode | str
    def configure(self, config: PipelineConfig) -> PipelineConfig: ...
    def run(self, pipeline: ConversionPipelineProtocol) -> PipelineConversionResult: ...
    def persist_success(
        self,
        context: ConversionRunContext,
        pipeline_result: PipelineConversionResult,
        stored_result: StoredConversionResult,
    ) -> None: ...


@dataclass(frozen=True)
class ConversionRunContext:
    repository: BookRepository
    event_sink: EventSink
    pipeline_factory: PipelineFactory
    result_adapter: ConversionResultAdapter
```

Test that commands can run with a fake context and never receive a
`ConversionService` instance.

**Step 2: Verify current private coupling**

Add a static test that rejects:

```python
"service._" in inspect.getsource(ConversionRunCommand)
"service._" in inspect.getsource(RepairRunCommand)
```

Expected: FAIL against the current command implementation.

**Step 3: Implement `ConversionJobRunner`**

Move these responsibilities from `ConversionService`:

- thread construction/start;
- state event ordering;
- pipeline callback adaptation;
- pipeline attach/detach;
- command execution;
- result adaptation;
- terminal job transitions;
- summary emission;
- TTS-session release.

Keep durable-source adoption and request validation in `ConversionService`.

**Step 4: Remove private service calls from commands**

Pass repository, result persistence, output-path resolution, and event functions
through explicit context fields. `resolve_repair_context` receives values or
protocols, not the entire service.

**Step 5: Remove controller private wrappers**

Delete or make public the compatibility methods at
`modules/app/controller.py:423-491` that call
`self._conversion_service._...`. Migrate all tests to the public runner/service
surface.

**Step 6: Split the 5,500-line service test by ownership**

Move tests without changing assertions:

- job state/coordinator -> `tests/app/test_jobs.py`;
- runner/callback/event behavior -> `tests/app/test_conversion_job_runner.py`;
- runtime option resolution -> `tests/app/test_conversion_runtime.py`;
- service submission/durable source -> `tests/app/test_conversion_service.py`;
- repair -> existing `tests/test_repair_runtime.py` or
  `tests/app/test_repair_service.py`.

Delete the old monolith only after `pytest --collect-only` shows the same test
count.

**Step 7: Run focused and contract gates**

```bash
python -m pytest tests/app tests/test_repair_runtime.py tests/test_web_api.py -q -o addopts=
python run_tests.py --contract
```

Acceptance:

- `ConversionService` owns validation, reservation, durable source adoption,
  and submission only;
- no command or controller method reaches into private conversion-service state;
- terminal event ordering is unchanged;
- cancellation still detaches and shuts down the shared worker pool.

**Step 8: Commit**

```bash
git add modules/app/services/conversion_job_runner.py modules/app/services/conversion_commands.py modules/app/services/conversion_service.py modules/app/services/tts_session.py modules/app/controller.py tests/app tests/test_conversion_service.py tests/test_repair_runtime.py
git commit -m "refactor(app): separate conversion submission and execution"
```

### Task 8: Split source intake, library catalog, and deletion transactions

**Files:**
- Create: `modules/contracts/book_identity.py`
- Create: `modules/app/services/source_intake_service.py`
- Create: `modules/app/services/library_catalog_service.py`
- Create: `modules/app/services/library_deletion_service.py`
- Modify: `modules/app/services/library_service.py`
- Modify: `modules/app/controller.py:228`
- Modify: `modules/app/services/__init__.py`
- Modify: `modules/pipeline/book_workspace.py`
- Modify: `modules/storage/maintenance_reconcile.py`
- Modify: `tests/test_architecture_boundaries.py`
- Create: `tests/app/test_source_intake_service.py`
- Create: `tests/app/test_library_catalog_service.py`
- Create: `tests/app/test_library_deletion_service.py`
- Split: `tests/test_library_service.py`

**Step 1: Characterize each existing responsibility**

Create focused tests for:

- upload size enforcement and partial-file cleanup;
- pasted Markdown naming and normalization;
- preflight fallbacks;
- paged catalog reads and source-availability calculation;
- metadata/playback updates;
- cleaning-preview reads;
- stage/restore/commit deletion;
- shared-source reference preservation;
- path containment and output-root refusal;
- folder opening.

Run the old and new test files together before moving code.

**Step 2: Move source intake**

Move upload, paste, stale-upload sweep, and preflight from
`LibraryService` to `SourceIntakeService`. Inject the source registry and path
roots.

**Step 3: Move catalog behavior**

Move list/count/detail/metadata/playback/preview/folder behavior to
`LibraryCatalogService`. Keep response serialization out of the service.

**Step 4: Move deletion behavior**

Move path classification and stage/restore/commit logic to
`LibraryDeletionService`. Inject `OperationCoordinator` and repository. The
service obtains and releases the per-book reservation itself through a context
manager:

```python
with coordinator.reserve_book_maintenance(book_id):
    return deletion_service.delete(book_id)
```

Preserve rollback if repository deletion fails.

**Step 5: Move shared book identity/path normalization below pipeline**

Move the pure `sanitize_book_title`/book-directory naming contract to
`modules/contracts/book_identity.py`. Pipeline workspace and storage
reconciliation both import the foundational function; storage must not import
pipeline. Preserve all current normalization cases byte-for-byte and remove:

```python
("modules.storage.maintenance_reconcile", "modules.pipeline.book_workspace")
```

from the architecture debt allowlist.

**Step 6: Remove the compatibility façade**

After `AppController` injects and delegates to the three focused services, delete
`LibraryService` or reduce it to a deprecated re-export for one commit. Because
internal compatibility is not required, remove it in the same phase once all
repository imports are migrated.

**Step 7: Run storage, boundary, and app gates**

```bash
python -m pytest \
  tests/app/test_source_intake_service.py \
  tests/app/test_library_catalog_service.py \
  tests/app/test_library_deletion_service.py \
  tests/test_storage_repository.py \
  tests/test_storage_maintenance.py \
  tests/test_architecture_boundaries.py \
  tests/test_web_api.py \
  -q -o addopts=
python run_tests.py --contract
```

Expected: stage-then-commit semantics, path safety, and API payloads are
unchanged.

**Step 8: Commit**

```bash
git add modules/contracts/book_identity.py modules/app/services modules/app/controller.py modules/pipeline/book_workspace.py modules/storage/maintenance_reconcile.py tests/app tests/test_library_service.py tests/test_architecture_boundaries.py
git commit -m "refactor(library): separate intake catalog and deletion"
```

## Phase 3: Move setup out of production-to-script coupling

### Task 9: Extract setup domain and application service from CLI scripts

**Dependency:** Task 6 must be complete; runtime/model deletion reuses the
shared `OperationCoordinator` rather than recreating a setup-specific lock.

**Files:**
- Create: `modules/setup/__init__.py`
- Create: `modules/setup/models.py`
- Create: `modules/setup/validation.py`
- Create: `modules/setup/state.py`
- Create: `modules/setup/probe.py`
- Create: `modules/setup/downloads.py`
- Create: `modules/setup/progress.py`
- Create: `modules/app/services/runtime_setup_service.py`
- Modify: `modules/app/operation_coordinator.py`
- Modify: `modules/app/services/model_download_service.py`
- Modify: `modules/runtime_profiles.py`
- Modify: `scripts/setup_runtime.py`
- Modify: `scripts/download_models.py`
- Modify: `modules/web/routes/setup.py`
- Modify: `modules/web/deps.py`
- Modify: `modules/web/server.py`
- Modify: `modules/web/setup_status_cache.py`
- Create: `tests/setup/test_models.py`
- Create: `tests/setup/test_state.py`
- Create: `tests/setup/test_service.py`
- Create: `tests/setup/test_model_download_service.py`
- Modify: `tests/test_setup_runtime.py`
- Modify: `tests/test_setup_route.py`
- Modify: `tests/test_setup_onboarding.py`
- Modify: `tests/test_download_models.py`
- Modify: `tests/test_architecture_boundaries.py`

**Step 1: Add the desired-boundary failure**

Remove all six setup-script edges from `ALLOWED_DEBT` in
`tests/test_architecture_boundaries.py`.

Run:

```bash
python -m pytest tests/test_architecture_boundaries.py -q -o addopts=
```

Expected: FAIL showing `modules.web.routes.setup -> scripts.*`.

**Step 2: Characterize setup behavior**

Before moving code, cover:

- profile and model-group validation;
- state-file schema and atomic writes;
- lock ownership/staleness;
- backend adoption compare-and-swap;
- installed/runtime probe output;
- model receipt verification;
- progress event schema and percent monotonicity;
- setup status cache key;
- model deletion and status invalidation;
- runtime/model deletion rejected while a conversion is reserved, including the
  submitted-but-not-yet-running window;
- deletion reservation release after success, failure, and cancellation;
- CLI JSON and exit codes.

**Step 3: Move pure domain code**

Move dataclasses/protocols to `models.py`, validation to `validation.py`, atomic
state/lock logic to `state.py`, probe combination to `probe.py`, model groups
and receipt verification to `downloads.py`, and progress DTO/sanitization to
`progress.py`.

Use public names. Web code must not call functions beginning with `_`.

**Step 4: Add `RuntimeSetupService`**

The service owns setup/adoption/download/delete use cases and composes:

```python
@dataclass(frozen=True)
class RuntimeSetupDependencies:
    probe_service: RuntimeProbe
    download_service: ModelDownloadService
    receipt_verifier: ModelReceiptVerifier
    operation_coordinator: OperationCoordinator
    clock: Clock
```

Expose it through a FastAPI dependency from app state or `AppController`.
Routes validate HTTP payloads and translate service results only.

Runtime/model deletion acquires the coordinator's global maintenance
reservation before any filesystem mutation. The service owns acquisition and
release; the route must not reproduce the old private storage-cleanup guard.
Add conflict tests for active and pending conversion reservations and prove the
token is released on every terminal path.

**Step 5: Reduce scripts to CLI adapters**

Target each script's `main()` plus parser/formatting glue. The scripts import
`modules.setup`; no production module imports the scripts.

Preserve all documented flags and JSON formats, including
`--progress-json` and `--validate-existing`.

**Step 6: Run focused tests and the boundary test**

```bash
python -m pytest \
  tests/setup \
  tests/test_setup_runtime.py \
  tests/test_setup_route.py \
  tests/test_setup_onboarding.py \
  tests/test_download_models.py \
  tests/test_architecture_boundaries.py \
  -q -o addopts=
python scripts/setup_runtime.py --profile base --dry-run --json
```

Expected: no production import of `scripts` or `tools`.

**Step 7: Commit**

```bash
git add modules/setup modules/app/operation_coordinator.py modules/app/services/runtime_setup_service.py modules/app/services/model_download_service.py modules/runtime_profiles.py modules/web/routes/setup.py modules/web/deps.py modules/web/server.py modules/web/setup_status_cache.py scripts/setup_runtime.py scripts/download_models.py tests/setup tests/test_setup_runtime.py tests/test_setup_route.py tests/test_setup_onboarding.py tests/test_download_models.py tests/test_architecture_boundaries.py
git commit -m "refactor(setup): move runtime setup behind application service"
```

### Task 10: Compile source-checkout setup supervision only where it can run

**Files:**
- Create: `desktop/src-tauri/src/process_control.rs`
- Create: `desktop/src-tauri/src/setup/mod.rs`
- Create: `desktop/src-tauri/src/setup/contract.rs`
- Create: `desktop/src-tauri/src/setup/source_checkout.rs`
- Create: `desktop/src-tauri/src/setup/packaged.rs`
- Delete: `desktop/src-tauri/src/setup.rs`
- Modify: `desktop/src-tauri/src/main.rs`
- Modify: `desktop/src-tauri/src/backend.rs`
- Modify: `desktop/src-tauri/src/backend_tests.rs`
- Modify: `desktop/src-tauri/src/setup_tests.rs`
- Modify: `desktop/src/features/setup/setupBridge.ts`
- Modify: `desktop/src/features/setup/SetupPage.tsx`

**Step 1: Add build-mode contract tests**

Test:

```text
debug/source checkout + trusted .venv + script -> setup available
debug/source checkout + missing script -> unavailable with current reason
packaged build -> unavailable/profile-baked
packaged start command -> deterministic unavailable error
backend process Windows Job Object and generation fencing -> unchanged
```

**Step 2: Move shared process primitives**

`WindowsJob`, suspended-child resume, process-tree termination, and safe command
environment helpers used by backend lifecycle move to `process_control.rs`.
They are not setup-domain concepts.

**Step 3: Split setup contract from implementation**

Always compile serializable request/status/environment/progress types and Tauri
command signatures in `setup/contract.rs`.

Compile the current Python supervisor only under `cfg(debug_assertions)` in
`setup/source_checkout.rs`.

Under `cfg(not(debug_assertions))`, `setup/packaged.rs` returns the current
profile-baked unavailable status without compiling source-checkout discovery,
script launch, progress readers, or setup process supervision.

**Step 4: Preserve the frontend command contract**

Keep Tauri command names and response shapes unchanged so SetupPage behavior is
preserved. The UI may simplify copy based on `environment.mode`, but this task
must not redesign setup UX.

**Step 5: Run Rust and frontend gates**

```bash
(cd desktop/src-tauri && cargo fmt --check && cargo test --locked -- --test-threads=1 && cargo check --locked && cargo check --locked --release)
(cd desktop && npm run typecheck && npm test -- --run && npm run build)
```

**Step 6: Verify packaged behavior**

```bash
(cd desktop && ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build)
```

Run the packaged smoke from `docs/TESTING.md`. Compare release binary/bundle
size with the Task 1 baseline and record the change.

**Step 7: Commit**

```bash
git add desktop/src-tauri/src desktop/src/features/setup
git commit -m "refactor(tauri): isolate source-checkout setup supervisor"
```

## Phase 4: Consolidate failure, worker, and pool policy

### Task 11: Introduce typed runtime failures and break the TTS/pipeline near-cycle

**Files:**
- Create: `modules/runtime_failures.py`
- Modify: `modules/errors.py`
- Create: `modules/tts/cleaner_memory.py`
- Modify: `modules/tts/cleaner_errors.py`
- Modify: `modules/tts/runtimes/errors.py`
- Modify: `modules/tts/engine.py:350`
- Modify: `modules/tts/device_memory.py:450`
- Modify: `modules/pipeline/cleaner_memory.py`
- Modify: `modules/pipeline/retry.py`
- Modify: `modules/pipeline/worker_errors.py`
- Modify: `modules/web/deps.py`
- Create: `tests/test_runtime_failures.py`
- Create: `tests/test_web_error_contract.py`
- Modify: `tests/test_device_memory_policy.py`
- Modify: `tests/test_runtime_recovery.py`
- Modify: `tests/test_architecture_boundaries.py`

**Step 1: Write failure-classification tests**

Define:

```python
class FailureKind(str, Enum):
    VALIDATION = "validation"
    UNAVAILABLE = "unavailable"
    MEMORY = "memory"
    INFRASTRUCTURE = "infrastructure"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class FailureInfo:
    kind: FailureKind
    code: str
    message: str
    retryable: bool
    stage: str | None = None
```

Tests must prove typed exceptions win over text and reject false positives:

```python
@pytest.mark.parametrize(
    "message",
    [
        "failed to create memoryview",
        "shared memory name is invalid",
        "memory measurement unavailable",
    ],
)
def test_unrelated_memory_words_are_not_oom(message):
    assert classify_external_runtime_error(RuntimeError(message)).kind is not FailureKind.MEMORY
```

Include real CUDA, MLX/Metal, PyTorch, worker pool, cancellation, and unavailable
messages already present in fixtures.

**Step 2: Verify current classification failure**

```bash
python -m pytest tests/test_runtime_failures.py -q -o addopts=
```

Expected: FAIL because broad substring matching classifies the negative cases as
memory pressure.

**Step 3: Implement typed failure plumbing**

Provider edges translate third-party exceptions once. Internal layers preserve
`FailureInfo` or typed exceptions instead of converting to strings and
reclassifying.

Keep a narrow compatibility classifier with phrases such as `"out of memory"`,
`"cuda error: memory allocation"`, and known Metal allocation errors. Do not use
the token `"memory"` alone.

Map live `AudiobookError` codes into `FailureInfo` without changing their
existing `str(error)` representation. Record the seven currently unreferenced
`ErrorCode` members as internal cleanup candidates; remove them only after the
full repository/CLI/doc search in Task 21.

**Step 4: Preserve HTTP error payloads exactly**

Task 11 changes internal runtime failure plumbing only. At
`call_with_http_errors`, translate a typed runtime failure back to the exact
current status code and `HTTPException.detail` value. Do not add a global
problem handler or a new payload field in this phase: existing details include
both strings and dictionaries, and both shapes are public compatibility
contracts.

`tests/test_web_error_contract.py` snapshots status and complete JSON bodies for
representative conversion, setup, upload, library, maintenance, authentication,
validation, and unhandled-error paths. It separately covers routes that call
`call_with_http_errors` and routes/authentication handlers that raise directly,
so the internal migration cannot accidentally normalize dictionary-valued
setup details into strings.

**Step 5: Move cleaner memory estimation to TTS**

Cleaner runtime/device estimation belongs in `modules/tts/cleaner_memory.py`.
Update pipeline consumers to import it downward. Remove the lazy
`tts.device_memory -> pipeline.cleaner_memory` import.

Delete `modules/pipeline/cleaner_memory.py` after all callers migrate, or leave a
one-commit forwarding shim only if required to bisect safely.

**Step 6: Remove the near-cycle allowlist entry**

Delete:

```python
("modules.tts.device_memory", "modules.pipeline.cleaner_memory")
```

from the architecture debt allowlist.

**Step 7: Run focused checks**

```bash
python -m pytest \
  tests/test_runtime_failures.py \
  tests/test_device_memory_policy.py \
  tests/test_runtime_recovery.py \
  tests/test_parallel_fallback.py \
  tests/test_web_error_contract.py \
  tests/test_architecture_boundaries.py \
  -q -o addopts=
python run_tests.py --contract
```

**Step 8: Commit**

```bash
git add modules/runtime_failures.py modules/errors.py modules/tts modules/pipeline modules/web/deps.py tests/test_runtime_failures.py tests/test_web_error_contract.py tests/test_device_memory_policy.py tests/test_runtime_recovery.py tests/test_parallel_fallback.py tests/test_architecture_boundaries.py
git commit -m "refactor(runtime): centralize failures and memory estimation"
```

### Task 12: Make parallel worker selection one explainable policy

**Files:**
- Create: `modules/pipeline/worker_policy.py`
- Modify: `modules/pipeline/models.py:45`
- Modify: `modules/pipeline/chapter_dispatch.py`
- Modify: `modules/pipeline/worker_recommendation.py`
- Modify: `modules/pipeline/memory_policy.py`
- Modify: `modules/app/services/conversion_runtime.py`
- Modify: `modules/app/services/runtime_capability_service.py`
- Modify: `modules/web/routes/options.py`
- Create: `tests/test_worker_policy.py`
- Modify: `tests/test_parallel_pipeline.py`
- Modify: `tests/test_pool_manager.py`
- Modify: `desktop/src/features/conversion/useParallelWorkers.ts`

**Step 1: Write a complete decision-table test**

Use immutable inputs:

```python
@dataclass(frozen=True, slots=True)
class WorkerPolicyInput:
    requested: int
    enabled: bool
    mode: Literal["auto", "manual"]
    backend_auto_mode: Literal["sequential", "cpu_cores", "memory_safe"]
    process_safe: bool
    same_device_contention: bool
    memory_limit: int | None
    total_chapters: int
    total_chars: int
    pool_capacity: int
```

The result contains:

```python
@dataclass(frozen=True, slots=True)
class WorkerDecision:
    selected: int
    requested: int
    reductions: tuple[WorkerReduction, ...]
```

Cover disabled, manual, sequential backend, CPU-core auto, same-device
contention, unavailable sampling, measured memory, small books, pool capacity,
and combined reductions.

**Step 2: Verify policy is currently distributed**

Add an assertion that `PipelineConfig.__post_init__` does not call
`build_worker_plan` and run:

```bash
python -m pytest tests/test_worker_policy.py -q -o addopts=
```

Expected: FAIL.

**Step 3: Implement the pure policy**

Move every worker-count reduction to `worker_policy.py`. The function must be
pure and deterministic. It may call a passed `MemoryAllowance` protocol but may
not sample memory directly.

Order reductions explicitly:

1. parallel disabled/request <= 1;
2. backend capability;
3. same-device contention;
4. memory allowance;
5. workload/chapter bound;
6. pool capacity.

Manual mode bypasses only the guards it bypasses today; it never bypasses
fundamental capacity/correctness bounds.

**Step 4: Remove hidden planning from `PipelineConfig`**

`PipelineConfig` stores resolved settings and a policy input/template. It does
not create a mutable `worker_plan` during `__post_init__`.

`chapter_dispatch` calls the single policy once it knows chapter count/size and
pool capacity.

**Step 5: Reuse the policy for capabilities/warmup**

The API worker recommendation uses the same decision component with an explicit
`workload_unknown=True` or representative workload. Return structured reduction
reasons; keep existing frontend label fields during compatibility.

**Step 6: Run focused and frontend tests**

```bash
python -m pytest tests/test_worker_policy.py tests/test_parallel_pipeline.py tests/test_pool_manager.py tests/test_conversion_service.py -q -o addopts=
(cd desktop && npm run typecheck && npm test -- --run)
```

Acceptance: one test table explains every final worker count, and no other
module independently changes it.

**Step 7: Commit**

```bash
git add modules/pipeline/worker_policy.py modules/pipeline/models.py modules/pipeline/chapter_dispatch.py modules/pipeline/worker_recommendation.py modules/pipeline/memory_policy.py modules/app/services/conversion_runtime.py modules/app/services/runtime_capability_service.py modules/web/routes/options.py desktop/src/features/conversion/useParallelWorkers.ts tests/test_worker_policy.py tests/test_parallel_pipeline.py tests/test_pool_manager.py
git commit -m "refactor(parallel): centralize worker selection policy"
```

### Task 13: Split worker-pool lifecycle from run tracking

**Files:**
- Create: `modules/pipeline/pool_lifecycle.py`
- Create: `modules/pipeline/parallel_run_tracker.py`
- Create: `modules/pipeline/synthesis_scheduler.py`
- Create: `modules/pipeline/synthesis_result_accumulator.py`
- Modify: `modules/pipeline/pool_manager.py`
- Delete or absorb: `modules/pipeline/pool_state.py`
- Delete or absorb: `modules/pipeline/pool_shutdown.py`
- Delete or absorb: `modules/pipeline/pool_results.py`
- Delete or absorb: `modules/pipeline/pool_workers.py`
- Modify: `modules/pipeline/parallel.py`
- Modify: `modules/pipeline/stages/synthesis.py`
- Create: `tests/pipeline/test_pool_lifecycle.py`
- Create: `tests/pipeline/test_parallel_run_tracker.py`
- Create: `tests/pipeline/test_synthesis_scheduler.py`
- Create: `tests/pipeline/test_synthesis_result_accumulator.py`
- Split: `tests/test_pool_manager.py`

**Step 1: Characterize lifecycle transitions**

Test this state machine:

```text
stopped -> starting -> running -> stopping -> stopped
starting -> failed -> stopped
running -> unhealthy -> stopping -> stopped
running + compatible config -> reused
running + incompatible config -> restarted
stopping + ensure_started -> waits boundedly
cancel + shutdown race -> one owner finalizes
```

Assert startup error status, bounded shutdown, process/thread executor
differences, generation fencing, and reuse counters.

**Step 2: Characterize run tracking**

Test submitted/completed/failed chapter sets, duplicate completion, cancellation,
progress snapshots, chapter titles, and metrics independently of an executor.
Also freeze synthesis fallback order, source-order result assembly,
file-backed/in-memory audio selection, partial failure, cancellation, timing
attribution, and profiling output before moving those responsibilities out of
`stages/synthesis.py`.

**Step 3: Implement `WorkerPoolLifecycle`**

Own:

- executor and fingerprint;
- startup probes;
- health checks;
- stopping barrier;
- bounded shutdown;
- lifecycle generation and metrics.

It exposes no chapter/job collections.

**Step 4: Implement `ParallelRunTracker`**

Own run-scoped chapter state, progress, cancellation, and performance snapshots.
It exposes immutable snapshots and explicit transition methods.

**Step 5: Give synthesis scheduling and accumulation explicit owners**

`SynthesisScheduler` owns serial/parallel dispatch, fallback selection, and
cancellation handoff through the pool interface. `SynthesisResultAccumulator`
owns source-order assembly, file-backed versus in-memory result selection,
partial failure aggregation, and stage timing/profile summaries. Both expose
immutable results.

`stages/synthesis.py` remains the pipeline-stage coordinator: it builds the
request, invokes the scheduler, feeds completed items into the accumulator, and
returns the existing stage result. It must not own executor lifecycle or
duplicate result-ordering state. This is an ownership extraction, not a pure
line-count split.

**Step 6: Make `SharedWorkerPoolManager` a coordinator**

The manager:

1. asks lifecycle for a compatible healthy executor;
2. creates one run tracker;
3. submits work;
4. feeds results into the tracker;
5. returns results;
6. tells lifecycle to stop only on policy/error/cancel conditions.

Remove single-consumer micro-files by moving their logic to the component that
owns the state. Keep a file only when its unit tests do not require constructing
the manager.

**Step 7: Verify cancellation and synthesis repeatedly**

```bash
python -m pytest tests/pipeline/test_pool_lifecycle.py tests/pipeline/test_parallel_run_tracker.py tests/pipeline/test_synthesis_scheduler.py tests/pipeline/test_synthesis_result_accumulator.py tests/test_pool_manager.py tests/test_parallel_pipeline.py tests/test_pipeline.py -q -o addopts=
```

Repeat the shutdown/cancel/startup-race cases at least 50 times locally and
record failures/timeouts.

**Step 8: Run a worker and synthesis benchmark**

Compare:

- first startup;
- reuse hit;
- incompatible restart;
- graceful shutdown;
- cancellation shutdown;
- 1/2/4 worker chapter throughput;
- ordered synthesis throughput, fallback count, and file/in-memory artifact
  selection.

No unexplained control-path slowdown over 5%; no leaked child process/thread.

**Step 9: Commit**

```bash
git add modules/pipeline tests/pipeline tests/test_pool_manager.py tests/test_parallel_pipeline.py
git commit -m "refactor(pool): separate lifecycle and run tracking"
```

## Phase 5: Make TTS, cleaner, and memory ownership real

### Task 14: Collapse the legacy MLX engine into the provider runtime

**Files:**
- Create: `modules/tts/runtimes/mlx_synthesizer.py`
- Modify: `modules/tts/runtimes/kokoro_mlx.py`
- Modify: `modules/tts/runtimes/kokoro_base.py`
- Modify: `modules/tts/strategies/kokoro.py`
- Modify: `modules/tts/factory.py`
- Move/absorb: `modules/tts/engine_runtime.py`
- Move/absorb: `modules/tts/engine_output.py`
- Move/absorb: `modules/tts/engine_config.py`
- Delete after migration: `modules/tts/engine.py`
- Modify: `modules/tts/__init__.py`
- Create: `tests/tts/test_mlx_synthesizer.py`
- Modify: `tests/test_kokoro_runtime_boundary.py`
- Modify: `tests/test_tts.py`
- Modify: `tests/test_tts_engine_modularization.py`
- Modify: `tests/test_tts_resilience.py`
- Modify: `tests/test_memory_management.py`
- Modify: `tests/test_device_memory_policy.py`

**Step 1: Freeze the MLX runtime contract**

Test load/cache/warmup/synthesize/batch/unload/metrics through
the current `TTSEngine` first, then run the same contract through
`MLXKokoroRuntime`. Include:

- cache-key and model-override behavior;
- stale output rejection;
- blank-text silence;
- multi-file concatenation;
- grouped result ordering;
- retry classification;
- metrics;
- cache-preserving and hard unload.

Before deleting `TTSEngine`, also freeze its live synchronous memory behavior:

- synthesis refreshes adaptive pressure on demand;
- low/medium/high/critical samples select the same batch size;
- adaptive pause decisions and reported pressure metrics are preserved;
- unavailable sampling follows the current fallback;
- the selected batch decision remains compatible with pipeline worker
  allowance.

Use injected samples and a fake clock, not hardware timing, for the contract
test. Task 16 may later consolidate the implementation only after these tests
pass against the new runtime.

**Step 2: Define one runtime protocol**

Use the `KokoroRuntime`/`SpeechRuntime` protocol with public properties for
loaded state, chunk count, sample rate, average time, and metrics. Remove
`getattr(runtime, "_...")` fallbacks.

**Step 3: Move MLX implementation behind the runtime**

`mlx_synthesizer.py` owns model fields, locks, cache identity, output generation,
and counters. Helpers receive an explicit `MLXRuntimeState` dataclass or scalar
arguments; they do not mutate an arbitrary engine facade.

**Step 4: Migrate production and test callers**

Pipeline, preview, benchmarks, and tests construct engines through
`TTSEngineFactory`/`KokoroRuntimeFactory`. Move low-level tests to the new
runtime class.

**Step 5: Delete the legacy public path**

Remove `TTSEngine` and the duplicate legacy `TTSConfig` after `rg` shows no
production/script/tool caller and no required public documented CLI.

```bash
rg -n '\bTTSEngine\b|modules\.tts\.engine|engine_config' modules scripts tools desktop tests
```

Expected: no runtime caller; only migration notes if any.

**Step 6: Run runtime/profile checks**

```bash
python -m pytest tests/tts tests/test_kokoro_runtime_boundary.py tests/test_tts.py tests/test_tts_engine_modularization.py tests/test_tts_resilience.py tests/test_memory_management.py tests/test_device_memory_policy.py tests/test_pipeline.py -q -o addopts=
python run_tests.py --verify-profile mlx --allow-skipped-profiles
python run_tests.py --contract
```

On Apple Silicon, run the bounded MLX TTS benchmark and compare time-to-first
audio, RTF, peak memory, and artifact validity.

**Step 7: Commit**

```bash
git add modules/tts tests/tts tests/test_kokoro_runtime_boundary.py tests/test_tts.py tests/test_tts_engine_modularization.py tests/test_tts_resilience.py tests/test_memory_management.py tests/test_device_memory_policy.py tests/test_pipeline.py
git commit -m "refactor(tts): make MLX a direct provider runtime"
```

### Task 15: Make cleaner backends own model and transport state

**Files:**
- Modify: `modules/tts/cleaner_backends/base.py`
- Modify: `modules/tts/cleaner_backends/registry.py`
- Create: `modules/tts/cleaner_backends/mlx.py`
- Create: `modules/tts/cleaner_backends/cuda.py`
- Create: `modules/tts/cleaner_backends/api.py`
- Modify: `modules/tts/cleaner.py`
- Move/absorb: `modules/tts/cleaner_mlx.py`
- Move/absorb: `modules/tts/cleaner_cuda.py`
- Move/absorb: `modules/tts/cleaner_api.py`
- Move/absorb: `modules/tts/cleaner_transport.py`
- Modify: `modules/tts/cleaner_trace.py`
- Create: `tests/tts/test_cleaner_backend_contract.py`
- Modify: `tests/test_cleaner.py`
- Modify: `tests/test_cleaner_cuda.py`
- Modify: `tests/test_cleaner_backend_registry.py`
- Modify: `tests/fixtures/fake_cleaner_backend.py`

**Step 1: Define lifecycle/result protocols**

```python
class CleanerBackend(Protocol):
    identity: CleanerRuntimeIdentity
    @property
    def is_loaded(self) -> bool: ...
    def load(self) -> None: ...
    def clean(self, request: CleanerRequest) -> CleanerBackendResult: ...
    def clean_batch(self, requests: Sequence[CleanerRequest]) -> list[CleanerBackendResult]: ...
    def unload(self) -> None: ...
    def metrics(self) -> CleanerMetrics: ...


@runtime_checkable
class AsyncCleanerBackend(Protocol):
    async def clean_batch_async(
        self,
        requests: Sequence[CleanerRequest],
    ) -> list[CleanerBackendResult]: ...
```

Backend objects own model, tokenizer, transport, semaphore/limiter references,
and loaded state. Local MLX/CUDA backends use the synchronous batch contract;
the API backend additionally implements the native async contract. Do not wrap
local inference in an event-loop executor merely to make the interfaces look
uniform.

**Step 2: Write a shared backend contract suite**

Run the same lifecycle/error/metrics tests against fake MLX, CUDA, and API
adapters. Real hardware/network tests remain separately marked.

**Step 3: Implement backend-owned state**

Move existing strategy code without semantic changes. Replace calls such as
`cleaner._model = ...` with backend instance fields. Transport helpers receive
an API backend/client, not `TextCleaner`.

**Step 4: Reduce `TextCleaner` to coordination**

`TextCleaner` owns:

- rule-based normalization;
- artifact scoring/preservation guard;
- request/trace assembly;
- selected backend;
- aggregate metrics if still needed.

It does not expose model/tokenizer/loaded fields for backends to mutate.

**Step 5: Remove test construction through `__new__`**

Replace partially initialized `TextCleaner.__new__(TextCleaner)` fixtures with
explicit fake backends and normal constructor injection. This is required to
make initialization invariants trustworthy.

**Step 6: Prove private reach-ins are gone**

Add a static test over cleaner backend modules that rejects `cleaner._`.

**Step 7: Run checks and hardware lanes**

```bash
python -m pytest tests/tts/test_cleaner_backend_contract.py tests/test_cleaner.py tests/test_cleaner_cuda.py tests/test_cleaner_backend_registry.py tests/test_cleaner_pipeline_dispatch.py -q -o addopts=
python run_tests.py --verify-profile mlx --allow-skipped-profiles
python run_tests.py --verify-profile cuda --allow-skipped-profiles
python run_tests.py --contract
```

Run clean-quality fixtures and compare preservation/fallback counts and latency.

**Step 8: Commit**

```bash
git add modules/tts/cleaner_backends modules/tts/cleaner.py modules/tts/cleaner_mlx.py modules/tts/cleaner_cuda.py modules/tts/cleaner_api.py modules/tts/cleaner_transport.py modules/tts/cleaner_trace.py tests/tts tests/test_cleaner.py tests/test_cleaner_cuda.py tests/test_cleaner_backend_registry.py tests/fixtures/fake_cleaner_backend.py
git commit -m "refactor(cleaner): give backends explicit state ownership"
```

### Task 16: Consolidate live memory behavior and delete unused monitor lifecycle

**Files:**
- Create: `modules/tts/memory_pressure.py`
- Modify: `modules/tts/device_memory.py`
- Modify: `modules/tts/vram_monitor.py`
- Modify: `modules/tts/vram_policy.py`
- Modify: `modules/tts/vram_budget.py`
- Modify: `modules/tts/memory.py`
- Delete after proof: `modules/tts/idle_timeout.py`
- Modify: `modules/app/services/tts_session.py`
- Modify: `modules/pipeline/memory_policy.py`
- Modify: `tests/test_memory_management.py`
- Modify: `tests/test_device_memory_policy.py`
- Modify: `tests/test_tts_engine_session.py`
- Create: `tests/tts/test_memory_pressure.py`

**Step 1: Prove which lifecycle is live**

Add static/runtime tests:

```python
def test_production_does_not_start_background_vram_monitor():
    assert production_call_sites("start_monitoring") == set()


def test_production_does_not_start_idle_timeout_manager():
    assert production_call_sites("IdleTimeoutManager.start") == set()


def test_synthesis_refreshes_pressure_on_demand():
    ...
```

Also test that `TtsEngineSession` is the actual inter-job idle-unload owner.

**Step 2: Record memory behavior**

For low/medium/high/critical samples, capture selected batch size, adaptive
pause, reported metrics, and worker allowance. Include unavailable sampling.

**Step 3: Implement `MemoryPressureSampler`**

Keep synchronous sampling, pressure calculation, and dynamic batch decision in
one object:

```python
class MemoryPressureSampler:
    def sample(self) -> MemoryPressureSnapshot: ...
    def batch_decision(self, base_size: int) -> BatchDecision: ...
```

No background thread is created. History is bounded only if metrics/decisions
actually consume it.

**Step 4: Keep ownership policies distinct but connected**

- `VRAMManager` or its replacement owns mutually exclusive MLX model loading;
- `MemoryPressureSampler` sizes on-demand batch work;
- `DeviceMemoryPolicy` supplies provider capacity/estimation;
- pipeline `SafeMemoryPolicy` consumes snapshots for process-worker limits;
- `TtsEngineSession` owns idle unload between jobs.

Rename objects so these roles are visible. Do not collapse them into one
singleton.

**Step 5: Delete unused lifecycle**

Delete background start/stop loop, `IdleTimeoutManager`, unused callbacks, and
thread-oriented config only after production-call tests are green. Migrate
useful pure pressure dataclasses before deleting old modules.

**Step 6: Run memory and performance checks**

```bash
python -m pytest tests/tts/test_memory_pressure.py tests/test_memory_management.py tests/test_device_memory_policy.py tests/test_tts_engine_session.py tests/test_parallel_pipeline.py -q -o addopts=
python run_tests.py --contract
```

On available hardware, compare peak memory, batch size, pause time, RTF, and
idle unload timing. Behavior may improve, but unexplained higher peak memory is
a failure.

**Step 7: Commit**

```bash
git add modules/tts modules/app/services/tts_session.py modules/pipeline/memory_policy.py tests/tts/test_memory_pressure.py tests/test_memory_management.py tests/test_device_memory_policy.py tests/test_tts_engine_session.py tests/test_parallel_pipeline.py
git commit -m "refactor(memory): retain live policy and remove idle monitors"
```

## Phase 6: Simplify frontend state and ingestion variants

### Task 17: Replace the library hook state web with a reducer and request coordinator

**Files:**
- Create: `desktop/src/features/library/libraryState.ts`
- Create: `desktop/src/features/library/libraryState.test.ts`
- Create: `desktop/src/features/library/libraryRequestCoordinator.ts`
- Create: `desktop/src/features/library/libraryRequestCoordinator.test.ts`
- Create: `desktop/src/features/library/useLibraryController.ts`
- Modify: `desktop/src/features/library/useLibraryList.ts`
- Modify: `desktop/src/features/library/useLibrary.ts`
- Modify: `desktop/src/features/library/useLibrary.test.tsx`
- Modify: `desktop/src/features/library/useLibraryMaintenance.ts`
- Modify: `desktop/src/features/library/useLibraryPanelState.ts`
- Modify: `desktop/src/features/library/LibraryPanel.tsx`

**Step 1: Inventory and freeze library states**

Create a state interface containing:

```ts
export interface LibraryState {
  query: string;
  modeFilter: LibraryModeFilter;
  items: LibraryBookSummary[];
  indexedCount: number;
  hasMore: boolean;
  nextOffset: number;
  loadedContext: LibraryQueryContext | null;
  selection: {
    bookId: number | null;
    index: number;
    detail: LibraryBookDetail | null;
    generation: number;
  };
  pending: {
    list: boolean;
    more: boolean;
    detail: boolean;
  };
  errorMessage: string;
  statusMessage: string;
}
```

Write reducer tests for reset, query replacement, initial page, append with
deduplication, selection, detail success/failure, metadata update, playback
update, deletion, repair status, and disconnect.

**Step 2: Characterize every existing race fence**

Migrate, do not delete, the tests for:

- stale initial list after a query/client change;
- stale detail after selection change;
- load-more landing after a new search;
- duplicate load-more clicks;
- deleting while selection changes;
- metadata/preview/repair landing on a successor selection;
- stepping across a page boundary;
- backend disconnect/reconnect;
- same-book reselection and generation changes.

**Step 3: Implement immutable request tokens**

`LibraryRequestCoordinator` owns three scopes:

```ts
type RequestScope = "list" | "selection" | "mutation";

interface RequestToken {
  scope: RequestScope;
  generation: number;
  bookId?: number;
  queryKey?: string;
  signal: AbortSignal;
  isCurrent(): boolean;
}
```

Starting a successor request aborts/retires only the appropriate scope.
Selection tokens include book ID and selection generation.

**Step 4: Implement `useLibraryController`**

Use `useReducer` for state and the coordinator for effects. Repository calls
dispatch actions only when their token remains current.

Do not create a generic app-wide async framework. Keep the coordinator specific
to the three library ownership scopes.

**Step 5: Keep a temporary `useLibraryList` adapter**

Return the existing `UseLibraryListResult` shape from the new controller so
`LibraryPanel` can migrate without a broad UI rewrite. Delete the adapter once
all local consumers use the controller.

**Step 6: Run repeated frontend tests**

```bash
(cd desktop && npm test -- --run src/features/library/libraryState.test.ts src/features/library/libraryRequestCoordinator.test.ts src/features/library/useLibrary.test.tsx && npm run typecheck && npm test -- --run && npm run build)
```

Repeat race-focused Vitest files with randomized fake delays. No `act` warnings,
unhandled promises, or state updates after unmount.

**Step 7: Measure request/render behavior**

Compare the Task 1 baseline for:

- initial requests;
- requests per search/filter change;
- requests when paging;
- renders for initial/page/detail transitions;
- response handling after abort.

Do not accept additional API calls or an obvious render regression without a
reason.

**Step 8: Commit**

```bash
git add desktop/src/features/library
git commit -m "refactor(library-ui): model library ownership explicitly"
```

### Task 18: Add one explicit ingestion registry

**Files:**
- Modify: `config/source_types.json`
- Create: `modules/ingestion/registry.py`
- Create: `modules/ingestion/epub_navigation.py`
- Create: `modules/ingestion/epub_section_extractor.py`
- Modify: `modules/ingestion/epub_sections.py`
- Modify: `modules/ingestion/epub_parser.py`
- Modify: `modules/ingestion/markdown_parser.py`
- Modify: `modules/pipeline/stages/ingestion.py`
- Modify: `modules/ingestion/preflight.py`
- Modify: `modules/ingestion/__init__.py`
- Modify: `modules/app/services/source_intake_service.py`
- Modify: `scripts/benchmark_nagel_pipeline.py`
- Modify: `orpheus/bench/cli.py`
- Create: `tests/test_ingestion_registry.py`
- Create: `tests/test_epub_navigation.py`
- Create: `tests/test_epub_section_extractor.py`
- Modify: `tests/test_ingestion.py`
- Modify: `tests/test_epub_parser.py`
- Modify: `tests/test_markdown_parser.py`
- Modify: `tests/test_source_type_manifest.py`
- Modify: `desktop/src/features/conversion/useConversionPanelState.ts`
- Modify: `desktop/src/features/conversion/ConversionPanelView.test.tsx`
- Modify: `tests/test_source_preflight.py`

**Step 1: Write registry contract tests**

Define:

```python
class SourceParser(Protocol):
    def parse(self) -> Document: ...


@dataclass(frozen=True, slots=True)
class SourceAdapter:
    definition: SourceTypeDefinition
    parser_factory: Callable[[Path, ParserOptions], SourceParser]
    preflight: Callable[[Path, PreflightOptions], SourcePreflight]
```

Test every manifest suffix has exactly one adapter, every adapter returns a
`Document`, and unsupported suffix errors are identical through preflight and
full ingestion.

**Step 2: Verify current duplicated dispatch**

Run:

```bash
python -m pytest tests/test_ingestion_registry.py -q -o addopts=
```

Expected: FAIL because no registry exists and parser/preflight/tool dispatch is
duplicated.

**Step 3: Implement four explicit adapters and add `.markdown` atomically**

Use an ordinary immutable mapping. Do not introduce entry points, reflection,
or a plugin loader for four built-in formats.

PDF parser factory consumes `pdf_extraction_backend`; others ignore that option.
In this same task, add `.markdown` to `config/source_types.json`, accept it in
`markdown_parser.py` and the pipeline registry, and normalize it to the existing
persisted source type `md`. Because Rust and React consume the Task 5 manifest,
the rebuilt picker/API/UI expose the alias only in the commit that full
ingestion accepts it. Add a packaged-sidecar upload/preflight contract test; do
not add a SQLite enum value.

Update the browser accept-string test in
`useConversionPanelState`/`ConversionPanelView.test.tsx` to prove the derived
capability list now includes `.markdown`; no hard-coded browser suffix constant
may remain.

**Step 4: Separate EPUB navigation from section extraction**

`EpubNavigationReader` owns package metadata, spine/TOC/nav discovery, href
resolution, and canonical reading order. `EpubSectionExtractor` owns content
loading, heading/section boundaries, footnote/non-content filtering, and
section metadata assembly. `epub_sections.py` becomes a compatibility
orchestrator during the move and is removed or reduced to public re-exports
after callers migrate.

Characterize EPUB2 NCX, EPUB3 nav, missing/broken nav fallback, nested headings,
duplicate href fragments, metadata, footnotes, images/non-content, and stable
chapter order. The two owners communicate through immutable navigation/section
records rather than shared mutable parser fields.

**Step 5: Use the registry everywhere**

Pipeline ingestion, source preflight, app source intake, and benchmark parser
selection resolve through the registry. UI/Rust still use the source manifest
from Task 5.

**Step 6: Return typed preflight results internally**

Use a `SourcePreflight` dataclass internally and serialize it at the web
boundary. Preserve the existing JSON field names and warnings.

**Step 7: Run parser, manifest, packaged, and performance gates**

```bash
python -m pytest tests/test_ingestion_registry.py tests/test_ingestion.py tests/test_source_preflight.py tests/test_pdf_parser.py tests/test_epub_parser.py tests/test_epub_navigation.py tests/test_epub_section_extractor.py tests/test_text_parser.py tests/test_markdown_parser.py tests/test_source_type_manifest.py tests/test_desktop_runtime_contract.py -q -o addopts=
python tools/architecture_baseline.py source --books-dir .artifacts/architecture-modernization/corpus --manifest benchmarks/stress_corpus/manifest.tsv --modes parse,gate --output .artifacts/architecture-modernization/task18-source.json
(cd desktop/src-tauri && cargo test --locked && cargo check --locked)
(cd desktop && ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build)
```

Expected: parser output, normalized chapter order, warnings, and parse
throughput remain within baseline. Source-checkout ingestion tests perform full
conversion with `.md` and `.markdown`. The packaged `base` sidecar verifies
upload/preflight recognition and the browser/native picker filters for both
suffixes while preserving the expected conversion-unavailable response; it
does not claim that the library-only profile can synthesize audio.

**Step 8: Commit**

```bash
git add config/source_types.json modules/ingestion modules/pipeline/stages/ingestion.py modules/app/services/source_intake_service.py scripts/benchmark_nagel_pipeline.py orpheus/bench/cli.py desktop/src/features/conversion/useConversionPanelState.ts desktop/src/features/conversion/ConversionPanelView.test.tsx tests/test_ingestion_registry.py tests/test_epub_navigation.py tests/test_epub_section_extractor.py tests/test_ingestion.py tests/test_epub_parser.py tests/test_markdown_parser.py tests/test_source_preflight.py tests/test_source_type_manifest.py
git commit -m "refactor(ingestion): centralize source adapter dispatch"
```

## Phase 7: Consolidate tooling and ratchet maintainability

### Task 19: Extract benchmark execution and schemas from CLI scripts

**Files:**
- Create: `orpheus/bench/models.py`
- Create: `orpheus/bench/runner.py`
- Modify: `orpheus/bench/metrics.py`
- Modify: `orpheus/bench/report.py`
- Modify: `orpheus/bench/presets.py`
- Modify: `orpheus/bench/cli.py`
- Modify: `scripts/benchmark_nagel_pipeline.py`
- Modify: `scripts/benchmark_nagel_repeated.py`
- Modify: `scripts/performance_regression_report.py`
- Modify: `benchmarks/kokoro_single_book/run_kokoro_single_book.py`
- Modify: `tools/feedback_loop_runner.py`
- Create: `tests/bench/test_models.py`
- Create: `tests/bench/test_runner.py`
- Modify: `tests/test_orpheus_bench_cli.py`
- Modify: `tests/test_performance_regression_report.py`

**Step 1: Define one versioned result envelope**

```python
@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    schema_version: Literal[4]
    run_id: str
    mode: str
    runtime: RuntimeIdentity
    input: InputIdentity
    timings: TimingMetrics
    throughput: ThroughputMetrics
    memory: MemoryMetrics | None
    quality: QualityMetrics
    artifacts: ArtifactManifest
```

Create explicit loaders and serializers for legacy wrapper v1 and Nagel v3.
The importable core uses v4. Documented compatibility commands continue to
write their historical file names/shapes alongside the v4 envelope until their
consumers and documentation have an explicit migration; do not silently replace
a documented JSON contract.

**Step 2: Write round-trip and compatibility tests**

Test JSON serialization, unknown future-version rejection, v1/v3 loading, stable
comparison, missing metrics, and deterministic report ordering.

**Step 3: Move benchmark core**

Move run orchestration, metrics, compare/report, and presets into
`orpheus/bench`. The module receives config values; it does not mutate process
environment to communicate configuration internally.

**Step 4: Reduce scripts to wrappers**

Preserve documented script flags. Each wrapper maps args to the importable core
and prints/delegates. `benchmarks/` and `tools/` must no longer import from
`scripts`.

**Step 5: Add discoverable CLI commands**

Support:

```bash
python -m orpheus bench run ...
python -m orpheus bench compare BASELINE CURRENT
python -m orpheus bench report RESULT
python -m orpheus bench preset quick
python -m orpheus bench preset policy
python -m orpheus bench preset tts-smoke
```

Keep existing commands as aliases where documented.

**Step 6: Run tooling tests**

```bash
python -m pytest tests/bench tests/test_orpheus_bench_cli.py tests/test_performance_regression_report.py tests/test_feedback_loop_runner.py -q -o addopts=
python -m orpheus bench --help
python scripts/benchmark_nagel_pipeline.py --help
```

Assert no `benchmarks -> scripts` or `tools -> scripts` import in a tooling
boundary test.

**Step 7: Commit**

```bash
git add orpheus/bench scripts/benchmark_nagel_pipeline.py scripts/benchmark_nagel_repeated.py scripts/performance_regression_report.py benchmarks/kokoro_single_book tools/feedback_loop_runner.py tests/bench tests/test_orpheus_bench_cli.py tests/test_performance_regression_report.py tests/test_feedback_loop_runner.py
git commit -m "refactor(bench): make benchmark core importable"
```

### Task 20: Make pytest the single Python test execution model and add incremental typing

**Files:**
- Modify: `run_tests.py`
- Modify: `pytest.ini`
- Modify: `requirements-dev.txt`
- Create: `mypy.ini`
- Create: `config/mypy_targets.txt`
- Create: `scripts/check_types.py`
- Modify: `scripts/hooks/pre-push`
- Modify: `.github/workflows/source-validation.yml`
- Modify: `docs/TESTING.md`
- Modify: the 26 `tests/test_*.py` files with direct-script runners
- Create: `tests/test_test_suite_contract.py`
- Create: `tests/test_mypy_targets.py`

**Step 1: Characterize test selection**

Record `pytest --collect-only` node IDs and counts for smoke and contract lanes.
Write a test that every smoke marker selects at least one test and that no
documented lane exits 5 because it is empty.

**Step 2: Replace direct-script execution**

Convert script-only assertions/print flows into ordinary pytest tests and
fixtures. Remove `main()` and `if __name__ == "__main__"` from automated test
modules.

Move genuine manual/hardware diagnostics to `tools/` or benchmark presets with
clear names; do not keep them disguised as automated tests.

**Step 3: Simplify `run_tests.py`**

Use pytest markers/node IDs for both smoke and contract modes. Remove the
`SCRIPT` execution mode and the manually maintained distinction between direct
scripts and pytest.

Either add real UI-marked Python tests or remove the permanently empty `ui`
Python marker lane from runner/docs. Frontend UI behavior remains in Vitest.

**Step 4: Add mypy as an expansion ratchet**

Start with:

```ini
[mypy]
python_version = 3.12
warn_unused_ignores = True
warn_redundant_casts = True
no_implicit_optional = True
check_untyped_defs = True

[mypy-modules.contracts.*]
disallow_untyped_defs = True
disallow_any_generics = True

[mypy-modules.app.jobs]
disallow_untyped_defs = True

[mypy-modules.app.operation_coordinator]
disallow_untyped_defs = True

[mypy-modules.setup.*]
disallow_untyped_defs = True
```

Add every newly created architectural Python module from Tasks 3-19. Do not
attempt a whole-repository strict conversion in one change.

**Step 5: Make one checked-in target manifest authoritative**

`config/mypy_targets.txt` contains one path per line and at minimum:

```text
modules/contracts
modules/web/schemas
modules/app/jobs.py
modules/app/operation_coordinator.py
modules/app/services/conversion_runtime.py
modules/app/services/conversion_job_runner.py
modules/app/services/source_intake_service.py
modules/app/services/library_catalog_service.py
modules/app/services/library_deletion_service.py
modules/app/services/runtime_setup_service.py
modules/setup
modules/runtime_failures.py
modules/pipeline/worker_policy.py
modules/pipeline/pool_lifecycle.py
modules/pipeline/parallel_run_tracker.py
modules/pipeline/synthesis_scheduler.py
modules/pipeline/synthesis_result_accumulator.py
modules/tts/runtimes/mlx_synthesizer.py
modules/tts/cleaner_backends
modules/tts/memory_pressure.py
modules/ingestion/registry.py
modules/ingestion/epub_navigation.py
modules/ingestion/epub_section_extractor.py
orpheus/bench/models.py
orpheus/bench/runner.py
```

`scripts/check_types.py` validates that every target exists, rejects duplicate
or broad repository-root entries, and invokes `python -m mypy` with exactly
that list. `tests/test_mypy_targets.py` compares the manifest with the
Create-files inventory in this plan (excluding tests, docs, frontend, and
non-Python files) so a new architectural module cannot silently miss the
ratchet.

**Step 6: Add CI and pre-push gates**

Run:

```bash
python scripts/check_types.py
```

CI, pre-push, and final acceptance all call this command; they must not repeat a
shorter hand-maintained path list.

**Step 7: Fix the coverage comment and ratchet**

`pytest.ini` currently enforces 85 while its comment refers to an older 69
threshold. Correct the comment. Set the threshold just below measured coverage;
never raise it above the current verified result.

**Step 8: Run all test-tooling checks**

```bash
python run_tests.py
python run_tests.py --contract
python scripts/check_types.py
python -m pytest tests/test_mypy_targets.py -q -o addopts=
python -m ruff check .
```

Expected: one Python execution model, no empty documented lane, no duplicate
marker expression in runner/hook, and green incremental typing.

**Step 9: Commit**

```bash
git add run_tests.py pytest.ini requirements-dev.txt mypy.ini config/mypy_targets.txt scripts/check_types.py scripts/hooks/pre-push .github/workflows/source-validation.yml docs/TESTING.md tests/test_test_suite_contract.py tests/test_mypy_targets.py
git commit -m "test(tooling): unify pytest lanes and ratchet typing"
```

### Task 21: Remove shims, finish documentation, and run release acceptance

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/CURRENT_STATE.md`
- Modify: `docs/architecture/python-boundaries.md`
- Modify: `docs/architecture/desktop-boundaries.md`
- Modify: `docs/architecture/tooling-boundaries.md`
- Create: `docs/architecture/decisions/0001-local-service-topology.md`
- Create: `docs/architecture/decisions/0002-static-api-contracts.md`
- Create: `docs/architecture/decisions/0003-operation-coordination.md`
- Create: `docs/architecture/decisions/0004-runtime-and-worker-policy.md`
- Create: `docs/architecture/decisions/0005-source-checkout-setup.md`
- Modify/delete: transitional re-exports and compatibility shims identified below

**Step 1: Inventory transitional code**

Run:

```bash
rg -n 'compatib|deprecated|legacy|__getattr__|re-export|forward' modules scripts tools desktop/src desktop/src-tauri/src
python -m vulture apps modules scripts tools tests vulture_whitelist.py --min-confidence 80 --sort-by-size
```

Classify every hit as:

- documented external compatibility: keep with removal policy;
- repository-internal shim with no caller: delete;
- test-only patch point: migrate test and delete;
- false positive: document in whitelist.

Specifically inspect:

- old conversion-option spec builders;
- `LibraryService` façade;
- `TTSEngine`/legacy `TTSConfig` exports;
- pipeline cleaner-memory forwarding module;
- setup script private wrappers;
- source-extension re-export;
- single-consumer `pool_*` files;
- old benchmark schemas and script imports;
- controller private compatibility methods.

**Step 2: Prove each deletion**

For each shim:

1. `rg` production, tests, docs, and scripts;
2. delete or migrate caller;
3. run the focused test;
4. commit with the owning workstream or one narrow cleanup commit.

Do not batch unrelated removals merely because vulture reported them together.

**Step 3: Write architectural decisions**

Each ADR records context, decision, alternatives, consequences, and enforcement
test. Update "Where to Change X" tables with final file paths.

`CURRENT_STATE.md` stays short and operational. Stable rationale belongs in
ADRs, not duplicated narrative.

**Step 4: Verify success metrics**

Run static checks:

```bash
python -m pytest tests/test_architecture_boundaries.py tests/test_openapi_contract.py tests/test_source_type_manifest.py -q -o addopts=
rg -n '^from (scripts|tools)|^import (scripts|tools)' modules
rg -n 'service\._|cleaner\._|engine\._' modules/app modules/tts
```

Expected:

- zero production import of scripts/tools;
- architecture debt allowlist empty;
- no command object calling service private methods;
- no cleaner backend mutating facade-private state;
- no TTS import of pipeline;
- generated TypeScript transport file clean.

**Step 5: Run complete source verification**

```bash
python -m ruff check .
python run_tests.py --contract
python scripts/check_types.py
(cd desktop && npm run api:generate && git diff --exit-code -- src/types/generated/openapi.ts)
(cd desktop && npm run typecheck && npm test -- --run && npm run build)
(cd desktop/src-tauri && cargo fmt --check && cargo test --locked -- --test-threads=1 && cargo check --locked && cargo check --locked --release)
```

**Step 6: Run profile and packaged acceptance**

```bash
python run_tests.py --verify-profile base
python run_tests.py --verify-profile mlx --allow-skipped-profiles
python run_tests.py --verify-profile cuda --allow-skipped-profiles
(cd desktop && ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build)
```

Run installed/package smoke where the host supports it. Record actual skips.

**Step 7: Compare performance**

Re-run the Task 1 measurements with the same concrete inputs and hashes:

```bash
mkdir -p .artifacts/architecture-modernization/current
python tools/architecture_baseline.py source --books-dir .artifacts/architecture-modernization/corpus --manifest benchmarks/stress_corpus/manifest.tsv --modes parse,gate --output .artifacts/architecture-modernization/current/source.json
python tools/architecture_baseline.py runtime --source tests/fixtures/benchmark_suite/short_chapter.md --profile mlx --repetitions 3 --chapter-limit 1 --output .artifacts/architecture-modernization/current/runtime-mlx.json
python tools/architecture_baseline.py runtime --source tests/fixtures/benchmark_suite/short_chapter.md --profile cuda --repetitions 3 --chapter-limit 1 --output .artifacts/architecture-modernization/current/runtime-cuda.json
python tools/architecture_baseline.py worker --source .artifacts/architecture-modernization/corpus/frankenstein.epub --workers 1,2,4 --repetitions 3 --output .artifacts/architecture-modernization/current/worker.json
(cd desktop && ORPHEUS_PERF_OUTPUT=../.artifacts/architecture-modernization/current/library.json npm test -- --run src/features/library/libraryPerformance.test.tsx)
(cd desktop && ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build)
python tools/architecture_baseline.py package --bundle-root desktop/src-tauri/target/release/bundle --expected-profile base --output .artifacts/architecture-modernization/current/package-base.json
python tools/architecture_baseline.py compare --baseline-dir .artifacts/architecture-modernization/baseline --current-dir .artifacts/architecture-modernization/current --max-control-regression-percent 5 --json-output .artifacts/architecture-modernization/comparison.json --markdown-output .artifacts/architecture-modernization/comparison.md
```

The comparison covers:

- parse/clean throughput;
- conversion orchestration latency;
- TTS time-to-first-audio and RTF;
- peak host/device memory;
- worker startup/reuse/shutdown;
- library request/render counts;
- packaged startup and bundle size;
- artifact validity/durations/metadata.

No unexplained control-path regression over 5%. Hardware results use median and
spread. Any accepted regression requires a documented trade-off and user
approval. A skipped profile remains a recorded gap and requires the matching
hardware lane before release.

**Step 8: Commit final docs and cleanup**

```bash
git add docs/ARCHITECTURE.md docs/CURRENT_STATE.md docs/architecture/python-boundaries.md docs/architecture/desktop-boundaries.md docs/architecture/tooling-boundaries.md docs/architecture/decisions/0001-local-service-topology.md docs/architecture/decisions/0002-static-api-contracts.md docs/architecture/decisions/0003-operation-coordination.md docs/architecture/decisions/0004-runtime-and-worker-policy.md docs/architecture/decisions/0005-source-checkout-setup.md
git commit -m "docs(architecture): complete modernization record"
```

Stage each proven shim deletion by its exact path in Step 2 before this commit;
never use a repository-wide `git add` for final cleanup.

## Final definition of done

- User-facing HTTP, SQLite, artifact, desktop lifecycle, and documented CLI
  behavior remains compatible or receives an explicitly tested additive
  improvement.
- The complete production dependency graph is enforced and has no debt
  allowlist.
- Conversion and setup transport contracts are static; TypeScript HTTP types are
  generated and drift-checked.
- Conversion job, operation reservation, pool lifecycle, parallel run, library
  request, and TTS session state each have one explicit owner.
- `ConversionService`, commands, controller, cleaner backends, and TTS helpers
  interact through public typed contracts rather than private-field reach-ins.
- Worker selection is one deterministic, explainable policy.
- TTS no longer imports pipeline.
- On-demand memory pressure remains measured; unused background/idle monitor
  lifecycle is gone.
- Packaged builds omit source-checkout setup supervision.
- Source types and parser dispatch have one canonical definition.
- Benchmarks use one versioned result envelope and importable core.
- Pytest is the only automated Python test execution model.
- New architectural modules are under static typing, with an explicit expansion
  ratchet.
- Full source, profile, packaged, and performance evidence is recorded.

## Execution order and checkpoints

Implement in numbered order within each dependency chain:

```text
1 -> 2 -> 3 -> 4 -> 5
              |    |
              |    +-> 18
              +-> 17

5 -> 6 -> 7 -> 8
6 -> 9 -> 10
6 -> 11 -> 12 -> 13 -> 14 -> 15 -> 16
4 -> 17
5 -> 18
1 -> 19
all -> 20 -> 21
```

Required review checkpoints:

1. after Task 5: contracts and dependency enforcement;
2. after Task 8: application concurrency and storage safety;
3. after Task 10: setup/package behavior;
4. after Task 13: worker selection and pool lifecycle;
5. after Task 16: TTS/cleaner/memory behavior and hardware evidence;
6. after Task 18: frontend/ingestion behavior;
7. after Task 21: full acceptance.
