# Releasing Orpheus on macOS (Apple Silicon)

Maintainer guide for producing and publishing an **unsigned, unnotarized** Apple
Silicon DMG. First target: **Apple Silicon**, **macOS 14+**.

This document does **not** automate GitHub state. Do **not** tag, push, or create
a GitHub Release unless you receive a **separate explicit request** for that
step.

**Signing policy:** qualification expects no Developer ID / Apple Distribution
identity and no nonempty `TeamIdentifier`. Builds request an outer ad-hoc seal
(`signingIdentity: "-"`) so `codesign --verify --deep --strict` passes with
`Signature=adhoc`, `TeamIdentifier=not set`, and sealed resources present.
That complete ad-hoc seal is the expected unsigned state; it carries **no
distribution identity** and is not notarization.

## Source CI vs full local release gate

| Gate | What it is | What it is not |
| --- | --- | --- |
| Source CI | `.github/workflows/source-validation.yml` on GitHub Actions | A release publisher, installer builder, or artifact uploader |
| Full local release gate | `scripts/release_qualification.py` on an Apple Silicon host | Automatic tagging, pushing, or `gh release` publishing |

Source validation remains informational for release readiness. Only a local
qualification run that finishes with release state `pass` produces releasable
evidence.

## Prerequisites

- Apple Silicon Mac running macOS 14 or newer
- Clean, qualified tree per [TESTING.md](TESTING.md) and the release
  qualification preflight
- Intended runtime profile already installed (typically `mlx` for Apple Silicon
  releases)

## Local qualification flow

Replace `<id>` with the report directory printed by `prepare`:

```bash
python scripts/release_qualification.py prepare --version 1.0.0 --final
python scripts/release_qualification.py run --report-dir .artifacts/release-qualification/<id>
# Complete Computer Use journeys and the Gatekeeper first-open handoff.
# Record every checklist item; do not mark pass without notes/evidence.
python scripts/release_qualification.py finalize --report-dir .artifacts/release-qualification/<id>
```

`finalize` succeeds only when every required phase and checklist item passed.
Failed or incomplete required work blocks release.

## Portable staged evidence (after finalize)

`finalize` stages portable release inputs **inside** the report directory:

- the exact qualified `.dmg` (copied beside the checksums)
- `release-qualification-<version>.md` (immutable Markdown report copy)
- `SHA256SUMS` (basename-only entries for the staged DMG and Markdown report)

Verify checksums from that directory so `shasum` resolves basenames correctly:

```bash
cd .artifacts/release-qualification/<id>
shasum -a 256 -c SHA256SUMS
```

Expected: every listed file reports `OK`. The staged DMG and
`release-qualification-<version>.md` sit beside `SHA256SUMS` in the same
directory.

## Four manual release inputs

Prepare these four inputs before any GitHub Release draft:

1. **DMG** — the staged unsigned Apple Silicon disk image from the report
   directory
2. **SHA256SUMS** — the portable checksum file from the same directory
3. **Qualification report** — `release-qualification-<version>.md`
4. **Release notes** — human-written notes (use
   [`.github/RELEASE_TEMPLATE.md`](../.github/RELEASE_TEMPLATE.md)); include
   known limitations, unsigned/unnotarized warning, and rollback guidance

There is no fifth automated upload path. Do not invent Actions workflows or
publish scripts for this.

## Manual GitHub Release procedure (draft → review → publish)

Perform this only after local `finalize` passed and checksum verification
succeeded. Keep it manual:

1. On GitHub, create a **draft** Release for the intended version.
2. Attach the four inputs above (DMG, `SHA256SUMS`,
   `release-qualification-<version>.md`, and paste/adapt the release notes).
3. Compare uploaded filenames and checksums against the local report directory
   (`shasum -a 256 -c SHA256SUMS` after `cd` into that directory).
4. **Review** the draft: Apple Silicon / macOS 14+, unsigned and unnotarized
   wording, install link to [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md),
   known limitations, and rollback/removal steps.
5. **Publish** the draft only after that final review.

Do **not** use the GitHub CLI to create or upload releases, automatic tag
creation, pushing of tags, or new GitHub Actions for publishing. Tagging,
pushing, and releasing each require a **separate explicit request**.

## End-user install handoff

Point users at [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md). Supported
first-open paths are right-click **Open** and System Settings **Open Anyway**.
Do not document Gatekeeper kill-switches or quarantine stripping.

## Rollback and removal

- **Rollback:** install the previous published DMG the same way (verify its
  `SHA256SUMS`, then open via the supported Gatekeeper path) so
  `/Applications/Orpheus.app` is replaced by the older bundle.
- **Uninstall / removal:** quit Orpheus, then remove `/Applications/Orpheus.app`
  (Trash or Finder). Optional local library data under the app support folder
  can be deleted separately if a full wipe is desired; qualification fixtures
  never use the live library.

## Related docs

- [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md) — end-user unsigned
  install
- [TESTING.md](TESTING.md) — source CI vs local matrices
- [RUN_APPLICATION.md](RUN_APPLICATION.md) — build and install from source
- [COMMIT_WORKFLOW.md](COMMIT_WORKFLOW.md) — commit and staging rules
