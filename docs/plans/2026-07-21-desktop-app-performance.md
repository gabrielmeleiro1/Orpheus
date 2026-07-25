# Desktop App Performance Implementation Plan

> Execute with scoped TDD changes, review every agent diff, and keep conversion-pipeline behavior out of scope.

**Goal:** Make the desktop UI close promptly, consume less idle CPU, avoid unnecessary React work and startup requests, and remain responsive with large libraries.

**Architecture:** Native Tauri owns the bounded exit lifecycle; the Python desktop server proactively closes SSE on signal. React work is isolated at panel boundaries, startup reads are shared, and library records are incrementally fetched and windowed.

**Stack:** Rust/Tauri 2, Python/FastAPI/Uvicorn, React 18/TypeScript, Vitest, pytest.

---

## Task 1: Native and server shutdown

**Files:**
- Modify: `desktop/src-tauri/src/main.rs`
- Modify: `apps/launchers/desktop_backend.py`
- Test: `desktop/src-tauri/src/main.rs`
- Test: `tests/test_desktop_runtime_contract.py`

1. Add failing tests for a one-shot exit coordinator and for early event-broker closure plus explicit Uvicorn graceful timeout.
2. Implement immediate window hiding, first-exit prevention, off-thread cleanup, and final programmatic exit.
3. Extend `_DesktopReadyServer` with an idempotent early-shutdown callback invoked before Uvicorn connection draining.
4. Run `cargo test --manifest-path desktop/src-tauri/Cargo.toml` and the focused pytest file.

## Task 2: Suspend idle canvases

**Files:**
- Modify: `desktop/src/features/dashboard/ConversionFlowCanvas.tsx`
- Modify: `desktop/src/features/dashboard/NowPlayingCanvas.tsx`
- Modify: `desktop/src/features/dashboard/DashboardPanel.tsx`
- Test: dashboard canvas/component tests under `desktop/src/features/dashboard/`

1. Add failing RAF tests for hidden, paused/inactive, reduced-motion, and visible-active states.
2. Gate both loops on activity and document visibility; render exactly one static frame otherwise.
3. Reuse conversion diffusion buffers and avoid per-frame dataset churn where possible.
4. Run focused Vitest tests and TypeScript checks.

## Task 3: Paginate and window the library

**Files:**
- Modify: `desktop/src/features/library/libraryRepository.ts`
- Modify: `desktop/src/features/library/useLibraryList.ts`
- Modify: `desktop/src/features/library/LibraryPanel.tsx`
- Modify: library styles as required
- Test: existing library repository/hook/panel tests

1. Add failing tests showing the repository requests one page, `loadMore` appends without duplicates, and changed filters reset pages.
2. Return page metadata from the repository and expose `hasMore`/`loadMore` from the hook.
3. Add progressive loading and a bounded/windowed list in the panel without changing edit/delete/player behavior.
4. Run all library Vitest tests and TypeScript checks.

## Task 4: Consolidate startup work

**Files:**
- Modify: `desktop/src/app/usePrimaryRuntime.ts`
- Modify: `desktop/src/features/conversion/useTtsOptions.ts`
- Create: `desktop/src/app/runtimeBootstrapCache.ts`
- Modify: `desktop/src/features/events/useEventStream.ts`
- Modify: `desktop/src/features/conversion/useConversionJob.ts`
- Modify: `desktop/src/features/library/useLibraryList.ts`
- Modify: `desktop/src/runtime/backendLifecycle.ts`
- Test: colocated hook/model tests

1. Add failing tests for shared in-flight model/capability reads, a single initial active-job refresh, immediate first library load, and one-command endpoint polling.
2. Implement the shared rejected-promise-evicting cache.
3. Remove redundant refresh paths while retaining reconnect reconciliation.
4. Make the first library request immediate and reuse `backend_status.endpoint`.
5. Run focused tests and TypeScript checks.

## Task 5: Optimistic setup readiness

**Files:**
- Modify: `desktop/src/App.tsx`
- Create: `desktop/src/app/runtimeSetupReadiness.ts`
- Test: `desktop/src/App.test.tsx` and a colocated readiness test

1. Add failing tests for configured-cache dashboard boot, uncached lightweight checking state, and stale-hint correction.
2. Implement a versioned local hint with safe storage failure handling.
3. Keep the API response authoritative and preserve explicit setup-opening behavior.
4. Run the focused tests.

## Task 6: Isolate React panel updates

**Files:**
- Modify: `desktop/src/App.tsx`
- Modify: panel components and relevant hooks under `desktop/src/features/`
- Test: `desktop/src/App.test.tsx` and panel tests

1. Add render-count tests showing hidden expensive panels do not rerender for playback/progress/health changes.
2. Stabilize callbacks and memoize hook return objects where appropriate.
3. Add visibility-aware memo boundaries while preserving local panel/form state.
4. Retain health object identity when the meaningful status is unchanged.
5. Run focused tests and TypeScript checks.

## Task 7: Integration and verification

1. Review every agent diff for scope, lifecycle races, stale async responses, and UI behavior.
2. Run `python -m pytest tests/test_desktop_runtime_contract.py` plus affected Python suites.
3. Run `npm test -- --run`, `npm run typecheck`, and `npm run build` in `desktop/`.
4. Run `cargo test --manifest-path desktop/src-tauri/Cargo.toml`.
5. Inspect `git diff --check`, `git status`, and the final diff before reporting results.
