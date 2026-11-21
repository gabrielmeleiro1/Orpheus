# Orpheus macOS release notes template

Use this template when drafting a **manual** GitHub Release. Publishing is not
automated: create a draft, attach the DMG, `SHA256SUMS`, and the qualification
report, paste these notes, review it, and publish only after that final review.
The full procedure is in `docs/RELEASING_MACOS.md`.

## Title

`Orpheus <version> (macOS Apple Silicon)`

## Body

Copy and edit the following into the GitHub Release description:

**Platform**

- Apple Silicon (arm64)
- macOS 14 or newer
- Distribution: **unsigned** and **unnotarized** DMG

**Install**

This build has no Developer ID and is not notarized, so macOS blocks the first
launch. Verify the checksum before opening, then allow the app under
**System Settings → Privacy & Security → Open Anyway**. Step-by-step
instructions:
https://github.com/gabrielmeleiro1/Orpheus/blob/main/docs/UNSIGNED_MACOS_INSTALL.md

**Files**

- `Orpheus_<version>_aarch64.dmg`: the qualified DMG
- `SHA256SUMS`: checksums for the DMG and the report
- `release-qualification-<version>.md`: the qualification report

**Changes**

- …

**Known limitations**

- Unsigned / unnotarized; Gatekeeper blocks the first launch until allowed
- …

**Rollback and removal**

- Rollback: install the previous release DMG the same way
- Removal: quit Orpheus and delete `/Applications/Orpheus.app`

## Maintainer checklist (do not paste into the release)

- [ ] Local `scripts/release_qualification.py finalize` passed
- [ ] `cd` into the report dir and `shasum -a 256 -c SHA256SUMS` reported OK
- [ ] Staged DMG and `release-qualification-<version>.md` are beside `SHA256SUMS`
- [ ] Draft GitHub Release reviewed; no automatic release CLI or new Actions used
- [ ] Release tag created and pushed deliberately by the release owner
