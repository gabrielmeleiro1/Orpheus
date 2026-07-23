# Red-Team Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Safely complete and verify the current Orpheus hardening patch while finding and fixing additional high/medium edge-case defects.

**Architecture:** Keep the existing repository, FastAPI, ingestion, and React boundaries. Tighten behavior with small guards at the owning layer and regression tests at the public contract. Quarantine only explicitly corrupt SQLite databases; preserve data on all ambiguous failures.

**Tech Stack:** Python 3.12, SQLite, FastAPI/Pydantic, pytest, React 18, TypeScript, Vitest, Tauri/Rust.

---

### Task 1: Make SQLite quarantine corruption-specific

**Files:**
- Modify: `tests/test_storage_repository.py`
- Modify: `modules/storage/sqlite_repo.py`

**Step 1: Write failing tests**

Add tests proving that:

- a transient `sqlite3.OperationalError("database is locked")` is re-raised and
  the healthy database remains in place with its book;
- explicit `SQLITE_CORRUPT`/`SQLITE_NOTADB` still triggers recovery;
- an existing same-timestamp quarantine is not overwritten;
- a sidecar move failure never deletes the sidecar.

**Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest --no-cov tests/test_storage_repository.py -q
```

Expected: the transient-error and collision/sidecar safety tests fail against
the current broad `DatabaseError` handler.

**Step 3: Implement the minimal fix**

Classify corruption from `sqlite_errorcode`, re-raise every other database
exception, allocate unique quarantine paths, and preserve sidecars if a move
fails.

**Step 4: Verify GREEN**

Run the focused storage repository test command again and expect all tests to
pass.

### Task 2: Add missing API regression coverage

**Files:**
- Modify: `tests/test_web_api.py`
- Modify only if a test exposes a defect:
  - `modules/web/server.py`
  - `modules/web/routes/conversions.py`
  - `modules/web/routes/library.py`
  - `modules/web/routes/options.py`
  - `modules/web/routes/voices.py`
  - `modules/contracts/conversion_runtime_options.py`

**Step 1: Write tests for the claimed fixes**

Cover NaN/Infinity rejection and JSON-safe 422 bodies, conversion speed bounds,
OpenAI and warmup error translation, and extensionless source messages.

**Step 2: Verify RED or existing behavior**

Run the individual tests. If a test passes immediately, retain it as missing
regression coverage but do not claim a new red-green fix. If it fails for the
intended behavior, implement the smallest owning-layer correction.

**Step 3: Verify focused API tests**

```bash
.venv/bin/python -m pytest --no-cov tests/test_web_api.py tests/test_pipeline.py -q
```

### Task 3: Red-team storage and ingestion boundaries

**Files:**
- Modify tests under `tests/test_storage_*.py`,
  `tests/test_document_normalizer.py`, `tests/test_epub_parser.py`, and
  `tests/test_pdf_artifact_normalizer.py`
- Modify production files only for confirmed high/medium defects.

**Step 1: Probe adversarial cases**

Exercise symlink and traversal boundaries, ambiguous/unclaimed audio, retention
races, repeated prune operations, pathological regex inputs, verse/back-matter
classification, overlong content, and unusual TOCs.

**Step 2: For each confirmed defect, follow RED/GREEN**

Add one minimal failing test, run it to confirm the expected failure, implement
one minimal fix, and rerun the focused subsystem tests.

**Step 3: Run focused subsystem gates**

```bash
.venv/bin/python -m pytest --no-cov \
  tests/test_storage_migrations.py tests/test_storage_repository.py \
  tests/test_storage_maintenance.py tests/test_library_service.py \
  tests/test_document_normalizer.py tests/test_epub_parser.py \
  tests/test_pdf_artifact_normalizer.py -q
```

### Task 4: Red-team desktop state and input boundaries

**Files:**
- Modify tests under `desktop/src/`
- Modify production TypeScript only for confirmed high/medium defects.

**Step 1: Probe repeated actions and state transitions**

Cover same-frame start/paste actions, active-job and disconnected states,
numeric extremes, queue failures, and tab transitions while jobs change state.

**Step 2: Apply RED/GREEN for confirmed defects**

Use focused Vitest files for each cycle.

**Step 3: Run desktop checks**

```bash
cd desktop
npm run typecheck
npm test -- --run
npm run build
```

### Task 5: Final release-relevant verification

**Step 1: Inspect the final diff**

Run `git diff --check`, review every changed production file, and verify no
unrelated user change was reverted.

**Step 2: Run the Python and static gates**

```bash
.venv/bin/python -m ruff check .
.venv/bin/python run_tests.py --contract
```

**Step 3: Run desktop/Tauri checks**

```bash
cd desktop
npm run typecheck
npm test -- --run
npm run build
cd src-tauri
cargo test
cargo check
```

**Step 4: Report**

List fixed high/medium findings with reproductions and tests, remaining low-risk
items, exact verification results, and an updated release-readiness verdict.
