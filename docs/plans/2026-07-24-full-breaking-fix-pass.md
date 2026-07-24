# Full Breaking-Fix Pass Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix every confirmed breaking issue from the July 24 audit and make the packaged desktop artifact—not only the source checkout—the release evidence.

**Architecture:** Keep the existing React/FastAPI/pipeline/Tauri boundaries. Add local request ownership and deterministic cleanup at each failing boundary, align all multiprocessing resources with the spawn pool, and make every Tauri package build produce a fresh architecture-matched sidecar. Preserve the current uncommitted conversion-panel work and integrate fixes without reverting it.

**Tech Stack:** Python 3.12, pytest/pytest-asyncio, React 18, TypeScript, Vitest/happy-dom, Rust 1.88, Tauri 2, GitHub Actions.

---

### Task 1: Make the global API limiter spawn- and cancellation-safe

**Files:**
- Modify: `modules/tts/api_rate_limit.py`
- Modify: `tests/test_cleaner_api_global_limiter.py`
- Test: `tests/test_pool_manager.py`

**Step 1: Write the Linux/spawn regression test**

Add a test that creates `GlobalAPILimiter`, passes its semaphore into a real
`multiprocessing.get_context("spawn")` child, and verifies the child can acquire
and release it. On Linux the current implementation must fail with the
fork-context `SemLock` error.

**Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/python -m pytest --no-cov \
  tests/test_cleaner_api_global_limiter.py::test_global_api_limiter_is_compatible_with_spawn_context -q
```

Expected: fail on Linux or fail an explicit context-construction assertion on
non-Linux hosts because `mp.BoundedSemaphore` is not created from the spawn
context.

**Step 3: Create the semaphore from the pool's context**

Use:

```python
context = mp.get_context("spawn")
self._semaphore = context.BoundedSemaphore(self.max_concurrency)
```

Keep the existing public `semaphore` contract.

**Step 4: Write the cancellation regression test**

Hold the only permit, start an `async with acquire_global_limiter_async(...)`
waiter, cancel it while blocked, release the holder, and assert another
non-blocking acquire succeeds. Also test that a false timed acquire raises
`TimeoutError` and never releases an unowned permit.

**Step 5: Run the cancellation tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest --no-cov \
  tests/test_cleaner_api_global_limiter.py -q
```

Expected: the cancelled waiter consumes the permit or the timed acquire corrupts
the bounded semaphore.

**Step 6: Implement cancellation-safe acquisition**

Extract a synchronous acquire adapter that normalizes supported call signatures
and returns only after ownership is confirmed. Run it in a task wrapping
`asyncio.to_thread`. Await it through `asyncio.shield`; if the coroutine is
cancelled, await the background acquisition, release any acquired permit exactly
once, then re-raise cancellation. Enter the context body only after ownership is
confirmed.

**Step 7: Verify GREEN and adjacent pool behavior**

Run:

```bash
.venv/bin/python -m pytest --no-cov \
  tests/test_cleaner_api_global_limiter.py tests/test_pool_manager.py -q
```

Expected: all pass with no leaked child or semaphore warning.

**Step 8: Commit only Task 1 files**

```bash
git add modules/tts/api_rate_limit.py tests/test_cleaner_api_global_limiter.py tests/test_pool_manager.py
git commit -m "fix(tts): make global API limiter spawn and cancellation safe"
```

### Task 2: Prevent stale TTS audio and runtime cache collisions

**Files:**
- Modify: `modules/tts/engine_output.py`
- Modify: `modules/tts/engine_runtime.py`
- Modify: `tests/test_tts_engine_modularization.py`
- Modify if the public facade contract needs coverage: `tests/test_tts.py`

**Step 1: Write the stale-output regression**

Pre-create `sample.wav` with one waveform. Make the injected generator emit only
`sample_000.wav` with a different waveform. Call
`synthesize_to_output_path(...)` and assert the returned and final audio are the
new waveform and no segment remains.

**Step 2: Verify stale-output RED**

Run:

```bash
.venv/bin/python -m pytest --no-cov \
  tests/test_tts_engine_modularization.py -k "stale_output_path" -q
```

Expected: current helper returns the pre-existing waveform.

**Step 3: Implement deterministic pre-cleaning**

Call the existing `_remove_stale_generated_files(output_path)` before generation.
After concatenating segments, remove the consumed segment files just as
`synthesize_to_output_file` does.

**Step 4: Write the cache-identity regression**

Create an unloaded engine whose config has a canonical bf16
`runtime_cache_key`, seed the cache with the bf16 model, then call
`load_model(quantization="4bit")`. Assert the effective cache key differs from
the bf16 key and the 4-bit loader runs.

**Step 5: Verify cache-identity RED**

Run:

```bash
.venv/bin/python -m pytest --no-cov \
  tests/test_tts_engine_modularization.py -k "quantization_override" -q
```

Expected: the cached bf16 object is returned while `_model_path` says 4-bit.

**Step 6: Align cache identity with the effective runtime**

When an explicit quantization override changes the effective model, derive a
new private cache key from the canonical runtime identity plus normalized
effective quantization. Never reuse the unchanged canonical key for a different
model path.

**Step 7: Verify GREEN**

Run:

```bash
.venv/bin/python -m pytest --no-cov \
  tests/test_tts_engine_modularization.py tests/test_tts.py \
  tests/test_tts_runtime_spec.py -q
```

**Step 8: Commit only Task 2 files**

```bash
git add modules/tts/engine_output.py modules/tts/engine_runtime.py \
  tests/test_tts_engine_modularization.py tests/test_tts.py
git commit -m "fix(tts): prevent stale audio and cache identity drift"
```

### Task 3: Fence active-job, playback-save, and playback-recovery races

**Files:**
- Modify: `desktop/src/features/events/useEventStream.ts`
- Modify: `desktop/src/features/conversion/useConversionJob.ts`
- Modify: `desktop/src/features/events/eventStreamModel.test.ts`
- Create if needed: `desktop/src/features/events/useEventStream.mounted.test.tsx`
- Modify: `desktop/src/features/player/playbackPersistence.ts`
- Modify: `desktop/src/features/player/useAudioPlayer.ts`
- Modify: `desktop/src/features/player/useAudioPlayer.test.tsx`
- Create if needed: `desktop/src/features/player/playbackPersistence.test.tsx`

**Step 1: Write the stale active-reconciliation test**

Mount the production hook with a deferred initial `getActiveConversion`.
Complete `startConversion` for job B or deliver a running event for B, then
resolve the older lookup as inactive. Assert B remains active/running.

**Step 2: Verify active-job RED**

Run:

```bash
cd desktop
npm test -- --run src/features/events/useEventStream.mounted.test.tsx
```

Expected: stale inactive response clears job B.

**Step 3: Implement mutation generations**

Track a monotonic runtime-state mutation generation. Each reconciliation captures
the generation before awaiting and applies its response only if unchanged.
Starting a job and authoritative state/progress events advance the generation.
Expose a narrow `markConversionStarted(jobId)` method from the event hook and
call it immediately after `startConversion` succeeds.

**Step 4: Write the ordered playback-save test**

Start saves for 60 seconds and 120 seconds with deferred API responses. Assert
the second request is not sent until the first settles and the final stored/UI
position is 120 seconds even if callers fire concurrently.

**Step 5: Verify playback-save RED**

Run the new playback-persistence test and expect both requests to be in flight
or the older completion to win.

**Step 6: Serialize saves per book**

Maintain a per-book promise tail. Append each save to that book's tail, continue
after failures, and delete completed tails without affecting a newer queued
operation. Preserve the existing throttle and failure-notice behavior.

**Step 7: Write the repeated recovery test**

Make the initial media error trigger a recovery load that fails. Restore the
endpoint and trigger playback/error again. Assert a second fresh URL/reload is
attempted without reloading the application.

**Step 8: Verify recovery RED**

Run:

```bash
cd desktop
npm test -- --run src/features/player/useAudioPlayer.test.tsx
```

Expected: only one recovery call occurs.

**Step 9: Make recovery ownership attempt-scoped**

Keep at most one recovery promise per book, but clear the guard in `finally`
after the attempt settles. A successful time update may still clear it early.

**Step 10: Verify GREEN**

Run all focused event/player tests, then:

```bash
cd desktop
npm run typecheck
npm test -- --run
```

**Step 11: Commit only Task 3 files**

```bash
git add desktop/src/features/events desktop/src/features/conversion/useConversionJob.ts \
  desktop/src/features/player
git commit -m "fix(desktop): fence conversion and playback races"
```

### Task 4: Fence library mutations, preserve partial uploads, and remove native dialogs

**Files:**
- Modify: `desktop/src/features/library/useLibraryList.ts`
- Modify: `desktop/src/features/library/useLibraryPanelState.ts`
- Modify: `desktop/src/features/library/useLibraryMaintenance.ts`
- Modify: `desktop/src/features/library/LibraryPanel.tsx`
- Modify: `desktop/src/features/library/useLibrary.test.tsx`
- Modify: `desktop/src/features/library/useLibraryMaintenance.test.tsx`
- Modify: `desktop/src/features/library/LibraryPanel.test.tsx`
- Modify: `desktop/src/features/conversion/useConversionSourceQueue.ts`
- Modify: `desktop/src/features/conversion/useConversionSourceQueue.test.tsx`

**Step 1: Write metadata and preview selection-race tests**

For metadata: defer PATCH A, select B, resolve A, and assert B's detail is not
replaced. For cleaning preview: defer A's preview, select B, resolve A, and
assert no A preview is rendered under B.

**Step 2: Verify RED**

Run the focused library hook/panel tests and confirm stale A data wins today.

**Step 3: Add entity/request ownership**

Capture the selected book ID and a mutation/preview generation. Apply responses
only when both still match. Loading state must also be generation-owned so an
older request cannot clear a newer spinner.

**Step 4: Write partial-upload and listener-unmount tests**

Resolve one of two browser uploads and reject the other; assert the successful
source is queued and the status reports one failure. Delay Tauri `listen`
registration, unmount, resolve registration, and assert every returned unlisten
function is called.

**Step 5: Verify RED**

Run:

```bash
cd desktop
npm test -- --run src/features/conversion/useConversionSourceQueue.test.tsx
```

**Step 6: Preserve partial successes and late listener cleanup**

Use `Promise.allSettled`, enqueue fulfilled uploads in input order, and report
the failed count. If listener registration resolves after unmount, immediately
invoke every returned unlisten handle instead of storing it.

**Step 7: Write packaged-safe cleanup confirmation tests**

Assert clicking `Clean storage` first renders inline Cancel/Confirm controls and
does not call the API. Confirm calls cleanup; Cancel does not. Assert no
production `window.confirm` dependency remains.

**Step 8: Verify confirmation RED**

Run the focused LibraryPanel/maintenance tests; current code either calls
`window.confirm` or executes without inline confirmation.

**Step 9: Move confirmation into React view state**

Make the maintenance hook's cleanup method perform the authorized operation
without a browser dialog. Add an inline armed state in `LibraryPanel`, following
the existing book-delete confirmation pattern.

**Step 10: Verify GREEN**

Run:

```bash
cd desktop
npm run typecheck
npm test -- --run
```

**Step 11: Commit Task 4 carefully**

These conversion files contain user changes. Inspect the staged diff and include
the integrated current state without reverting any pre-existing hunk.

```bash
git add desktop/src/features/library \
  desktop/src/features/conversion/useConversionSourceQueue.ts \
  desktop/src/features/conversion/useConversionSourceQueue.test.tsx
git diff --cached --check
git commit -m "fix(desktop): preserve async library and upload ownership"
```

### Task 5: Require fresh architecture-matched sidecars for every package

**Files:**
- Modify: `scripts/build_desktop_backend.py`
- Modify: `tests/test_desktop_sidecar_build.py`
- Modify: `desktop/package.json`
- Modify: `desktop/src-tauri/tauri.conf.json`
- Modify: `docs/RUN_APPLICATION.md`
- Modify: `docs/TESTING.md`

**Step 1: Write architecture mismatch tests**

Mock an arm64 host with an x86_64 same-OS target and the inverse. Assert the
build rejects both before dependency validation or PyInstaller execution.
Cover `amd64`/`x86_64` and `arm64`/`aarch64` normalization.

**Step 2: Verify architecture RED**

Run:

```bash
.venv/bin/python -m pytest --no-cov \
  tests/test_desktop_sidecar_build.py -k "architecture" -q
```

Expected: current OS-only validator accepts the mismatch.

**Step 3: Implement host/target architecture validation**

Parse the first target-triple component, normalize aliases, normalize
`platform.machine()`, and reject unknown or unequal architectures with an
actionable PyInstaller cross-architecture error.

**Step 4: Write the fresh-build contract test**

Assert the Tauri `beforeBuildCommand` invokes
`build_desktop_backend.py` before the frontend build, and that package scripts
do not use `--validate-existing` as their packaging gate.

**Step 5: Verify fresh-build RED**

Run the focused contract test; current `desktop:build` accepts any existing
ignored sidecar.

**Step 6: Make Tauri own the fresh sidecar build**

Set `beforeBuildCommand` to run the sidecar build followed by `npm run build`.
Make `desktop:build` invoke `tauri build` without a separate stale validation
step. Keep `--validate-existing` available only as an explicit diagnostic.
Update documentation to state that every package build is fresh.

**Step 7: Verify GREEN**

Run:

```bash
.venv/bin/python -m pytest --no-cov tests/test_desktop_sidecar_build.py -q
cd desktop
npm run typecheck
npm run build
```

**Step 8: Commit Task 5**

```bash
git add scripts/build_desktop_backend.py tests/test_desktop_sidecar_build.py \
  desktop/package.json desktop/src-tauri/tauri.conf.json \
  docs/RUN_APPLICATION.md docs/TESTING.md
git commit -m "fix(release): require fresh architecture-matched sidecars"
```

### Task 6: Contain Windows backend descendants

**Files:**
- Modify: `desktop/src-tauri/src/setup.rs`
- Modify: `desktop/src-tauri/src/backend.rs`
- Modify: `desktop/src-tauri/src/backend_tests.rs`
- Modify if needed: `desktop/src-tauri/Cargo.toml`

**Step 1: Write the Windows containment regression**

On Windows, launch a backend fixture that creates a long-lived descendant,
force backend shutdown, and assert both root and descendant exit. Add a
cross-platform structural test that `RunningBackend` owns the Windows
containment handle.

**Step 2: Verify RED**

Run on Windows:

```powershell
cd desktop/src-tauri
cargo test --locked backend_descendants
```

Expected: descendant survives forced direct-child kill.

**Step 3: Reuse the setup Job Object**

Expose the existing `WindowsJob` within the crate or extract it into a small
shared module. Assign the backend immediately after spawn, keep the handle in
`RunningBackend`, enable kill-on-close, and terminate the job during forced
shutdown. If assignment fails, kill/wait the child and fail startup rather than
running uncontained.

**Step 4: Verify GREEN**

Run:

```bash
cd desktop/src-tauri
cargo fmt --check
cargo test --locked -- --test-threads=1
cargo check --locked
```

The Windows CI run must execute the descendant integration test.

**Step 5: Commit Task 6**

```bash
git add desktop/src-tauri/src/setup.rs desktop/src-tauri/src/backend.rs \
  desktop/src-tauri/src/backend_tests.rs desktop/src-tauri/Cargo.toml
git commit -m "fix(desktop): contain Windows backend process trees"
```

### Task 7: Gate releases on the packaged artifact and WebKit playback policy

**Files:**
- Modify: `.github/workflows/source-validation.yml`
- Create: `.github/workflows/release-artifact-validation.yml`
- Modify: `scripts/desktop_runtime_smoke.py`
- Modify: `tests/test_desktop_runtime_smoke.py`
- Modify: `desktop/src/features/player/useAudioAnalyser.test.ts`
- Modify or create: `desktop/src/features/player/useAudioAnalyser.mounted.test.tsx`
- Modify: `docs/TESTING.md`

**Step 1: Write the WebKit hook-level invariant test**

Mount `useAudioAnalyser` with a WebKit user agent and a real ref-like audio
element. Provide an `AudioContext` constructor spy and assert it is never
constructed and `createMediaElementSource` is never called. Retain Blink
coverage.

**Step 2: Verify the test protects behavior**

Temporarily demonstrate RED by exercising the hook without the engine gate or by
asserting the current test lacks the mounted behavior, then restore production
state and retain the new passing regression.

**Step 3: Add packaged-sidecar smoke selection**

Extend `desktop_runtime_smoke.py` with an explicit packaged sidecar resource
argument or deterministic bundle-sidecar resolver. Test that the smoke command
uses the executable and resources copied into the built `.app`, not the source
Python launcher or repo-side staging directory.

**Step 4: Verify packaged smoke tests RED**

Run:

```bash
.venv/bin/python -m pytest --no-cov tests/test_desktop_runtime_smoke.py -q
```

Expected: no current path selects the `.app/Contents/Resources/binaries/...`
sidecar.

**Step 5: Add the release artifact workflow**

Create a macOS release-host workflow that:

1. installs locked Python/Node/Rust dependencies and FFmpeg;
2. installs the release runtime profile;
3. runs source tests;
4. runs `npm run desktop:build`, which freshly rebuilds the sidecar;
5. verifies the `.app`/DMG and embedded executable architecture/resources;
6. runs desktop runtime smoke against the embedded packaged sidecar;
7. uploads the validation artifact and logs.

Keep Windows source validation so Task 6 compiles/tests on Windows. Make
release-artifact validation callable manually and required for release-tag or
release-branch builds.

**Step 6: Verify workflow contracts locally**

Add source-level tests where practical for command ordering and packaged path
selection. Run YAML syntax/static inspection and all focused smoke tests.

**Step 7: Run the full local release matrix**

```bash
.venv/bin/python -m ruff check .
.venv/bin/python run_tests.py --contract
.venv/bin/python -m pytest -m property -o addopts= -q
cd desktop
npm run typecheck
npm test -- --run
npm run build
cd src-tauri
cargo fmt --check
cargo test --locked -- --test-threads=1
cargo check --locked
cd ../..
.venv/bin/python scripts/desktop_runtime_smoke.py
cd desktop
npm run desktop:build
```

Then inspect the produced `.app`/DMG, run the packaged-sidecar smoke, and verify
the bundled frontend contains the WebKit routing gate.

**Step 8: Commit Task 7**

```bash
git add .github/workflows scripts/desktop_runtime_smoke.py \
  tests/test_desktop_runtime_smoke.py desktop/src/features/player \
  docs/TESTING.md
git commit -m "ci(release): validate the packaged desktop artifact"
```

### Task 8: Final integration, review, and release verdict

**Files:**
- Review all files changed since design commit `773a501`
- Preserve all pre-existing uncommitted conversion-panel files

**Step 1: Inspect repository state**

Run:

```bash
git status --short
git diff --check
git diff 773a501 --stat
```

Confirm no original user change was reverted or omitted.

**Step 2: Run the complete verification matrix again**

Repeat Task 7 Step 7 from a fresh command invocation and record exact counts and
artifact paths.

**Step 3: Run specification review**

Dispatch a fresh reviewer with the design, this plan, and the full diff. Fix all
missing or extra behavior and rerun focused tests.

**Step 4: Run code-quality review**

Dispatch a separate fresh reviewer after specification approval. Fix every
Critical or Important issue and request re-review until approved.

**Step 5: Run final verification-before-completion**

Use `superpowers:verification-before-completion`. Do not claim completion unless
the full source matrix and the locally available packaged-artifact matrix pass.

**Step 6: Finish the development branch**

Use `superpowers:finishing-a-development-branch` and present the verified branch
state without merging, pushing, or opening a PR unless explicitly authorized.
