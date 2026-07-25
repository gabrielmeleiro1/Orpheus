# Orpheus macOS release notes template

Use this template when drafting a **manual** GitHub Release. Do not automate
publishing. Create a draft, attach the four inputs, review, then publish only
after final review. Tagging, pushing, and releasing each need a **separate
explicit request**.

## Title

`Orpheus <version> (macOS Apple Silicon)`

## Body

Copy and edit the following into the GitHub Release description:

**Platform**

- Apple Silicon (arm64)
- macOS 14 or newer
- Distribution: **unsigned** and **unnotarized** DMG

**Security notice**

This build is not Developer ID signed and is not notarized. macOS will show a
Gatekeeper warning on first open. Verify checksums before opening, then use
right-click **Open** or System Settings **Open Anyway**. See
`docs/UNSIGNED_MACOS_INSTALL.md`.

**Four release inputs**

Attach or include all four:

1. `Orpheus_<version>_aarch64.dmg` (exact qualified DMG)
2. `SHA256SUMS`
3. `release-qualification-<version>.md`
4. These release notes (edited for the version)

After download, verify with:

`cd /path/to/downloaded-files` then `shasum -a 256 -c SHA256SUMS`

**Changes**

- …

**Known limitations**

- Unsigned / unnotarized; Gatekeeper warning on first open is expected
- …

**Install**

Follow `docs/UNSIGNED_MACOS_INSTALL.md` (checksum verification before first
open; supported right-click Open / System Settings Open Anyway only).

**Rollback and removal**

- Rollback: install the previous release DMG after verifying its `SHA256SUMS`
- Removal: quit Orpheus and delete `/Applications/Orpheus.app`

## Maintainer checklist (do not paste into the release)

- [ ] Local `scripts/release_qualification.py finalize` passed
- [ ] `cd` into the report dir and `shasum -a 256 -c SHA256SUMS` reported OK
- [ ] Staged DMG and `release-qualification-<version>.md` are beside `SHA256SUMS`
- [ ] Draft GitHub Release reviewed; no automatic release CLI or new Actions used
- [ ] No tag or push performed without a separate explicit request
