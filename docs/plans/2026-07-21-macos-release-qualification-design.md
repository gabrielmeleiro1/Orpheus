# macOS Release Qualification Design

## Purpose

Create a repeatable, local-first release gate for the first public Orpheus desktop release. The release targets Apple Silicon Macs running macOS 14 or newer, is built on the maintainer's Mac, and is distributed as an unsigned GitHub Release artifact.

The process must produce evidence that the exact downloadable artifact was tested. GitHub remains the source and download host; the existing source-validation workflow remains unchanged and no GitHub Actions release pipeline is added.

## Goals

- Turn the existing release-readiness commands into one resumable local qualification command.
- Test the packaged application rather than treating source tests as release proof.
- Measure startup, shutdown, idle resource use, process cleanup, and large-library responsiveness.
- Exercise failures that matter to a desktop application: backend loss, interrupted startup, repeated launch/quit, sleep/wake, invalid state, and missing resources.
- Use isolated qualification data so the maintainer's real library and models are not damaged.
- Use Computer Use to verify the installed app's visible and interactive behavior.
- Generate a machine-readable report, a human-readable report, SHA-256 checksums, and a release checklist for the exact commit and artifact.
- Document unsigned installation and Gatekeeper handling honestly.

## Non-goals

- Windows, Intel macOS, Linux desktop, or universal binaries in the first release.
- Apple Developer ID signing, notarization, or automated App Store distribution.
- Automated GitHub Release publishing or a new GitHub Actions release workflow.
- A beta tester group, telemetry service, remote crash-reporting backend, or release analytics.
- Revalidating conversion quality across a large content corpus. Existing pipeline tests and bounded conversion smoke remain authoritative for the pipeline.
- Making Computer Use a runtime or repository dependency. It is an agent-operated qualification tool only.

## Recommended Architecture

### 1. Local qualification runner

Add `scripts/release_qualification.py` as the single maintainer entry point. It orchestrates existing commands instead of duplicating their logic, records command duration and exit status, stops on blocking failures, and supports resuming from a report directory.

Configuration lives in `config/release_qualification.toml`. It declares the supported platform, expected bundle identifier/version, runtime profiles, artifact paths, lifecycle iteration counts, and performance budgets. Python 3.12's `tomllib` reads the file without adding a dependency.

The runner writes to `.artifacts/release-qualification/<release-id>/`:

- `report.json`: complete structured evidence and gate decisions.
- `report.md`: concise human-readable release report.
- `commands/`: bounded stdout/stderr logs for each command.
- `metrics/`: timestamped process/resource measurements.
- `screenshots/`: Computer Use screenshots selected for release evidence.
- `SHA256SUMS`: checksums for the DMG and report.
- `manual-checklist.md`: generated checklist tied to the release ID.

No evidence directory is committed. The final report and checksum may be uploaded to the GitHub Release beside the DMG.

### 2. Deterministic automated gates

The runner performs these phases in order:

1. **Preflight:** require macOS arm64, Python 3.12, Node 20+, Rust 1.88, a clean tracked worktree, an exact commit, adequate free disk space, and expected release metadata.
2. **Source validation:** run Ruff, the Python contract, base/MLX clean-profile verification, focused desktop/API tests, frontend typecheck/tests/build, and Rust test/check.
3. **Sidecar validation:** build the MLX sidecar, run the reduced-`PATH` sidecar smoke, verify bundled `ffmpeg`/`ffprobe`, and run a bounded offline conversion journey when cached runtime assets are available.
4. **Bundle validation:** build the Tauri app and DMG, inspect `Info.plist`, bundle identifier, version, arm64 Mach-O slices, resources, dynamic-library references, and the expected unsigned status.
5. **Lifecycle and performance:** install into an isolated applications directory, observe Computer Use-driven launch/navigation/quit sessions, sample the app/backend process tree, and fail on orphaned processes or exceeded budgets.
6. **Fault and persistence:** run isolated-data scenarios for backend interruption, startup/quit races, corrupted copies of state, and recovery after relaunch. Never mutate the maintainer's live application data.
7. **Evidence finalization:** render reports, calculate checksums, verify the checksum against the final artifact, and mark the candidate `pass`, `fail`, or `incomplete`.

`incomplete` is not releasable. It is used when a required manual/Computer Use check has not been recorded, a required MLX asset is unavailable, or the Gatekeeper installation rehearsal has not been performed.

### 3. Computer Use qualification

Computer Use drives and observes native behavior after deterministic gates succeed. Each action is followed by a fresh accessibility-tree read, with screenshots used where the accessibility tree is insufficient.

Required journeys:

- Launch the installed `.app` and reach a usable dashboard or an actionable setup screen.
- Complete the first-run/library-only path without UI stalls.
- Navigate every tab using pointer and keyboard controls.
- Exercise file selection or drag/drop, start a bounded test conversion when local assets are ready, observe progress, open the resulting library item, and play audio.
- Search and scroll an isolated large library while checking interaction latency and bounded resource use.
- Close during idle, startup, setup, and an active cancellable operation; verify immediate visual dismissal and eventual process-tree exit.
- Relaunch after backend termination and after sleep/wake.
- Verify reduced-motion behavior, resizing, keyboard focus, labels, and the absence of clipped or inaccessible primary controls.

Computer Use does not bypass macOS security warnings. Because bypassing Gatekeeper is a security-sensitive handoff, the maintainer performs the unsigned first-open step. Qualification resumes after the maintainer confirms that the documented procedure worked.

### 4. Performance budgets

Initial budgets are deliberately conservative and configurable. They establish a release baseline rather than claiming universal hardware performance.

- Existing-runtime cold launch to usable UI: at most 15 seconds.
- Warm launch to usable UI: at most 5 seconds.
- Close request to window disappearance: at most 1 second.
- Close request to complete Orpheus/backend process exit: at most 12 seconds.
- Settled dashboard idle CPU: median at most 3%, 95th percentile at most 8% over 60 seconds.
- Thirty-minute navigation/playback soak: no crash, no orphan, and no monotonic resident-memory increase greater than 100 MiB or 10%, whichever is larger.
- Library list: bounded rendered row count and no interaction stall exceeding 250 ms in the scripted 10,000-record isolated dataset.
- Release candidate: 25 consecutive clean launch/quit cycles; the final 1.0.0 candidate additionally runs 100 cycles.

Reports include raw values even when a budget passes. Future releases should ratchet budgets only after comparable local measurements exist.

### 5. Isolated data and destructive-safety policy

Qualification paths are rooted below the evidence directory and supplied through the existing desktop path environment contract. The runner refuses broad, home-directory, repository-root, or live Application Support paths. It writes a sentinel file into every disposable root and only removes directories containing the expected sentinel.

The installed application is copied to an isolated applications directory for most checks. A final `/Applications/Orpheus.app` rehearsal is optional until the release candidate is otherwise green and must use the existing replacement installer. The runner never deletes the maintainer's current installed app or live data automatically.

Fault scenarios operate on generated databases or copies. Backup/restore verification compares hashes and record counts before and after the scenario.

### 6. Unsigned GitHub distribution

The release package consists of:

- `Orpheus_<version>_aarch64.dmg`
- `SHA256SUMS`
- `release-qualification-<version>.md`
- GitHub release notes

Documentation states that the app is unsigned and not notarized. It explains how to verify SHA-256, install the DMG, and use macOS's supported manual-open path. It does not recommend disabling Gatekeeper globally or clearing quarantine recursively.

The GitHub Release is created manually only after:

- the exact tag commit matches the qualified commit;
- the DMG checksum matches the report;
- all automated gates pass;
- every required Computer Use/manual item is recorded;
- the unsigned installation rehearsal succeeds;
- rollback instructions and known limitations are present.

## Error Handling and Resumption

Every phase records `not_started`, `running`, `passed`, `failed`, `skipped`, or `incomplete`. A failed blocking command stops subsequent build or publishing preparation, but report generation always runs. `--resume <report-dir>` reruns the failed phase and all downstream phases; it never silently reuses evidence if the commit, configuration, toolchain, or artifact hash changed.

Logs redact bearer tokens, local usernames where practical, API keys, and query credentials. Reports include tool versions and sanitized paths but never environment dumps.

## Release Decision

A candidate is releasable only when its final state is `pass`. There are no warning-only exceptions for failed security audits, missing architecture slices, orphaned processes, checksum mismatches, incomplete Computer Use checks, or unverified unsigned installation instructions. Any accepted exception must be explicit, time-bounded, documented in the report, and must not downgrade a blocking security or integrity failure.
