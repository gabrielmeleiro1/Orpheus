# Local macOS Release Qualification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use @computer-use:computer-use for the installed-app verification tasks.

**Goal:** Build a reproducible, local-first release gate for an unsigned Apple Silicon macOS release, producing test evidence, performance measurements, checksums, and a Computer Use-verified installed-app checklist for the exact GitHub Release artifact.

**Architecture:** A Python qualification runner composes existing source, profile, sidecar, Tauri, and smoke commands; small helper modules own configuration, report state, macOS artifact inspection, process sampling, and isolated fixtures. The runner never publishes or changes GitHub state. Computer Use drives the installed app after deterministic gates succeed and records results into the same evidence directory, while the maintainer performs the Gatekeeper security-warning handoff.

**Tech Stack:** Python 3.12 (`argparse`, `dataclasses`, `hashlib`, `json`, `plistlib`, `statistics`, `subprocess`, `tomllib`), pytest, existing Orpheus/Tauri build tools, macOS `hdiutil`/`file`/`otool`/`codesign`/`ps`, Computer Use, Markdown, GitHub Releases.

---

## Constraints and release policy

- First target: Apple Silicon, macOS 14 or newer.
- Distribution: unsigned and unnotarized DMG uploaded manually to GitHub Releases.
- GitHub Actions: keep `.github/workflows/source-validation.yml` unchanged; do not add release automation.
- Qualification data: always isolated below `.artifacts/release-qualification/`; never use the maintainer's live library as a test fixture.
- Release result: only `pass` is releasable. `failed` and `incomplete` block release.
- Gatekeeper: Computer Use must not bypass the unsigned-app warning. Pause and ask the maintainer to complete the documented manual-open step.
- Pipeline: use existing bounded smoke/conversion checks; do not alter conversion behavior as part of release tooling.

### Task 1: Define configuration, evidence schema, and release states

**Files:**
- Create: `config/release_qualification.toml`
- Create: `tools/release_qualification/__init__.py`
- Create: `tools/release_qualification/model.py`
- Test: `tests/test_release_qualification_model.py`

**Step 1: Write failing configuration and state tests**

Cover:

- supported host is `darwin` + `arm64`;
- required bundle metadata is `Orpheus`, `com.orpheus.desktop`, and a non-empty version;
- performance budgets are positive and internally consistent;
- phase states are restricted to `not_started`, `running`, `passed`, `failed`, `skipped`, and `incomplete`;
- a report can only be `passed` when every required phase passed;
- a skipped required phase yields `incomplete`, never `passed`.

Example:

```python
def test_required_incomplete_phase_blocks_release():
    report = QualificationReport.new(release_id="1.0.0-test", commit="abc123")
    report.set_phase("computer_use", PhaseState.INCOMPLETE, "not recorded")
    assert report.release_state is ReleaseState.INCOMPLETE
```

**Step 2: Run the tests and verify failure**

Run:

```bash
python -m pytest --no-cov tests/test_release_qualification_model.py -q
```

Expected: FAIL because the package and model types do not exist.

**Step 3: Add the TOML policy**

Include configurable defaults:

```toml
[release]
product_name = "Orpheus"
bundle_id = "com.orpheus.desktop"
minimum_macos = "14.0"
architecture = "arm64"
runtime_profiles = ["base", "mlx"]

[budgets]
cold_launch_seconds = 15.0
warm_launch_seconds = 5.0
window_close_seconds = 1.0
process_exit_seconds = 12.0
idle_cpu_median_percent = 3.0
idle_cpu_p95_percent = 8.0
soak_memory_growth_mib = 100
soak_memory_growth_percent = 10.0
library_long_task_ms = 250
candidate_launch_cycles = 25
final_launch_cycles = 100
```

**Step 4: Implement immutable models and validation**

Use enums and dataclasses. Store timestamps in UTC ISO-8601. Validate unknown TOML keys so misspelled budgets cannot silently fall back.

**Step 5: Run focused tests**

Expected: all model tests PASS.

**Step 6: Commit**

```bash
git add config/release_qualification.toml tools/release_qualification tests/test_release_qualification_model.py
git commit -m "feat(release): define local qualification policy"
```

### Task 2: Implement safe evidence directories and report rendering

**Files:**
- Create: `tools/release_qualification/evidence.py`
- Create: `tools/release_qualification/report.py`
- Test: `tests/test_release_qualification_evidence.py`
- Modify: `.gitignore`

**Step 1: Write failing safety tests**

Test that evidence creation:

- stays beneath `.artifacts/release-qualification` by default;
- rejects `/`, the home directory, the repository root, and unresolved traversal;
- creates a unique sentinel containing the release ID;
- refuses cleanup if the sentinel is missing or mismatched;
- writes JSON atomically through a sibling temporary file;
- redacts bearer tokens, URL credentials, API-key-shaped values, and the local username from logs;
- renders deterministic Markdown phase tables.

**Step 2: Verify the tests fail**

Run:

```bash
python -m pytest --no-cov tests/test_release_qualification_evidence.py -q
```

**Step 3: Implement evidence creation and atomic writes**

Use `Path.resolve()`, `Path.is_relative_to()`, `tempfile`, and `Path.replace()`. Do not add general recursive-delete helpers. Expose one cleanup function that requires the sentinel and exact expected root.

**Step 4: Ignore generated evidence**

Add only:

```gitignore
.artifacts/release-qualification/
```

**Step 5: Run focused tests and `git diff --check`**

Expected: tests PASS and no whitespace errors.

**Step 6: Commit**

```bash
git add .gitignore tools/release_qualification tests/test_release_qualification_evidence.py
git commit -m "feat(release): add safe qualification evidence reports"
```

### Task 3: Build the bounded command runner and resumable phase engine

**Files:**
- Create: `tools/release_qualification/commands.py`
- Create: `tools/release_qualification/engine.py`
- Test: `tests/test_release_qualification_engine.py`

**Step 1: Write failing runner tests**

Cover:

- commands execute with an explicit working directory and sanitized environment;
- stdout/stderr stream to bounded log files rather than accumulating without limit;
- timeouts terminate the child process group and record failure;
- command arguments are arrays, never shell strings;
- failure stops downstream blocking phases but still writes the report;
- `--resume` reruns the first failed/incomplete phase and downstream phases;
- resume is rejected when commit, config hash, toolchain fingerprint, or artifact hash changed;
- secrets are redacted before persistence.

**Step 2: Run failing tests**

```bash
python -m pytest --no-cov tests/test_release_qualification_engine.py -q
```

**Step 3: Implement `CommandSpec` and `CommandResult`**

Each result records command label, sanitized argv, start/end timestamp, duration, exit code, timeout state, and relative log path.

**Step 4: Implement the phase engine**

Use a fixed ordered phase registry. Avoid a generic plugin framework. Persist after every phase transition so an interrupted long build can be diagnosed and resumed.

**Step 5: Run tests**

Expected: all engine tests PASS, including timeout cleanup.

**Step 6: Commit**

```bash
git add tools/release_qualification tests/test_release_qualification_engine.py
git commit -m "feat(release): orchestrate resumable local gates"
```

### Task 4: Add strict macOS release preflight

**Files:**
- Create: `tools/release_qualification/preflight.py`
- Test: `tests/test_release_qualification_preflight.py`

**Step 1: Write failing preflight tests**

Use injected command/system readers to cover:

- non-Darwin and non-arm64 hosts fail;
- macOS below 14 fails;
- Python below 3.12, Node below 20, or Rust not equal to the pinned toolchain fails;
- missing `hdiutil`, `otool`, `codesign`, `file`, `ffmpeg`, or `ffprobe` fails;
- dirty tracked files fail, while ignored evidence does not;
- detached/uncommitted state is recorded precisely;
- insufficient disk space fails with required/available bytes;
- Tauri version, package version, and requested release version must agree;
- the existing GitHub Actions workflow is informational, not a release dependency.

**Step 2: Run and observe failure**

```bash
python -m pytest --no-cov tests/test_release_qualification_preflight.py -q
```

**Step 3: Implement the preflight collector**

Never dump the complete environment. Record only allowlisted tool versions, OS build, architecture, commit, branch/tag, disk availability, and configuration hash.

**Step 4: Add exact-version behavior**

For candidate `1.0.0`, compare:

- `desktop/package.json` version;
- `desktop/src-tauri/tauri.conf.json` version;
- requested CLI version;
- an existing `v1.0.0` tag, if present.

Reject a tag that points at a different commit.

**Step 5: Run focused tests**

Expected: all preflight tests PASS.

**Step 6: Commit**

```bash
git add tools/release_qualification/preflight.py tests/test_release_qualification_preflight.py
git commit -m "feat(release): validate macOS release host"
```

### Task 5: Compose the existing automated validation matrix

**Files:**
- Create: `tools/release_qualification/matrix.py`
- Test: `tests/test_release_qualification_matrix.py`
- Modify: `docs/TESTING.md`

**Step 1: Write failing matrix tests**

Assert the phase creates exact argv/cwd specs for:

1. `.venv/bin/python -m ruff check .`
2. `.venv/bin/python run_tests.py --contract`
3. `.venv/bin/python run_tests.py --verify-profile base --verify-profile mlx`
4. the focused desktop/web pytest command from `docs/TESTING.md` with `--no-cov`;
5. `npm ci`, `npm run typecheck`, `npm test -- --run`, and `npm run build` in `desktop/`;
6. `cargo test --locked -- --test-threads=1` and `cargo check --locked` in `desktop/src-tauri/`;
7. `python -m orpheus bench quick` as bounded non-model regression evidence.

Test that a failed command blocks build qualification and that no command uses `shell=True`.

**Step 2: Verify failure**

```bash
python -m pytest --no-cov tests/test_release_qualification_matrix.py -q
```

**Step 3: Implement the matrix as declarative `CommandSpec` values**

Reuse existing commands exactly. Do not move their logic into the release tool.

**Step 4: Update testing documentation**

Clarify that `scripts/release_qualification.py` is the full local release gate and that the existing GitHub workflow remains source validation only.

**Step 5: Run focused tests**

Expected: matrix tests PASS.

**Step 6: Commit**

```bash
git add tools/release_qualification/matrix.py tests/test_release_qualification_matrix.py docs/TESTING.md
git commit -m "feat(release): compose local validation matrix"
```

### Task 6: Inspect the exact sidecar, app bundle, and DMG

**Files:**
- Create: `tools/release_qualification/artifact.py`
- Test: `tests/test_release_qualification_artifact.py`
- Modify: `scripts/build_desktop_backend.py` only if an artifact path cannot be obtained without parsing human output

**Step 1: Write failing artifact tests with temporary fake bundles**

Cover:

- `Info.plist` product, identifier, version, and minimum OS checks;
- missing app executable, sidecar, `ffmpeg`, `ffprobe`, icons, or frontend assets fails;
- every Mach-O executable contains arm64;
- forbidden absolute build-machine dynamic-library paths fail;
- `codesign` reports the expected unsigned state; an unexpected ad-hoc or Developer ID state is recorded and evaluated explicitly;
- DMG name includes version and `aarch64`;
- `hdiutil verify` failure blocks release;
- mounted DMG contains exactly one expected `.app` plus intended presentation files;
- SHA-256 is calculated by streaming and changes when any artifact byte changes.

**Step 2: Run failing tests**

```bash
python -m pytest --no-cov tests/test_release_qualification_artifact.py -q
```

**Step 3: Implement artifact inspection**

Use `plistlib` and explicit macOS command wrappers. Mount DMGs under a temporary directory and always detach in `finally`. Never copy or execute untrusted artifacts during unit tests.

**Step 4: Add build command specs**

The production build phase invokes:

```bash
.venv/bin/python scripts/build_desktop_backend.py --runtime-profile mlx
cd desktop && npm run desktop:build
```

It then locates the app/DMG through deterministic Tauri bundle paths and records hashes before any installation.

**Step 5: Run tests and inspect one locally built bundle**

Expected: unit tests PASS. The real inspection may fail initially if release metadata/resources are incomplete; record and fix those findings as separate scoped tasks rather than weakening the inspector.

**Step 6: Commit**

```bash
git add tools/release_qualification/artifact.py tests/test_release_qualification_artifact.py scripts/build_desktop_backend.py
git commit -m "feat(release): verify packaged macOS artifacts"
```

### Task 7: Create isolated application data and large-library fixtures

**Files:**
- Create: `tools/release_qualification/fixtures.py`
- Test: `tests/test_release_qualification_fixtures.py`

**Step 1: Write failing fixture tests**

Test that:

- all runtime paths live below a sentinelled qualification root;
- the environment uses the existing `AUDIOBOOK_DATA_DIR`, `AUDIOBOOK_OUTPUT_DIR`, `AUDIOBOOK_TEMP_DIR`, and `AUDIOBOOK_DB_PATH` contract;
- 10,000 books are generated through the storage schema/repository without invalid records;
- fixture generation is deterministic from a seed;
- a small playable fixture uses only project-authored/generated audio;
- corruption scenarios operate on copies and preserve a hash-addressed pristine backup;
- cleanup refuses live Application Support and repository data paths.

**Step 2: Run failing tests**

```bash
python -m pytest --no-cov tests/test_release_qualification_fixtures.py -q
```

**Step 3: Implement fixture generation with production storage APIs**

Use `modules.storage.SQLiteRepository` and migrations rather than hard-coding table SQL. Batch inserts inside bounded transactions. Generate titles/authors/tags that exercise search and filtering.

**Step 4: Implement scenario copies**

Create independent roots for `clean`, `large-library`, `corrupt-db`, `missing-output`, and `read-only-copy`. Do not chmod the parent evidence directory.

**Step 5: Run focused storage and fixture tests**

```bash
python -m pytest --no-cov tests/test_release_qualification_fixtures.py tests/test_storage_migrations.py tests/test_storage_repository.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add tools/release_qualification/fixtures.py tests/test_release_qualification_fixtures.py
git commit -m "feat(release): generate isolated desktop fixtures"
```

### Task 8: Measure process lifecycle and performance budgets

**Files:**
- Create: `tools/release_qualification/macos_processes.py`
- Create: `tools/release_qualification/metrics.py`
- Test: `tests/test_release_qualification_metrics.py`

**Step 1: Write failing parser and budget tests**

Using recorded `ps` fixtures, test:

- app, WebView/helper, backend sidecar, and setup descendants are attributed to one Orpheus process tree;
- unrelated processes containing similar text are excluded;
- CPU median/p95 and RSS totals are computed correctly;
- startup and shutdown deadlines use monotonic time;
- an app window closing quickly does not pass if the backend remains alive;
- no process from a prior cycle survives into the next cycle;
- 25/100-cycle summaries identify the exact failing iteration;
- soak memory compares a settled baseline against a trend, not a single noisy sample.

**Step 2: Verify failure**

```bash
python -m pytest --no-cov tests/test_release_qualification_metrics.py -q
```

**Step 3: Implement read-only process sampling**

Use `ps` with explicit columns and no privileged APIs. Write JSON Lines samples. Never terminate a PID unless its executable path resolves inside the qualified app bundle or qualification runtime root.

**Step 4: Implement observer sessions**

Support:

```bash
python scripts/release_qualification.py observe warm-launch --report-dir PATH
python scripts/release_qualification.py observe idle-dashboard --seconds 60 --report-dir PATH
python scripts/release_qualification.py observe shutdown --report-dir PATH
python scripts/release_qualification.py observe soak --minutes 30 --report-dir PATH
```

Computer Use performs the matching UI action while the observer samples. Each observer emits a concise ready marker and a final budget decision.

**Step 5: Run focused tests**

Expected: metrics tests PASS.

**Step 6: Commit**

```bash
git add tools/release_qualification/macos_processes.py tools/release_qualification/metrics.py tests/test_release_qualification_metrics.py
git commit -m "feat(release): measure desktop lifecycle budgets"
```

### Task 9: Add the qualification CLI and checklist recording

**Files:**
- Create: `scripts/release_qualification.py`
- Create: `tools/release_qualification/checklist.py`
- Test: `tests/test_release_qualification_cli.py`

**Step 1: Write failing CLI tests**

Cover help and argument validation for:

```text
prepare --version VERSION [--final]
run --report-dir PATH [--resume]
observe SCENARIO --report-dir PATH
record-check CHECK_ID --result pass|fail|incomplete --notes TEXT [--evidence PATH]
finalize --report-dir PATH
status --report-dir PATH
```

Test that:

- `prepare` creates evidence/config snapshots but does not build;
- `run` stops before Computer Use with that phase `incomplete`;
- evidence paths are copied into the report directory and hashed;
- checks cannot be marked passed without required notes/metrics/evidence;
- `finalize` refuses any failed/incomplete required item;
- no command publishes, tags, pushes, opens GitHub, or modifies `.github/workflows`.

**Step 2: Run failing tests**

```bash
python -m pytest --no-cov tests/test_release_qualification_cli.py -q
```

**Step 3: Implement the CLI**

Keep `main()` thin. Delegate to the modules from Tasks 1–8. Return exit code `0` for pass, `1` for failure, and `2` for incomplete/unsupported qualification.

**Step 4: Generate the required checklist**

Checklist IDs must be stable and include:

- `unsigned_first_open`
- `cold_launch`
- `warm_launch`
- `all_tabs_pointer`
- `all_tabs_keyboard`
- `file_picker_or_drop`
- `conversion_progress`
- `library_playback`
- `large_library_search_scroll`
- `idle_shutdown`
- `startup_shutdown`
- `active_operation_shutdown`
- `backend_recovery`
- `sleep_wake`
- `resize_scaling`
- `reduced_motion`
- `focus_and_labels`
- `visual_regression`
- `known_limitations_reviewed`

**Step 5: Run CLI tests and a dry-run prepare**

```bash
python -m pytest --no-cov tests/test_release_qualification_cli.py -q
python scripts/release_qualification.py prepare --version 1.0.0
```

Expected: tests PASS; prepare prints the evidence directory and leaves the release state `incomplete`.

**Step 6: Commit**

```bash
git add scripts/release_qualification.py tools/release_qualification tests/test_release_qualification_cli.py
git commit -m "feat(release): add local qualification command"
```

### Task 10: Document unsigned installation and manual GitHub publishing

**Files:**
- Create: `docs/RELEASING_MACOS.md`
- Create: `docs/UNSIGNED_MACOS_INSTALL.md`
- Create: `.github/RELEASE_TEMPLATE.md`
- Modify: `README.md`
- Modify: `docs/RUN_APPLICATION.md`
- Modify: `docs/README.md`
- Test: `tests/test_release_documentation.py`

**Step 1: Write failing documentation contract tests**

Assert that documentation:

- says Apple Silicon and macOS 14+;
- states unsigned and unnotarized clearly;
- gives `shasum -a 256` verification instructions;
- uses supported right-click **Open** / System Settings **Open Anyway** guidance;
- never recommends `spctl --master-disable`, globally disabling Gatekeeper, or recursive quarantine removal;
- distinguishes source CI from local release qualification;
- lists the four release uploads and the manual GitHub Release steps;
- includes rollback and removal instructions;
- contains no automatic `gh release create` command.

**Step 2: Verify tests fail**

```bash
python -m pytest --no-cov tests/test_release_documentation.py -q
```

**Step 3: Write maintainer documentation**

Document the exact local flow:

```bash
python scripts/release_qualification.py prepare --version 1.0.0 --final
python scripts/release_qualification.py run --report-dir .artifacts/release-qualification/<id>
# Complete Computer Use and Gatekeeper checklist
python scripts/release_qualification.py finalize --report-dir .artifacts/release-qualification/<id>
```

Then manually create a draft GitHub Release, attach the DMG, `SHA256SUMS`, and Markdown report, compare the uploaded filenames/checksum, and publish only after final review.

**Step 4: Write end-user unsigned-install documentation**

Explain the security trade-off without implying the app is notarized. Include checksum verification before first open and a normal uninstall path.

**Step 5: Run documentation tests**

Expected: PASS.

**Step 6: Commit**

```bash
git add README.md docs .github/RELEASE_TEMPLATE.md tests/test_release_documentation.py
git commit -m "docs: add unsigned macOS release process"
```

### Task 11: Execute the Computer Use qualification journeys

**Files:**
- Evidence only: `.artifacts/release-qualification/<release-id>/`
- No source files should change unless a verified defect is fixed in a separate commit with its own regression test.

**Step 1: Read the Computer Use skill and initialize it**

Use `@computer-use:computer-use`. Target the built app by its absolute isolated `.app` path or bundle identifier. Begin every journey with a fresh `get_app_state`, and fetch fresh state after every action before reusing element references.

**Step 2: Perform the unsigned first-open handoff**

Computer Use may open Finder and inspect the DMG, but it must not bypass the Gatekeeper warning. Ask the maintainer to perform the documented right-click/Open or Open Anyway step. Resume only after confirmation, then record `unsigned_first_open`.

**Step 3: Verify startup and navigation**

For cold and warm launches:

1. Start the matching observer command.
2. Launch Orpheus with Computer Use.
3. Verify the dashboard or actionable setup screen through the accessibility tree.
4. Navigate all tabs by pointer.
5. Repeat using keyboard tab navigation.
6. Capture screenshots only at stable representative states.
7. Record the checks with observer metrics attached.

**Step 4: Verify conversion and playback**

Use a project-authored short Markdown fixture and isolated runtime data. Do not download unexpected models. If the MLX runtime/assets are unavailable, the release remains `incomplete`; do not mark the journey skipped-passed.

Verify source selection, primary action, progress routing, completion routing, library detail, playback, seeking, stop/pause, and visible error recovery.

**Step 5: Verify large-library responsiveness**

Launch against the 10,000-book fixture. Start the process sampler, then use Computer Use to search, change filters, page forward, hold scrolling, select distant records, resize the window, and return to Dashboard. Record interaction stalls and rendered-row evidence from the app/test instrumentation without opening developer tools in the release build.

**Step 6: Verify lifecycle and faults**

Run these independently with clean process baselines:

- quit while idle;
- quit during cold startup;
- quit during setup/status checking;
- cancel/quit during an active bounded operation;
- terminate only the qualified backend process, then observe recovery;
- sleep/wake the Mac with the app open (maintainer initiates OS sleep if Computer Use cannot);
- relaunch after every scenario;
- run 25 cycles for candidates and 100 cycles for the final 1.0.0 build.

Window dismissal and full process-tree exit are separate gates.

**Step 7: Verify accessibility and visual behavior**

Use keyboard-only traversal, inspect accessible labels/roles, test reduced-motion, resize to minimum dimensions, and inspect setup, dashboard, conversion, progress, library, logs, settings, and player states. Fail clipped primary controls, invisible focus, missing tab panels, or continuously animating reduced-motion canvases.

**Step 8: Record every result**

For each checklist item:

```bash
python scripts/release_qualification.py record-check CHECK_ID \
  --result pass \
  --notes "Observed behavior and measured result" \
  --evidence /absolute/path/to/evidence.png
```

Never record a pass solely because the UI looked plausible; attach the relevant state, screenshot, process log, or metric.

### Task 12: Fault remediation loop and full regression verification

**Files:**
- Modify only files implicated by verified qualification failures.
- Add a colocated regression test for every code defect.

**Step 1: Classify each finding**

Use one of: release blocker, functional defect, performance-budget failure, documentation defect, environmental prerequisite, or accepted known limitation. A release blocker cannot become a known limitation merely to obtain a pass.

**Step 2: Reproduce outside Computer Use where possible**

Add the smallest deterministic regression test. Use `superpowers:systematic-debugging` before proposing a fix and `superpowers:test-driven-development` for implementation.

**Step 3: Fix one finding per commit**

Run its focused test, then rerun the failed qualification phase and all downstream phases. Resume must reject stale evidence automatically when the artifact hash changes.

**Step 4: Run the complete fresh gate**

After the final code change, create a new release ID rather than reusing old evidence:

```bash
python scripts/release_qualification.py prepare --version 1.0.0 --final
python scripts/release_qualification.py run --report-dir .artifacts/release-qualification/<new-id>
```

Repeat every required Computer Use item against the newly hashed DMG.

**Step 5: Run repository-level verification**

```bash
python run_tests.py --contract
python -m pytest --no-cov tests/test_release_qualification_*.py tests/test_release_documentation.py -q
cd desktop && npm run typecheck
cd desktop && npm test -- --run
cd desktop && npm run build
cd desktop/src-tauri && cargo test --locked -- --test-threads=1
cd desktop/src-tauri && cargo check --locked
git diff --check
git status --short
```

Expected: all commands pass and the tracked worktree is clean.

### Task 13: Finalize evidence and rehearse manual GitHub Release creation

**Files:**
- Evidence: `.artifacts/release-qualification/<release-id>/report.json`
- Evidence: `.artifacts/release-qualification/<release-id>/report.md`
- Evidence: `.artifacts/release-qualification/<release-id>/SHA256SUMS`
- Evidence: qualified DMG under `desktop/src-tauri/target/release/bundle/dmg/`

**Step 1: Finalize locally**

```bash
python scripts/release_qualification.py finalize \
  --report-dir .artifacts/release-qualification/<release-id>
```

Expected: exit `0`, release state `pass`, exact commit/tag/version recorded, all required checks passed, and hashes finalized.

**Step 2: Verify the checksum independently**

```bash
shasum -a 256 -c .artifacts/release-qualification/<release-id>/SHA256SUMS
```

Expected: every listed file reports `OK`.

**Step 3: Review release contents without publishing**

Confirm the four intended deliverables, release notes, known limitations, rollback steps, and unsigned warning. Do not push a tag or create/publish a GitHub Release without a separate explicit user request.

**Step 4: Commit the completed release-tooling implementation**

Do not commit evidence artifacts. Stage only source, tests, configuration, and documentation:

```bash
git status --short
git add config scripts tools tests docs README.md .gitignore .github/RELEASE_TEMPLATE.md
git commit -m "feat(release): add local macOS qualification gate"
```

## Completion criteria

Implementation is complete when:

- no new GitHub Actions release workflow exists;
- focused qualification tests and the full repository matrix pass;
- a clean Apple Silicon host can generate a report from one local command sequence;
- the exact unsigned DMG passes artifact inspection and checksum verification;
- Computer Use has exercised every required installed-app journey against that DMG;
- the maintainer has completed the Gatekeeper handoff successfully;
- lifecycle/performance budgets pass, including no orphaned backend after quit;
- isolated fixture cleanup proves it cannot touch live data;
- final release state is `pass` and manual GitHub Release inputs are ready.
