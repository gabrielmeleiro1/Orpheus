# Codebase Cleanup And Modularization Plan

## Purpose

This document is a high-level implementation plan for making Orpheus easier to
understand, change, benchmark, and verify. It is intentionally written for
subagents to execute the detailed analysis and implementation work in later
passes.

The goal is not cosmetic churn. The goal is a codebase with clear ownership
boundaries, smaller modules, stable public contracts, empirical performance
evidence, and fewer duplicated or drift-prone paths.

## Current High-Level Shape

Orpheus is a local-first audiobook conversion app with these broad areas:

- Desktop shell: Tauri, React, TypeScript, Vitest, and Rust launcher/runtime code.
- Web/API layer: FastAPI routes, desktop auth, uploads, SSE events, and route
  serializers.
- App services: conversion orchestration, library operations, voice services,
  runtime capability probing, and job lifecycle.
- Pipeline: document ingestion, cleaning, TTS synthesis, encoding, packaging,
  parallel execution, memory policies, and repair.
- Ingestion: PDF, EPUB, Markdown, text parsing, preflight, normalization, and
  non-prose filtering.
- TTS and cleaner runtimes: Kokoro, MLX, CUDA, API cleaner, runtime specs,
  options, voices, caching, and memory monitoring.
- Storage: SQLite repository, migrations, models, and maintenance.
- Developer tooling: benchmark scripts, feedback loops, sidecar builds, desktop
  smoke tests, git hooks, and the new `python -m orpheus bench` harness.

The cleanup should preserve app behavior while improving the internal shape.

## Non-Negotiable Constraints

- Do not change user-visible app behavior without an explicit test and rationale.
- Do not break existing API/frontend payload compatibility.
- Do not remove runtime options, benchmark switches, or sidecar paths unless a
  compatibility path exists.
- Do not commit generated benchmark artifacts, books, audio, model files, local
  databases, or temp output.
- Prefer small commits grouped by behavioral area.
- Every behavior-changing cleanup must include tests.
- Every performance-sensitive cleanup must include benchmark evidence.
- Keep direct pipeline benchmarks separate from API/desktop integration smoke
  benchmarks; they answer different questions.

## Success Criteria

A cleanup pass is successful when:

- Module boundaries are documented and enforced by tests or simple import checks.
- Shared contracts are centralized instead of duplicated across scripts, API,
  desktop helpers, and pipeline workers.
- Large modules have clear internal sections or are split into focused helpers.
- Benchmark commands produce stable JSON summaries that can compare branches.
- The Python contract, focused desktop checks, and benchmark smoke checks pass.
- Subagents can explain ownership of any touched files without relying on hidden
  context from this thread.

## Workstream 1: Architecture Inventory

### Goal

Create a concrete map of current boundaries before editing broadly.

### Subagent Tasks

1. Inventory Python package boundaries.
   - Inspect `modules/app`, `modules/web`, `modules/pipeline`,
     `modules/ingestion`, `modules/tts`, `modules/audio`, and
     `modules/storage`.
   - Produce a short dependency map: who imports whom, where cycles or
     near-cycles exist, and which modules behave like public contracts.
   - Output: `docs/architecture/python-boundaries.md`.

2. Inventory desktop/frontend boundaries.
   - Inspect `desktop/src`, `desktop/src-tauri`, Tauri command flow, and API
     client usage.
   - Identify duplicated frontend/backend assumptions, especially conversion
     options, runtime capabilities, upload limits, and cleaner/TTS metadata.
   - Output: `docs/architecture/desktop-boundaries.md`.

3. Inventory developer tooling.
   - Inspect `scripts/`, `tools/`, `benchmarks/`, git hooks, and desktop build
     helpers.
   - Identify duplicated CLI arguments, hardcoded backend choices, output path
     conventions, and benchmark/report formats.
   - Output: `docs/architecture/tooling-boundaries.md`.

### Acceptance Criteria

- Each inventory doc lists top-level ownership, public entry points, private
  helpers, and cleanup candidates.
- No production code changes are made in this workstream.

## Workstream 2: Contract Centralization

### Goal

Reduce drift by moving shared configuration and schemas into single sources of
truth.

### Candidate Areas

- Cleaner backend choices and CUDA quantization options.
- TTS runtime provider/model/device/quantization metadata.
- Conversion request payload shape shared by frontend, API routes, app services,
  worker serialization, and benchmarks.
- Benchmark summary schemas.
- Desktop/runtime capability response shape.
- Upload limits and accepted source extensions.

### Subagent Tasks

1. Find duplicated constants and schema fragments.
   - Search for repeated lists, option names, environment variables, and payload
     keys.
   - Classify each duplicate as harmless, test fixture, or drift risk.

2. Propose central contract modules.
   - Prefer existing modules when they already function as a source of truth.
   - Avoid creating a generic `constants.py` dumping ground.

3. Implement one narrow contract consolidation per commit.
   - Add failing tests that prove two or more consumers now read the same source.
   - Update docs only when the user-facing workflow changes.

### Acceptance Criteria

- Each consolidated contract has at least one test proving consumers stay in
  sync.
- CLI, API, worker, and frontend compatibility is preserved.

## Workstream 3: Pipeline Modularization

### Goal

Make the conversion pipeline easier to reason about without changing observable
output.

### Candidate Areas

- `ConversionPipeline` orchestration versus stage implementation.
- `ChapterProcessor` responsibilities around chunking, cleaning, synthesis,
  encoding, and artifacts.
- Stage timing and benchmark instrumentation.
- Parallel worker configuration and runtime serialization.
- Repair flow versus normal conversion flow.

### Subagent Tasks

1. Trace one successful conversion path.
   - Start at API/app service entry and separately at direct pipeline benchmark
     entry.
   - Produce a sequence diagram or numbered flow with file/function names.

2. Identify oversized modules and mixed responsibilities.
   - For each candidate, recommend either "leave", "section only", "extract
     helper", or "split module".
   - Prioritize files where tests already exist.

3. Improve stage metrics.
   - Ensure parse, clean, TTS, encode, package, overlap, warmup, and audio
     duration metrics are reliable across current Kokoro runtime paths.
   - Add benchmark tests before changing instrumentation.

4. Simplify repair and normal conversion shared logic.
   - Extract only when duplication is real and covered by tests.

### Acceptance Criteria

- Full conversion behavior remains compatible.
- Benchmark full mode reports useful TTS/audio throughput metrics.
- Existing pipeline tests pass, plus any new focused tests.

## Workstream 4: Ingestion And Cleaning Policy

### Goal

Make parsing, normalization, non-prose filtering, and cleaning decisions
observable and empirically testable.

### Candidate Areas

- PDF/EPUB/TXT/Markdown parser selection.
- Document normalization and content filtering.
- Cleaner gate heuristics and artifact-quality scans.
- Real-book regression cases stored as small synthetic excerpts, not copyrighted
  full text.

### Subagent Tasks

1. Audit parser output contracts.
   - Confirm each parser returns compatible document/chapter metadata.
   - Identify places where PDF-specific behavior leaks into generic code.

2. Add cleaning-policy benchmark cases.
   - Use synthetic excerpts for index, bibliography, TOC, copyright, prose,
     footnotes, OCR residue, and social boilerplate.
   - Add expected parse/clean summary assertions.

3. Improve observability.
   - Ensure skipped non-prose sections, artifact-quality issues, and cleaner
     gate decisions appear in benchmark summaries or trace artifacts.

### Acceptance Criteria

- `python -m orpheus bench suite ... --modes parse,clean` gives actionable
  policy evidence.
- False-positive risk is tested with normal prose containing citations, numbers,
  and chapter titles.

## Workstream 5: TTS Runtime And CUDA/MLX/CPU Clarity

### Goal

Keep runtime selection explicit, platform-aware, and testable.

### Candidate Areas

- Kokoro runtime default selection.
- CUDA availability and dependency checks.
- MLX-only paths versus package Kokoro paths.
- Runtime cache identity and model lifecycle.
- Sidecar packaging requirements.

### Subagent Tasks

1. Map runtime decision flow.
   - Start from frontend/API payload and follow it to `RuntimeSpec`, factory,
     strategy, and concrete runtime.

2. Find platform assumptions.
   - Audit Apple Silicon, Windows, CUDA, CPU fallback, sidecar packaging, and
     dependency docs.

3. Add or improve runtime probes.
   - Keep real GPU tests skip-guarded.
   - Use mocked CUDA for contract tests and real CUDA only in explicit hardware
     benchmark/smoke runs.

4. Extend benchmark evidence.
   - Add documented commands for CPU, MLX, and CUDA comparisons.
   - Ensure output summaries capture provider, device, model, quantization, and
     audio realtime factor.

### Acceptance Criteria

- A developer can explain what runtime will be selected on macOS Apple Silicon,
  macOS Intel, Windows CUDA, and CPU-only machines.
- Benchmark and API capability data agree on available options.

## Workstream 6: API, App Services, And Frontend Integration

### Goal

Keep the frontend, API, and app services aligned while reducing duplication and
hidden behavior.

### Candidate Areas

- Conversion start/repair/cancel lifecycle.
- Upload source handling and durable source paths.
- Progress event math and SSE behavior.
- Runtime capability endpoints and frontend option defaults.
- Desktop auth and media access.

### Subagent Tasks

1. Audit API payload contracts.
   - Compare route models, frontend request construction, app service kwargs,
     and worker config serialization.

2. Identify naming inconsistencies.
   - Focus on user-visible option names, provider names, cleaner/TTS labels, and
     progress stage names.

3. Add integration smoke cases where gaps exist.
   - Prefer API-level tests for payload compatibility.
   - Prefer desktop tests only for shell/sidecar behavior.

4. Avoid UI rewrites in cleanup passes.
   - Only change frontend layout or behavior when required by a contract fix.

### Acceptance Criteria

- Existing frontend behavior remains stable.
- API tests cover all conversion option shapes the frontend can send.
- Desktop shell tests still pass.

## Workstream 7: Storage And Artifacts

### Goal

Make persisted library state and generated artifact paths predictable and safe.

### Candidate Areas

- SQLite repository methods and migration history.
- Output root and temp root conventions.
- Packaged output metadata and playback paths.
- Cleanup/prune behavior.
- Benchmark output versus user output separation.

### Subagent Tasks

1. Map storage ownership.
   - Identify which layer owns source paths, output paths, playback metadata,
     and repair updates.

2. Audit artifact cleanup behavior.
   - Confirm cleanup only removes current-run or owned artifacts.
   - Check benchmark cleanup separately from user conversion cleanup.

3. Add path-safety tests if needed.
   - Include traversal, duplicate title, repeated run, and deleted source cases.

### Acceptance Criteria

- No cleanup path can delete user-owned source files unexpectedly.
- Repeated benchmark runs do not scan stale artifacts.
- Library metadata continues to round-trip.

## Workstream 8: Developer Tooling And Benchmark Consolidation

### Goal

Make developer commands discoverable and empirical.

### Candidate Areas

- `python -m orpheus bench`.
- Existing `scripts/benchmark_nagel_pipeline.py`.
- Repeated benchmark runner and performance regression report.
- `tools/feedback_loop_runner.py`.
- Desktop runtime smoke.
- Git pre-push hook.

### Subagent Tasks

1. Decide tooling ownership.
   - Keep compatibility wrappers where scripts are already documented.
   - Move reusable benchmark/report logic behind importable modules.

2. Add comparison/report command.
   - Candidate CLI shape:
     - `python -m orpheus bench compare BASELINE CURRENT`
     - `python -m orpheus bench report SUMMARY`

3. Add benchmark presets.
   - `quick`: small parse/clean.
   - `policy`: parser/cleaner policy fixtures.
   - `tts-smoke`: one bounded full run.
   - `real-local`: user-provided local book paths.

4. Document expected evidence.
   - Which commands to run before parser, cleaner, TTS, API, desktop, and
     storage changes.

### Acceptance Criteria

- Benchmark output is stable JSON.
- Reports compare branch-to-branch without manual spreadsheet work.
- Slow/full benchmarks remain opt-in.

## Workstream 9: Test Suite Hygiene

### Goal

Make validation fast, layered, and meaningful.

### Candidate Areas

- Contract tests versus focused tests versus benchmark/slow/GPU tests.
- Coverage thresholds and focused test ergonomics.
- Desktop Node/Vitest/Tauri paths.
- Generated fixture files that become dirty after hooks.

### Subagent Tasks

1. Map test tiers.
   - Document what each tier proves and what it does not prove.

2. Fix generated fixture dirtiness.
   - Identify why sample EPUB/PDF files become modified during hooks.
   - Make generation deterministic or isolate generated outputs.

3. Add focused commands to docs.
   - Parser/cleaner changes.
   - TTS/runtime changes.
   - API/frontend payload changes.
   - Storage/path changes.
   - Desktop sidecar changes.

### Acceptance Criteria

- Pre-push does not leave tracked files dirty.
- Developers can run focused checks without fighting global coverage flags.
- Full contract remains green.

## Workstream 10: Documentation And Onboarding

### Goal

Make the repo easier for a new contributor or subagent to navigate.

### Candidate Areas

- `README.md`.
- `docs/ARCHITECTURE.md`.
- `docs/TESTING.md`.
- `docs/DEPENDENCIES.md`.
- `docs/CURRENT_STATE.md`.
- New architecture inventory docs.

### Subagent Tasks

1. Update architecture docs after code cleanup, not before.
2. Add a "common tasks" section:
   - Run app.
   - Run API.
   - Run benchmark.
   - Build sidecar.
   - Run focused tests.
3. Add "where to change X" guidance:
   - Parser behavior.
   - Cleaner policy.
   - TTS runtime options.
   - Frontend conversion form.
   - API payloads.
   - Storage schema.

### Acceptance Criteria

- Docs match actual command behavior.
- No stale branch-specific or one-off investigation notes remain in primary docs.

## Suggested Execution Order

1. Architecture inventory only.
2. Test hygiene and generated fixture dirtiness.
3. Benchmark/report consolidation.
4. Contract centralization.
5. Pipeline metrics and modularization.
6. Ingestion/cleaning policy observability.
7. Runtime selection clarity.
8. API/frontend alignment.
9. Storage/artifact cleanup.
10. Documentation refresh.

This order gives subagents shared context and better measurement before deeper
refactors.

## Commit Strategy

Use small commits:

- `docs: map python architecture boundaries`
- `test: stabilize generated sample fixtures`
- `refactor: centralize conversion option contracts`
- `bench: add policy benchmark preset`
- `refactor: split pipeline stage metrics`
- `docs: update runtime selection guide`

Avoid commits that mix unrelated refactors, docs, behavior changes, and test
fixture updates.

## Verification Matrix

Run the minimum relevant checks for each workstream:

- Python contract:
  - `python run_tests.py --contract`
- Benchmark CLI:
  - `PYTEST_ADDOPTS='' /opt/anaconda3/bin/python -m pytest tests/test_orpheus_bench_cli.py -q -o addopts=''`
  - `python -m orpheus bench suite <small> <medium> --modes parse,clean`
- Parser/cleaner:
  - `python -m orpheus bench suite <policy-fixtures> --modes parse,clean`
- Full TTS:
  - `python -m orpheus bench full <small-source> --chapter-limit 1 --profile-tts-chunks`
- API:
  - focused web/API tests from `docs/TESTING.md`
- Desktop:
  - `cd desktop && npm run typecheck && npm test -- --run`
  - `cd desktop/src-tauri && cargo test && cargo check`

## Subagent Review Requirements

Every implementation workstream should end with two independent reviews:

1. Spec reviewer:
   - Confirms the work matches this plan and any task-specific plan.
   - Checks for missing tests, missing docs, and unintended scope expansion.

2. Code-quality reviewer:
   - Checks modularity, API compatibility, path safety, benchmark correctness,
     import behavior, and hidden coupling.

Do not merge cleanup work just because tests pass. Cleanup work must make the
codebase easier to understand or safer to change.

## Explicit Non-Goals For The First Cleanup Pass

- Rewriting the frontend UI.
- Replacing FastAPI, Tauri, SQLite, or the pipeline architecture wholesale.
- Removing local MLX/CUDA/API runtime choices.
- Making real full-book TTS benchmarks part of default validation.
- Committing copyrighted book fixtures or generated audio.
- Chasing every style preference without measurable maintainability value.
