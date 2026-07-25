# Installing unsigned Orpheus on macOS

Orpheus desktop releases for the first target platform are built for
**Apple Silicon** on **macOS 14+**. Published GitHub Release DMGs are
**unsigned** and **unnotarized**. macOS Gatekeeper will warn on first open;
that warning is expected and does not mean the download is automatically safe.

Treat every download as untrusted until you verify its checksum against the
release’s `SHA256SUMS`.

## Before you open the app

1. Download the Apple Silicon `.dmg` and the matching `SHA256SUMS` from the
   GitHub Release.
2. Place both files in the same folder.
3. Verify the checksum **before first open**:

```bash
cd /path/to/download-folder
shasum -a 256 -c SHA256SUMS
```

Every listed file must report `OK`. If verification fails, delete the DMG and
stop.

## Supported first-open paths

After checksum verification:

1. Open the DMG and drag `Orpheus.app` to **Applications** (or another location
   you control).
2. On first launch, use one of the supported Gatekeeper paths:
   - In Finder, **right-click** `Orpheus.app` and choose **Open**, then confirm
     **Open** in the dialog; or
   - Follow the macOS **System Settings** prompt and choose **Open Anyway**
     when offered for this app.

These are the supported ways to proceed past the unsigned-app warning.

Do **not** turn off Gatekeeper for the whole Mac, or mass-clear quarantine
flags on downloaded files. Those steps are unsupported and weaken system
protections for every app.

## Security trade-off

An unsigned, unnotarized build is not Apple-notarized. You are trusting the
GitHub Release publisher and the checksum match, not Apple’s notarization
pipeline. Prefer releases that also publish `SHA256SUMS` and a qualification
report, and verify hashes yourself.

macOS reports a complete **ad-hoc** outer seal on the app with
`TeamIdentifier=not set` and sealed resources. That seal is not a Developer ID
signature and carries **no distribution identity**; Gatekeeper will still warn
on first open.

## Uninstall and rollback

- **Uninstall / removal:** quit Orpheus, then move `/Applications/Orpheus.app`
  to Trash (or delete that bundle). Remove leftover app-support data only if
  you intend to wipe local library state.
- **Rollback:** download the previous release’s DMG, verify it with that
  release’s `SHA256SUMS`, install it the same way, and replace
  `/Applications/Orpheus.app`.

## Maintainer / packaging reference

Maintainers producing releases should follow
[RELEASING_MACOS.md](RELEASING_MACOS.md). Building from source is covered in
[RUN_APPLICATION.md](RUN_APPLICATION.md).
