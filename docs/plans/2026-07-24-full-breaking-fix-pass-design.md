# Full Breaking-Fix Pass Design

## Objective

Fix every high-confidence breaking issue found in the July 24 codebase audit and
close the release-process gap that allowed a packaged WKWebView playback failure
to pass source-only review.

The current uncommitted conversion-panel work remains in the same workspace and
branch. Fixes must preserve and integrate with those edits; no existing work may
be reset or reverted.

## Selected Approach

Use a defense-in-depth hardening pass:

1. Correct each confirmed defect at its owning boundary.
2. Add a regression test that fails before each production fix.
3. Strengthen build and release validation so source checks are no longer
   treated as evidence that the shipped application works.
4. Keep the current architecture and avoid broad rewrites that would introduce
   unrelated risk.

This is preferred over code-only patches, which would leave the release escape
mechanism intact, and over a broad state/process rewrite, which would increase
regression risk without being necessary to fix the confirmed failures.

## Python Runtime and Audio Correctness

The global API cleaner limiter will be created from the same multiprocessing
context used by the process pool. Async acquisition will own a cancellation-safe
background acquire: if cancellation wins, any subsequently acquired permit is
released exactly once.

TTS output helpers will remove deterministic output and segment files before
generation so retries or repeated paths cannot reuse prior audio. Runtime model
cache identity will remain aligned with quantization overrides; a cache lookup
must never return a model for a different effective runtime.

## Desktop State and Playback Correctness

Asynchronous desktop operations will use request generations or captured entity
identity:

- active-job reconciliation cannot overwrite a newer start or event;
- playback-position responses cannot regress a newer save;
- metadata and cleaning-preview responses cannot mutate a different selection;
- partial browser uploads preserve successful results and report individual
  failures;
- Tauri listener registration cleans up even when unmount wins the race;
- playback recovery can retry after a failed recovery attempt;
- destructive storage cleanup uses an inline React confirmation instead of a
  browser-native dialog.

The guards will be local to the owning hooks/reducers rather than adding a new
global state framework.

## Tauri, Sidecar, and Release Correctness

Sidecar builds will reject target architectures that do not match the
PyInstaller host architecture. Existing-sidecar validation will require a
provenance manifest that records source identity, runtime profile, target
triple, executable architecture, and relevant build inputs. A backend source
change or mismatched target/profile will make validation fail instead of
packaging stale ignored binaries.

The Windows backend process will use a Job Object with kill-on-close semantics,
matching the containment already used by runtime setup, so forced shutdown also
terminates descendants.

CI/release validation will build a fresh production frontend and sidecar, build
the Tauri artifact on supported release hosts, inspect the packaged resources,
run backend/runtime smoke against the packaged sidecar, and retain the explicit
WebKit invariant that playback audio is never routed through Web Audio. Hardware
audibility remains a release-host smoke item, but packaged source, resources,
launch, media delivery, and WebKit routing policy become automated gates.

## Error Handling

All race guards fail closed:

- stale responses are ignored;
- successful partial work is retained;
- leaked processes, permits, listeners, and temporary audio are deterministically
  released;
- invalid or stale build artifacts stop the build with an actionable error.

No error path may silently report success after discarding work or selecting an
artifact with unverified provenance.

## Testing and Completion Criteria

Every production change follows red-green TDD. Required evidence:

- focused regression tests for all confirmed issues;
- complete Python contract and property suites;
- Ruff;
- frontend typecheck, full Vitest suite, and production Vite build;
- Rust tests and `cargo check` on the current host, plus Windows-specific
  containment tests compiled under Windows CI;
- desktop runtime smoke using the packaged sidecar;
- fresh sidecar and Tauri bundle validation with provenance and architecture
  checks;
- final full-diff specification and code-quality review.

The pass is complete only when all confirmed findings are fixed, all regression
tests pass, the release artifact path is exercised, and the original dirty
worktree changes remain present.
