# Complete Release-Readiness Audit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development and superpowers:systematic-debugging to execute this plan; every production fix must use superpowers:test-driven-development and receive independent Luna-high review.

**Goal:** Audit and harden the entire Orpheus desktop application across reliability, concurrency, data integrity, security, cross-platform behavior, and the actual packaged release artifact.

**Architecture:** Preserve the existing React/Tauri/FastAPI/application/pipeline/storage boundaries and the current dirty worktree. Four disjoint audit owners inspect the current implementation in parallel; confirmed issues move through proof, failing regression, smallest robust fix, independent review, focused verification, and a focused commit. Root owns integration, release tooling, and final verification.

**Tech Stack:** React 18, TypeScript, Vite, Tauri 2, Rust 1.88, Python 3.12, FastAPI, SQLite, PyInstaller, FFmpeg/FFprobe, pytest, Vitest, GitHub Actions.

---

## Safety baseline

- Branch at audit start: `feature/full-breaking-fix-pass`
- HEAD at audit start: `26e363049031fb2539de109b8b7604c1011f85cc`
- Existing work: 16 modified frontend files plus
  `desktop/src/features/conversion/useCleanerOptions.test.tsx`.
- Protected dirty scope:
  `desktop/src/api/client.ts`,
  `desktop/src/features/conversion/**`,
  `desktop/src/styles/controls.css`, and
  `desktop/src/types/conversion.ts`.
- Never reset, restore, clean, or rewrite the protected work.
- Never test against `data/`, `output/`, `temp/`, user libraries, model
  directories, installed apps, packaged releases, or existing databases.
- Use temporary runtime homes, databases, source fixtures, and output paths.
- Do not install optional runtimes, download models, or replace
  `/Applications/Orpheus.app`.
- A separate worktree is intentionally not used because the audit must include
  the current uncommitted implementation and preserve it in place.

## Ownership

| Owner | Exclusive audit/edit scope | Cross-scope rule |
| --- | --- | --- |
| Luna A | `modules/ingestion/`, `modules/pipeline/`, `modules/tts/`, `modules/audio/`, directly corresponding tests | Coordinate before touching app/web/storage/desktop/native files |
| Luna B | `modules/app/`, `modules/web/`, `modules/storage/`, directly corresponding tests | Coordinate before touching processing, desktop, or native files |
| Luna C | `desktop/src/` and directly corresponding Vitest files | Treat every protected dirty file as read-only unless root explicitly grants a precise hunk |
| Root | `desktop/src-tauri/`, `apps/launchers/`, release/runtime scripts, manifests, workflows, packaging tests, integration docs and matrix | Coordinate any cross-layer fix before editing another owner's files |

## Task 1: Parallel source and journey audit

**Files:**
- Inspect: every owned production file and test.
- Update: this plan's audit matrix only through root coordination.

**Steps:**

1. Inventory public entry points, state machines, identities, persistent/file
   effects, subprocesses, and tests in the owned scope.
2. Trace each relevant user journey across adjacent layers, recording exact
   call sites and ownership identifiers.
3. Adversarially inspect success, validation, partial failure, cancellation,
   retry, duplicate actions, stale/out-of-order results, shutdown, filesystem
   failure, and missing dependency behavior.
4. Run focused existing tests and add temporary or committed proof tests only
   for high-confidence issues.
5. Report findings with severity, exact file/line evidence, reproduction,
   violated contract, proposed regression, and affected journeys.
6. Do not modify production code until root accepts a confirmed issue and
   assigns exact file ownership.

## Task 2: Per-issue red/green implementation

**Files:**
- Test: the narrowest public contract covering the issue.
- Modify: only the owning production boundary.

**Steps for every confirmed issue:**

1. Write the smallest deterministic regression.
2. Run it and record the expected pre-fix failure.
3. Identify the violated ownership, atomicity, validation, or cleanup contract.
4. Implement the smallest robust fix without sleeps, timeout inflation,
   weakened assertions, suppression, or unrelated rewrites.
5. Run the focused test and adjacent subsystem gate.
6. Request independent Luna-high specification review.
7. Resolve every valid specification finding and request re-review.
8. Request a different Luna-high code-quality/security review.
9. Resolve every valid quality finding and request re-review.
10. Root reruns verification and commits only the focused issue files.

## Task 3: Full source verification

Run and record exact observed results:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python run_tests.py --contract
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -m property
.venv/bin/python -m pytest -m benchmark
.venv/bin/python -m pytest -m slow
.venv/bin/python -m pytest -m ui
cd desktop
npm run typecheck
npm test -- --run
npm run build
cd src-tauri
cargo fmt --all -- --check
cargo test --locked
cargo check --locked
```

Unavailable hardware/platform suites must be recorded as unexecuted or skipped,
never passed.

## Task 4: Runtime, workflow, and packaged artifact verification

1. Validate base/MLX/CUDA manifests, prohibited packages, precedence, target
   triples, skip semantics, and setup dry runs without installing profiles.
2. Run the focused launcher/runtime contract:

   ```bash
   .venv/bin/python -m pytest --no-cov \
     tests/test_desktop_backend_launcher.py \
     tests/test_desktop_runtime_contract.py \
     tests/test_desktop_runtime_smoke.py \
     -q -o addopts=
   ```

3. Build a fresh base artifact:

   ```bash
   cd desktop
   ORPHEUS_RUNTIME_PROFILE=base npm run desktop:build
   ```

4. Resolve the actual `.app` and DMG paths without assuming the internal
   executable name. Inspect app executable, sidecar, FFmpeg, FFprobe,
   architecture, permissions, resources, profile identity, metadata, icons,
   and license files.
5. Run packaged sidecar smoke against that exact app under reduced `PATH`:

   ```bash
   .venv/bin/python scripts/desktop_runtime_smoke.py \
     --backend sidecar \
     --app-bundle <actual-built-app-path> \
     --reduced-path \
     --check-ffmpeg-runtime
   ```

6. Run a bounded fixture conversion only when an already installed,
   positively-probed runtime is available offline. Otherwise record the exact
   limitation without downloading anything.
7. Validate workflow YAML and source contracts, including Windows source checks
   and exact executable discovery.

## Task 5: Final preservation and release verdict

1. Compare final status/name-only/stat against the safety baseline.
2. Confirm no protected user hunk or runtime data was reverted, deleted, or
   overwritten.
3. Run final specification and code-quality review of the full audit diff.
4. Use `superpowers:verification-before-completion`.
5. Complete the matrix, exact test counts, artifact evidence, commit summary,
   survivor analysis, and remaining risks.
6. Do not merge, push, deploy, install, delete data, download models, or create
   a pull request.

## Audit matrix

| Subsystem | User journey / cross-layer path | Failure modes inspected | Confirmed issue / root cause | Regression test | Fix commit | Independent reviewer / result | Verification evidence | Remaining risk |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Markdown ingestion | file/upload → preflight → `MarkdownParser` → speech text | BOM/UTF-16, leading/later/unclosed fences, empty content, raw-markdown preservation | A leading YAML fence was narrated because front matter had no boundary-specific removal | `tests/test_markdown_parser.py` leading/later/unclosed fence cases | `6194542` | Luna-high application/storage reviewer: approved, no Important findings | 39 focused ingestion tests; final Python suite green | Only a closed leading YAML fence is removed by design |
| EPUB ingestion | file/upload → ZIP validation → `EPUBParser`/preflight → chapters | corrupt/encrypted metadata, invalid UTF-8, ZIP count/member/total/ratio bounds, validation bypass | Raw `RuntimeError`/`UnicodeDecodeError` escaped while reading `mimetype`; normalized at the archive boundary | `tests/test_epub_metadata_boundary.py`; protected `tests/test_epub_zip_limits.py` | `d954553`; ZIP-limit work deliberately remains protected/uncommitted | Two different Luna-high reviewers approved typed boundary and ZIP-limit parity | 53 focused EPUB/preflight tests; full suite includes 8 ZIP-limit and 2 metadata-boundary regressions | Protected ZIP-limit files remain dirty by preservation requirement |
| Cleaning / cloud discovery | settings → key/backend → model list → conversion payload → cleaner | duplicate manual/auto fetch, key change during flight, stale response, missing model/key, provider error formatting | A pending manual fetch did not suppress the debounce; old-key models and selection survived key changes | `useCleanerOptions.test.tsx` deferred/fake-timer cases | `a9ebec8` | Luna-high application/storage reviewer approved after stale-key review | 8 focused cleaner-hook, 121 conversion, 397 full frontend tests | Real cloud credentials/provider responses were not exercised |
| TTS/model lifecycle | runtime options → factory/session → model cache → segment output → idle unload | cache reuse/identity, delayed unload, client/engine swap, cancellation, legacy detached references | Old idle timers and unconditional cache eviction could unload/evict a successor engine; legacy refs could outlive ownership | `test_tts_engine_session.py`, package-runtime/cache lifecycle regressions | `ffec5b4` | Different Luna-high processing reviewer: approved after generation/cache-identity review | 78 focused TTS lifecycle tests; final suite green | Real MLX/CUDA/Kokoro models and GPU/ANE hardware were not loaded |
| Pipeline workers | start → shared pool → cleaner/TTS workers → cancel/restart/shutdown | process/thread mode, stuck worker, queued successor, cancellation, timeout, metrics, restart | Cancellation returned before thread workers quiesced; timeout could clear ownership while the old executor still ran | `test_pool_manager.py`, `test_pipeline.py` cancellation/barrier cases | `9e165eb`, `d969de6` | Luna-high application/storage reviewer approved locking, barriers, and shutdown fencing | 115 relevant pipeline/conversion tests at review; final suite green | Forced OS/process death outside Python cleanup remains platform-owned |
| Audio/publication and paths | chapter audio → FFmpeg/FFprobe → M4A/M4B → atomic publish/workspace | subprocess timeout/cancel, stderr pressure, concat quoting, mixed inputs, partial output, Windows basenames | Windows device names such as `CON.txt` survived generic filename sanitization | packaging/workspace reserved-name regressions | `e9f61dc` | Luna-high processing reviewer: approved | Real FFmpeg packaging tests in Python suite; packaged FFmpeg/FFprobe smoke passed | Windows filesystem behavior was cross-target checked, not run on Windows |
| Conversion events | job → controller/event broker → SSE → React job/log state | late/out-of-order events, wrong job terminal/logs, queue overflow, shutdown sentinel | Events lacked job ownership; a late publish could displace the shutdown sentinel | job-scoped event, stale-log, and sentinel regressions | `8249b19`, `5a90666`, `ab77bac` | Separate Luna-high desktop/application reviewers approved | Event, SSE, conversion, and 397 frontend tests green | Terminal job-ID retention is bounded; extremely old IDs are a theoretical survivor |
| Conversion form ownership | manual/file/paste source → preflight/queue → start → retry/disconnect | blank manual source, cleared source, stale preflight, duplicate Start, paste upload in flight, reconnect | Cleared/manual source state retained stale ownership; disconnect retained job state; sourceless paste Start could race enqueue, then broad gating could block unrelated sources | conversion state/view/payload tests including scoped `pasteBusy && sourcelessPasteReady` | `d6ae73a`, `356cb34`, `6e57a45` | Luna-high processing implementation; different desktop reviewer found and verified the scoped-gate correction | 121 conversion tests; 397 frontend tests; typecheck/build green | Browser automation/manual visual QA not performed |
| Cleanup reservations | delete/prune/backup → controller → conversion service → storage | duplicate cleanup, storage/book overlap, active start/repair, exception release, wrong owner | Boolean/set reservations allowed a second caller to end another caller's cleanup | 11 reservation/owner cases in `test_conversion_service.py` | `131e6d0` | Luna-high desktop reviewer traced every synchronous call site and approved | 115 conversion-service tests; Ruff and final suite green | Ownership assumes begin/end remain in the same synchronous request stack |
| Storage/library | API → library service → SQLite/files → response/UI | traversal/symlink, preview containment, permissions, partial cleanup, pagination cap, stale delete/client swap | Preview cleanup trusted paths; maintenance artifacts inherited broad modes; a 25k cap looked like a real count; delete ownership survived API-client replacement | library/storage/permissions/pagination/delete regressions | `1ad0283`, `45d9a70`, `2589861`, `2bb40d4` | Multiple different Luna-high reviewers approved; delete lifecycle got separate cross-review | 66 adjacent library tests; storage suite and full Python/frontend suites green | Exact libraries at the 25k safety boundary deliberately error instead of returning a partial count |
| Player persistence | book selection → audio client → throttled/tail saves → API | rapid seek, hung save, client swap, stale success/failure, unmount | Per-client tail/throttle/commit/failure maps survived client replacement and blocked or corrupted successor ownership | `usePlayer` client-swap/hung-tail regressions | `6df374c` | Luna-high desktop reviewer: approved | 40 focused player tests at review; 397 frontend tests final | Real media-device interruption/background behavior remains manual |
| Desktop auth/server | Tauri token/readiness → loopback FastAPI → API/SSE/media | missing/wrong/scoped token, CORS origin, traversal, shutdown, malformed readiness | No new breaking defect after current implementation audit | Existing auth, web, readiness, media-path, and runtime contract suites | — | Luna-high application/storage plus root integration audit: no Critical/Important findings | Contract 1,675; runtime contract 60; packaged sidecar authenticated smoke passed | Host firewall/security-product behavior remains untested |
| Tauri/process lifecycle | app start/replace/close → sidecar readiness → graceful/forced shutdown; setup child supervision | stale generations, inherited pipes, process groups, timeout/cancel, restart authorization, Windows flags | Earlier hardening held; no new breaking defect found | 79 Rust tests plus Python launcher/runtime contracts | — | Root audit plus Luna-high native-path review: approved | `cargo fmt`, 79 tests, native check, Windows MSVC cross-target check all green | Windows/Linux native execution and signed macOS launch were unavailable |
| Runtime profiles | setup/manifests → clean profile verification → sidecar marker → capability probe | host dependency leakage, wrong target/profile, prohibited packages, marker mismatch, missing runtime/model | Packaged sidecar identity was implicit; release build did not prove a clean base dependency set | runtime marker/smoke/profile/workflow contract tests | `fe164f0`, `636202b` | Different Luna-high release/application reviewers approved | 60 focused runtime tests; packaged marker is `{"profile":"base"}` | MLX/CUDA installs, hardware probes, and real model assets intentionally not exercised |
| Release licenses/dependencies | lock/manifests → source checks → build → bundle resources | missing notices, vulnerable dev transitive, Python/Rust/npm integrity | Licenses were absent from bundle; `postcss 8.5.10` had two high advisories | sidecar/resource workflow tests; lock consistency/audit checks | `e112a32`, `06d2245` | Separate Luna-high release and processing reviewers approved | npm audit: 0; pip check: clean; cargo audit: 0 vulnerabilities; licenses present | Rust audit retains upstream informational unmaintained crates and Linux-only `glib 0.18.5` unsound advisory |
| Installer | built `.app` → hidden staging → target swap → optional source removal | copy failure, final rename failure, rollback, cleanup failure, existing install preservation | Existing app was removed before a safe swap; partial staging survived copy failure | 4 installer tests including final-swap and partial-copy failures | `d0650f9` | Luna-high processing found staging gap; different desktop reviewer approved correction | 4 focused tests and Ruff green; never run against `/Applications` | Process death or backup-removal failure can leave a hidden recoverable backup |
| Test gates | exact full/contract/marker commands → pytest-cov | narrow marker collection, empty lane, false coverage regression | Global 85% threshold falsely failed populated property/benchmark lanes | `test_run_tests.py` exact-expression/threshold regressions | `19b7854` | Luna-high processing reviewer verified hook ordering and non-exempt threshold | property 29, benchmark 12 pass; slow 5 skip; full 1,716 at 86.62% | Python `-m ui` has zero marked tests and exits 5; React Vitest is the actual UI gate |
| Packaged artifact | committed source + protected EPUB hardening → PyInstaller/Vite/Rust → `.app`/DMG → sidecar smoke | architecture, execute bits, resources, profile identity, dependency contents, FFmpeg runtime, reduced PATH, DMG integrity | The `fsspec` PyInstaller hook discovered `fsspec.conftest`, pulling the complete pytest and Hypothesis stacks into the production PYZ | sidecar-spec exclusion contract plus real pre/post-build PYZ inspection | `22ed685` plus all applicable commits above | Two independent Luna-high reviewers approved runtime safety; root rebuilt and inspected the archive | pytest, `_pytest`, Hypothesis, `fsspec.conftest`, coverage, mypy, and Ruff absent; arm64 executables; 105,814,039-byte valid DMG; authenticated smoke 100% | Unsigned/unnotarized local build; fixture conversion skipped because base bundle has no local TTS (`mlx` absent) |

## Final observed verification

- Ruff: all checks passed.
- Authoritative contract: 1,675 passed, 46 deselected, 86.17% coverage.
- Full Python suite: 1,716 passed, 5 skipped, 86.62% coverage.
- Property: 29 passed. Benchmark: 12 passed. Slow: 5 skipped because
  credentials/model runtime were unavailable. Python UI marker: 0 selected,
  exit 5 (recorded limitation, not a pass).
- Frontend: both TypeScript configurations passed; 49 Vitest files and 397
  tests passed; production Vite build passed.
- Rust: formatting passed; 79 tests passed; native `cargo check --locked`
  passed; installed `x86_64-pc-windows-msvc` target check passed with one
  dead-code warning.
- Runtime contract: 60 passed.
- Base artifact: source-matched macOS arm64 `.app` and
  `Orpheus_1.0.0_aarch64.dmg`; app, sidecar, FFmpeg, and FFprobe are executable
  arm64 Mach-O files; base marker and both license resources are present. The
  final DMG is 105,814,039 bytes with SHA-256
  `7ef01e7d3c16ec41824f590788b40a7c2c73ea4498f88d06835448fabab47cc7`.
- Frozen-module inspection: `fsspec.conftest`, pytest, `_pytest`, Hypothesis,
  coverage, pytest-cov, mypy, and Ruff are absent from the rebuilt PYZ.
- Packaged smoke: reduced-PATH sidecar startup/auth/SSE/library/FFmpeg check
  passed at 100%. The opted-in conversion journey was skipped because the base
  bundle positively reported no local TTS runtime (`No module named 'mlx'`);
  no model was downloaded.
- DMG: `hdiutil verify` succeeded; read-only mount contained `Orpheus.app` and
  the Applications link. The mounted app, sidecar, FFmpeg, and FFprobe were
  executable arm64 Mach-O files and the mounted marker reported `base`.
- Signing: strict deep verification failed because this local build has no
  sealed distribution signature. Signing and notarization remain a release
  blocker, not a passed check.
- No merge, push, deployment, installation, optional-runtime install, model
  download, real-data mutation, or installed-app replacement was performed.

## Survivor analysis

The confirmed failures were not one class of typo. Most survived because the
existing tests proved local happy paths but did not preserve an owner identity
across an asynchronous boundary: job IDs, client generations, executor
instances, cache engines, cleanup reservations, or in-flight API keys. Several
storage and installer failures similarly appeared only at the second failure
boundary, after partial cleanup, copy, rename, or pagination work. The final
packaging leak was invisible to source and dependency audits because it was
introduced by a PyInstaller hook; only inspecting the real PYZ exposed it.

Independent review added value where a locally plausible fix was too broad:
the pasted-source Start guard was narrowed so unrelated input modes remained
actionable, and installer staging was extended to restore the prior app after
copy as well as rename failures. Each meaningful production fix therefore has
a deterministic regression, focused verification, a focused commit, and
independent Luna-high review.

## End-to-end guarantees after the audit

- Async and event state is fenced by job, client, executor, request, engine, or
  reservation ownership before stale results can mutate current state.
- Source ingestion and publication reject hostile archive metadata, unsafe
  paths, Windows-reserved names, and out-of-bound work while preserving atomic
  publication and cleanup containment.
- TTS cache and idle-unload callbacks cannot evict or unload a successor
  engine; unavailable real runtimes are reported as unavailable rather than
  treated as passed.
- Library deletion, pagination, player save tails, conversion cleanup, and
  preview/storage maintenance preserve their current owner and fail explicitly
  at safety limits.
- Desktop startup/auth/SSE/library/FFmpeg behavior is verified against the
  packaged sidecar under reduced `PATH`; process shutdown and cancellation wait
  for owned work or fence timed-out predecessors.
- The release build proves its runtime profile, bundles license resources,
  excludes discovered test frameworks from the frozen archive, and produces a
  checksum-valid DMG. Distribution still requires real signing/notarization.

## Cross-platform and hardware result

- macOS arm64: native Rust tests/check, full source gates, actual base
  `.app`/DMG build, mounted-artifact inspection, and packaged smoke passed.
- Windows MSVC: installed `x86_64-pc-windows-msvc` cross-target
  `cargo check --locked` passed with one dead-code warning; no Windows-native
  execution or filesystem integration was available.
- Linux: source/workflow/profile contracts were inspected, but no Linux-native
  executable or GTK/WebKit launch was run.
- MLX/CUDA/Kokoro, GPU/ANE devices, real models, media-device interruption,
  provider credentials, and signed installed-app launch remain explicitly
  unexecuted.

## Branch and focused commit summary

Audit branch: `feature/full-breaking-fix-pass`. Baseline:
`26e363049031fb2539de109b8b7604c1011f85cc`. Final source-fix head:
`22ed685`.

- Desktop/event/player ownership: `d6ae73a`, `8249b19`, `5a90666`,
  `ab77bac`, `6df374c`, `356cb34`, `6e57a45`.
- Pipeline/TTS/cleanup ownership: `9e165eb`, `d969de6`, `ffec5b4`,
  `131e6d0`.
- Ingestion/storage/path hardening: `6194542`, `d954553`, `1ad0283`,
  `45d9a70`, `2589861`, `e9f61dc`, `2bb40d4`.
- Cleaner and installer lifecycle: `a9ebec8`, `d0650f9`.
- Release/runtime/dependency/test gates: `e112a32`, `fe164f0`, `06d2245`,
  `636202b`, `19b7854`, `22ed685`.

## Preservation confirmation

The final protected dirty scope is limited to the pre-existing EPUB ZIP-limit
work in `modules/errors.py`, `modules/ingestion/epub_parser.py`,
`modules/ingestion/preflight.py`, and `tests/test_epub_zip_limits.py`. It was
audited and tested but intentionally not committed, reset, restored, or
rewritten. No user runtime data, existing library/database, installed app,
model directory, or real output path was mutated.
