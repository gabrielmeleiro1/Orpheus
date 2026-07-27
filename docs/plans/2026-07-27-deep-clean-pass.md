# Deep Clean Pass — Plan (2026-07-27)

**Goal:** Remove complexity from the codebase without changing behavior: delete
dead and superseded mechanisms, collapse duplicated logic, decompose the
largest modules, and bring documentation back in line with the code.

**Not the goal:** Bug hunting (the 2026-07-24 audit covered that), feature
work, performance work, or cosmetic churn. Every change here must be
behavior-preserving and provable by the existing gates.

**Baseline:** Branch `feature/full-breaking-fix-pass` after the 2026-07-27
simplification commits. All gates green at baseline: Ruff clean; full Python
suite 1915 passed / 5 skipped (86.68% coverage); contract gate passed;
frontend typecheck + 462 Vitest tests + production build passed; Rust fmt +
tests + check passed.

---

## Context: what the 2026-07-27 pass already did

Three Opus reviewers swept the full branch diff (~28.5k added lines, 205
files) for removable complexity; verified WORTH-DOING findings were applied
and gated the same day:

- **Rust/native:** deleted the `backend_windows_creation_flags` passthrough
  (the Windows-target dead-code warning) and the two `backend_tests.rs` tests
  that were verbatim duplicates of `setup_tests.rs` tests over the same
  aliased type; merged the byte-identical
  `validate_existing_contained_dir`/`_file` pair in `paths.rs` into one
  function and pointed `validate_qualification_runtime_tree` at
  `DesktopRuntimePaths` instead of re-deriving the layout from string
  literals; removed the `Check Tauri shell` step from
  `release-artifact-validation.yml` (subsumed by the release build in the
  same job; `cargo check` stays in source-validation, which is what catches
  non-test-cfg dead code).
- **Python:** deleted the dead `read_signing_state` injection point in
  `tools/release_qualification/artifact.py` (its fabricated `CodesignInfo`
  would have bypassed Developer-ID rejection if ever wired up); collapsed the
  lockstep `_book_cleanup_ids`/`_book_cleanup_owners` pair in
  `conversion_service.py` into one dict; removed dead code in
  `scripts/release_qualification.py` (`execute_evidence_finalization`,
  unused `dmg_path` param, shadowed `shutil` import, `_file_sha256` clone);
  deleted dead `_cancel_idle_unload` in `tts_session.py`; deleted dead
  `PS_COLUMNS`, `ProcessSample.identity()`, and the `exe_path` alias in
  `macos_processes.py`; removed the `checklist_blocks_release` wrapper and
  the never-overridden injection params of `compute_toolchain_fingerprint`;
  deduplicated the triplicated app-bundle validation and doubled
  capability-fetcher closures in `scripts/desktop_runtime_smoke.py`; dropped
  the redundant EPUB except-branch in `modules/ingestion/preflight.py`.
- **Frontend:** replaced the byte-identical inline reduced-motion/visibility
  effects in `ConversionFlowCanvas`/`NowPlayingCanvas` with shared hooks;
  deleted dead `noteProbe` from `runtimeCapabilityPolling.ts`; removed the
  never-rendered `bookSummaryExtra` prop chain; deleted
  `currentListContextRef` (always equal to `reloadContextIdentityRef`);
  removed `reconciliationRequestSequenceRef` from `useEventStream.ts`
  (provably subsumed by `refreshGenerationRef`); converted
  `useParallelWorkers` status/error to pure derivations.

Everything below was **found but deliberately deferred** — either it needs a
product decision, more careful test work, or belongs in a structural pass.

---

## Workstream 1: Verified simplifications, ready to apply

These were traced and grep-verified by the 2026-07-27 reviewers but not
applied. Each is a small, self-contained commit.

### Frontend

1. **`useLibraryList.ts` selection-ownership triple guard, written out 8×**
   (~lines 499–513, 680–714, 736–752, 774–789). The identical 5-line
   predicate (`!isCurrentX() || selectedBookIdRef.current !== targetBookId ||
   selectionGenerationRef.current !== targetSelectionGeneration`) should fold
   into a sibling of `beginOwnedRequest` taking `targetBookId` explicitly.
   Two constraints from the trace: don't read `selectedBookIdRef.current`
   inside the helper (callbacks capture the state value, and the ref can lead
   by one render), and keep both the book-id and generation clauses
   (`selectRelative` advances the ref without bumping the generation).
   Safety net: `useLibrary.test.tsx:421, 478, 534, 563, 599, 634`. ~30 LOC.
2. **`openSelectedBookFolder` / `repairSelectedBook` are the same function
   twice** (`useLibraryList.ts:486–520` vs `762–794`). After item 1, extract
   `runEntityAction(call, onSuccess, fallbackMessage)`; both callbacks become
   ~6 lines. ~25 LOC.
3. **`pasteRequestRef` in `useConversionSourceQueue.ts`** (~lines 48, 68, 77,
   303, 314, 342, 349) is subsumed by `isCurrentSourceContext` +
   `pasteInFlightRef` (at most one paste in flight per context; the ref bumps
   on exactly the events the context fence already covers). Apply only with a
   targeted rerun of `useConversionSourceQueue.test.tsx:644, 698, 787` — this
   touches the paste-race area the audit hardened. ~9 LOC.
4. **`initialProbe` on the capability poller** has no production caller;
   tests pass full capability fixtures and can use `initialCapabilities`
   instead. Test churn only. ~16 LOC.
5. **Small ones:** the always-true third clause of `isCurrentRequest` in
   `useConversionQueueController.ts:137–144` (~2 LOC); a
   `memoWhenHidden(Component)` factory replacing the two visibility wrappers
   + two comparators in `App.tsx:63–84` / `visibilityMemo.ts` (~12 LOC); the
   redundant generation check next to the identity check in
   `playbackPersistence.ts:106–109` (~5 LOC).

### Python

6. **`modules/web/routes/setup.py:538–560` capability-cache resolution**:
   the `_runtime_capability_service` fallback is behavior-neutral and the
   `elif cache.probe_service is not shared` reconstruction branch (which
   reaches into private `cache._ttl_seconds`/`_clock`) is unreachable now
   that server state starts as `None` and controller identity is stable.
   Collapse 21 lines to ~8. Toucher: `tests/test_setup_route.py:738–757`.
7. **`pool_manager.py` shutdown duplication**: the thread-executor branch in
   `shutdown()` (~801–856) re-implements `_shutdown_executor_bounded`
   (183–213) including the same thread name; an `on_quiesced` callback on the
   existing helper removes the second implementation. Also: the 4-line
   barrier-release block appears twice (269–272, 289–292), and
   `_abort_parallel_run` clears the current job a second time right after
   `_stop_executor_after_cancellation` already did.
8. **Small ones:** `_utc_now_iso` duplicated verbatim in
   `tools/release_qualification/model.py` and `commands.py`;
   `_parse_version_tuple` duplicated between `artifact.py` and `preflight.py`
   (differing only in the raised subclass); `api_rate_limit.py`'s
   `self._manager` is `None` forever, making its stat branches and
   `shutdown()` body unreachable (keep `_invoke_acquire`/`_acquire_succeeded`
   — test-pinned); `packaging.py:122–126` computes a sanitized title it may
   throw away.

### Rust / native

9. **`resolve_sidecar_path` carries four candidates that can never match**
   (`backend.rs` ~700–733): the `dir.join(&filename).join(&filename)` shape
   duplicates the `sidecar_dir_name` candidates on Unix and searches a
   directory literally named `*.exe` on Windows, which PyInstaller never
   emits. Removing them also shortens the not-found error. ~16 LOC;
   `backend_tests.rs:457–509` cover the surviving candidates.
10. **The refresh trio in `main.rs`** (`refresh_backend_ready_from_file` →
    `refresh_backend_process_exit` → `refresh_backend_startup_deadline`)
    appears verbatim 4×; one `refresh_backend_state()` helper makes the one
    deviating call site (`restart_backend`) visibly deliberate. ~9 LOC.

## Workstream 2: Decisions needed (mechanism removals)

Each of these deletes a *working but arguably redundant* defensive mechanism.
Decide explicitly; don't let an agent decide.

1. **Windows `taskkill /T /F` fallback** (`setup.rs:1371–1411`). The Job
   Object with `KILL_ON_JOB_CLOSE`, no-breakaway, and assignment-before-
   resume provably kills the whole tree atomically; the PID-tree walk is the
   weaker superseded mechanism (misses reparented grandchildren, races PID
   reuse). Removing it deletes ~40 LOC **including the
   `windows_taskkill_path_is_absolute_system32_path` regression test** (a
   PATH-hijack guard for the mechanism itself). Counter-argument: Windows is
   not natively testable here, and "Windows shutdown kills descendants" is a
   named regression target. Recommendation: remove only when a Windows-native
   CI lane can exercise the Job-Object kill end-to-end; until then it is
   belt-and-braces on an untestable platform.
2. **`--validate-existing` in `scripts/build_desktop_backend.py`**
   (~141–146, 495–524, 1113–1120). Superseded as a build step by the
   `beforeBuildCommand` migration; no automated caller; documented in
   `RUN_APPLICATION.md` as a manual diagnostic. Removing it unlocks deleting
   the marker re-verification (478–492) that re-reads a file written from the
   same variable ten lines earlier. ~60 prod LOC + ~55 test LOC. Decide:
   keep as a documented diagnostic, or delete and update the doc.
3. **Release workflow re-runs the full source gate**
   (`release-artifact-validation.yml`: ruff, contract, typecheck, npm test,
   cargo test). `source-validation.yml` fires on every push including tags,
   so the same SHA runs everything twice. Removing the duplicates (~17 LOC)
   means a release tag could go green while source-validation is red on the
   same commit — acceptable only if branch protection already requires
   source-validation. Decide based on the intended GitHub publishing setup.

## Workstream 3: Structural decomposition

The audit passes grew several modules past the point where ownership is easy
to hold in your head. Decompose along existing seams; pure moves plus the
minimum re-exports, no behavior change. Largest first:

| File | Lines | Natural seams |
| --- | --- | --- |
| `modules/storage/maintenance.py` | 1043 | prune / repair / preview / diagnostics are near-independent |
| `modules/app/services/conversion_service.py` | 954 | reservation logic vs job lifecycle vs cleanup |
| `desktop/src/features/setup/SetupPage.tsx` | 929 | step components already exist implicitly |
| `desktop/src/features/library/useLibraryList.ts` | 913 | list loading vs selection ownership vs entity actions vs maintenance (Workstream 1 items 1–2 shrink it first) |
| `modules/pipeline/pool_manager.py` | 882 | executor lifecycle vs cancellation vs metrics (item 7 first) |
| `modules/tts/vram_monitor.py` | 850 | sampling vs policy vs reporting |
| `modules/web/routes/setup.py` | 839 | route handlers vs capability cache (item 6 first) |
| `desktop/src/api/client.ts` | 784 | resource groups (library / conversion / setup / media) |

Rule: decomposition commits contain **zero** logic edits — reviewers must be
able to verify them as moves. Apply Workstream 1 shrinkers before splitting
the same file.

## Workstream 4: Docs and repo hygiene

1. **`docs/CURRENT_STATE.md` is a month stale** (last commit 2026-06-27,
   ~108 branch commits behind). At least two claims are now wrong: library
   loads no longer eagerly prune/fetch (on-demand paging replaced the 25k
   fetch), and the setup/capability flow changed. Rewrite against the code,
   or shrink it to a pointer at ARCHITECTURE.md + the boundary maps so it
   can't drift this way again.
2. **`docs/plans/CODEBASE_CLEANUP_PLAN.md` is a byte-identical untracked
   duplicate** of the committed `docs/CODEBASE_CLEANUP_PLAN.md`. Delete the
   untracked copy (verify `diff -q` first; it was identical on 2026-07-27).
3. **`docs/plans/` is gitignored (line 31) yet contains committed files**
   (e.g. the 2026-07-24 audit record was force-added). Pick one convention:
   either un-ignore the directory or stop committing into it; the current
   split makes `git status` lie about what's recorded.
4. Sweep `docs/` for references to removed mechanisms after Workstream 2
   decisions (`--validate-existing` in RUN_APPLICATION.md, etc.).

## Workstream 5: Test hygiene

1. The full Python suite leaves a multiprocessing resource-tracker child
   alive at session end (conftest reports it every run). Track down which
   fixture leaks it; the conftest guard hides rather than fixes it.
2. `pytest -m ui` selects zero tests and exits 5 (recorded in the audit as a
   limitation). Either remove the marker lane from `run_tests.py`/docs or
   add the intended tests; a permanently-empty gate invites "it passed"
   misreads.
3. While removing `initialProbe` (Workstream 1, item 4), migrate its tests
   to the public surface rather than deleting coverage.
4. Latent orphan-timer nit found during review: `scheduleNext` in
   `runtimeCapabilityPolling.ts` (~126–138) overwrites `timer` without
   clearing. Harmless today (only called after fire/stop), but fix the
   one-liner if the poller ever grows a `pollNow()`.

## Process

- Same discipline as the audit: one focused commit per item; behavior-
  preserving changes ride on existing tests (no new tests needed for pure
  removals, but never weaken one); anything touching a fence named in the
  audit's regression-target list gets an independent review before commit.
- Suggested ownership split (disjoint, mirrors the audit): frontend
  (Workstream 1 items 1–5, then `useLibraryList`/`client.ts`/`SetupPage`
  splits) / Python services+pipeline (items 6–8, then `maintenance.py`,
  `conversion_service.py`, `pool_manager.py` splits) / native+release
  (items 9–10 and Workstream 2 decisions) / docs+test hygiene (Workstreams
  4–5) — the last can run with any of the others.
- Gates per commit: the touched-area focused suite; per merge-point: Ruff,
  contract gate, full pytest, frontend typecheck + Vitest + build, cargo
  fmt/test/check. Re-run the packaged smoke only if Workstream 2 item 2 or
  any `scripts/build_desktop_backend.py` change lands.
- Expected end state: ~250–400 production LOC removed beyond the 2026-07-27
  pass, no file over ~600 lines in the hot paths, docs matching the code,
  and zero permanently-empty gates.
