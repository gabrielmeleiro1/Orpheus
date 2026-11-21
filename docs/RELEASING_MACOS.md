# Releasing Orpheus on macOS (Apple Silicon)

This is the maintainer procedure for producing and publishing an **unsigned,
unnotarized** DMG for **Apple Silicon** on **macOS 14+**.

Publishing is manual on purpose. No script or workflow tags, pushes, or creates
a GitHub Release. Tagging, pushing, and publishing are separate steps, and each
needs an explicit request from the release owner.

**Signing policy.** Qualification expects no Developer ID or Apple
Distribution identity and no nonempty `TeamIdentifier`. Builds request an
outer ad-hoc seal (`signingIdentity: "-"`), so
`codesign --verify --deep --strict` passes with `Signature=adhoc`,
`TeamIdentifier=not set`, and sealed resources present. That complete ad-hoc
seal is the expected unsigned state. It carries **no distribution identity**
and is not notarization.

## Source CI vs the full local release gate

| Gate | What it is | What it is not |
| --- | --- | --- |
| Source CI | `.github/workflows/source-validation.yml` on GitHub Actions | A release publisher, installer builder, or artifact uploader |
| Full local release gate | `scripts/release_qualification.py` on an Apple Silicon Mac | Automatic tagging, pushing, or release publishing |

Source validation is informational for a release. Only a local qualification
run that ends in release state `pass` produces releasable evidence.

## Prerequisites

- An Apple Silicon Mac running macOS 14 or newer.
- A clean tree that passes the checks in [TESTING.md](TESTING.md) and the
  qualification preflight.
- The runtime profile you intend to ship, already installed (usually `mlx`).

## Local qualification

`prepare` prints the report directory; set `REPORT` to it. Every later
command takes it as `--report-dir`:

```bash
python scripts/release_qualification.py prepare --version 1.0.0 --final
REPORT=.artifacts/release-qualification/<id>
python scripts/release_qualification.py run --report-dir "$REPORT"

# Lifecycle budgets: one passing observe per scenario (see below).
python scripts/release_qualification.py observe cold-launch --report-dir "$REPORT" \
  --action-started-at <t0> --ui-ready-at <t1>
python scripts/release_qualification.py observe warm-launch --report-dir "$REPORT" \
  --action-started-at <t0> --ui-ready-at <t1>
python scripts/release_qualification.py observe idle-dashboard --report-dir "$REPORT"
python scripts/release_qualification.py observe soak --report-dir "$REPORT"
python scripts/release_qualification.py observe shutdown --report-dir "$REPORT"

# Walk through the installed-app journeys and the Gatekeeper first-open check,
# then record each item in manual-checklist.md, for example:
python scripts/release_qualification.py record-check cold_launch --report-dir "$REPORT" \
  --result pass --notes "..." --metrics <file>

python scripts/release_qualification.py finalize --report-dir "$REPORT"
```

`status --report-dir "$REPORT"` shows progress at any point.
`manual-checklist.md` in the report directory lists which items also need
`--evidence` or `--metrics`. Record every item with notes or evidence before
marking it passed.

### Lifecycle observations

`observe` refuses an app bundle that differs from the qualified build. It uses
`desktop/src-tauri/target/release/bundle/macos/Orpheus.app` unless you pass
`--app-bundle` (for example `--app-bundle /Applications/Orpheus.app` for the
installed copy). Run Orpheus from that same bundle. Each run prints
`budget_passed=... detail=...` and writes its decision to
`metrics/<scenario>-decision.json` in the report directory. The budgets are in
the report's `config/release_qualification.toml`, copied from
`config/release_qualification.toml` by `prepare`.

- **`cold-launch` and `warm-launch`** require `--action-started-at` and
  `--ui-ready-at`. The tool does not time the launch: it compares
  `--ui-ready-at` minus `--action-started-at`, in seconds, with
  `cold_launch_seconds` or `warm_launch_seconds`. Measure the two moments
  yourself, on one clock, as decimal seconds: when you open Orpheus, and when
  the library appears. Any clock works because only the difference is used,
  for example `0` and a stopwatch reading (`--action-started-at 0
  --ui-ready-at 7.4`), or two readings of
  `python3 -c 'import time; print(time.monotonic())'`. Measure `cold-launch`
  on the first launch after restarting the Mac and `warm-launch` on a relaunch
  soon after quitting. Run the command while Orpheus is still open, because it
  also records the running process tree.
- **`idle-dashboard`** samples the CPU use of the Orpheus processes once a
  second for 60 seconds (`--seconds` changes this) while the app sits idle,
  and checks the median and 95th percentile against `idle_cpu_median_percent`
  and `idle_cpu_p95_percent`.
- **`soak`** samples their memory for 30 minutes (`--minutes` changes this)
  and checks the growth against `soak_memory_growth_mib` and
  `soak_memory_growth_percent`.
- **`shutdown`** needs Orpheus running when it starts. It waits 12 seconds
  from its start (`process_exit_seconds`; pass `--seconds` for a longer wait)
  for Orpheus to quit and exit, so quit Orpheus right after starting it. It
  checks that the window closes within `window_close_seconds` and every
  Orpheus process exits within `process_exit_seconds` of the quit request. Without `--action-started-at`, the quit request is taken as the
  last sample in which the window was still open. Pass `--action-started-at`
  only with a `time.monotonic()` reading taken on the same Mac, because
  `observe` compares it with its own `time.monotonic()` samples.

`idle-dashboard` and `soak` fail when no Orpheus backend from that bundle is
running, and `shutdown` fails when no Orpheus process is running at its start.

### Finalize

`finalize` succeeds only when every required phase and checklist item passed
and every `observe` scenario has a passing decision recorded against the
qualified artifact. A decision recorded against an earlier build does not
count; re-run `observe` for that scenario.
Failed or incomplete required work blocks the release. Qualification includes
the `lifecycle_performance` phase and its configured performance budgets; keep
those measurements and the installed-app checklist evidence in the report.
Source validation alone does not satisfy them.

## Staged evidence

`finalize` stages portable release inputs **inside** the report directory:

- the exact qualified `.dmg`;
- `release-qualification-<version>.md`, an immutable copy of the report;
- `SHA256SUMS`, with basename-only entries for the DMG and the report.

Verify the checksums from that directory so `shasum` resolves the basenames:

```bash
cd .artifacts/release-qualification/<id>
shasum -a 256 -c SHA256SUMS
```

Every listed file must report `OK`. The staged DMG and
`release-qualification-<version>.md` sit beside `SHA256SUMS`.

## The four release inputs

Prepare these four inputs before drafting a GitHub Release:

1. **DMG:** the staged, unsigned Apple Silicon disk image from the report
   directory.
2. **SHA256SUMS:** the checksum file from the same directory.
3. **Qualification report:** `release-qualification-<version>.md`.
4. **Release notes:** written by hand from
   [`.github/RELEASE_TEMPLATE.md`](../.github/RELEASE_TEMPLATE.md), including
   known limitations, the unsigned and unnotarized warning, and rollback
   guidance.

There is no automated upload path for these.

## Manual GitHub Release (draft, review, publish)

Do this only after `finalize` passed and the checksums verified.

1. On GitHub, create a **draft** Release for the version.
2. Attach the DMG, `SHA256SUMS`, and `release-qualification-<version>.md`, and
   paste the release notes.
3. Compare the uploaded filenames and checksums with the local report
   directory (`cd` into it and run `shasum -a 256 -c SHA256SUMS`).
4. **Review** the draft: Apple Silicon and macOS 14+, the unsigned and
   unnotarized wording, a link to
   [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md), known limitations,
   and rollback and removal steps.
5. **Publish** the draft after that review.

Keep this in the GitHub web interface. Do not add command-line release
uploads, automatic tag creation, or a publishing workflow.

## End-user install

Point users to [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md), the
one place that describes checksum verification and the first-open steps. Link
to it rather than restating the steps. Do not document system-wide Gatekeeper
switches or quarantine stripping.

## Rollback and removal

- **Rollback:** install the previous published DMG by following
  [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md), so the older bundle
  replaces `/Applications/Orpheus.app`.
- **Uninstall:** quit Orpheus, then move `/Applications/Orpheus.app` to the
  Trash. Delete the library data in the app support folder only for a full
  wipe. Qualification fixtures never use the live library.

## Related docs

- [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md): end-user install of
  unsigned builds
- [TESTING.md](TESTING.md): source CI and local verification
- [RUN_APPLICATION.md](RUN_APPLICATION.md): building and installing from
  source
- [CONTRIBUTING.md](../CONTRIBUTING.md): branch and commit conventions
