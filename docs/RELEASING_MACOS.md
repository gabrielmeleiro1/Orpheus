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

Replace `<id>` with the report directory that `prepare` prints:

```bash
python scripts/release_qualification.py prepare --version 1.0.0 --final
python scripts/release_qualification.py run --report-dir .artifacts/release-qualification/<id>
# Walk through the installed-app journeys and the Gatekeeper first-open check.
# Record every checklist item with notes or evidence before marking it passed.
python scripts/release_qualification.py finalize --report-dir .artifacts/release-qualification/<id>
```

Other subcommands: `status` shows progress, `observe` samples process budgets
for a lifecycle scenario (`warm-launch`, `idle-dashboard`, `shutdown`, or
`soak`), and `record-check` records a checklist result.

`finalize` succeeds only when every required phase and checklist item passed.
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

Point users to [UNSIGNED_MACOS_INSTALL.md](UNSIGNED_MACOS_INSTALL.md). The
supported first-open paths are right-click **Open** and **Open Anyway** in
System Settings. Do not document system-wide Gatekeeper switches or quarantine
stripping.

## Rollback and removal

- **Rollback:** install the previous published DMG the same way: verify its
  `SHA256SUMS`, then open it through a supported Gatekeeper path so the older
  bundle replaces `/Applications/Orpheus.app`.
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
