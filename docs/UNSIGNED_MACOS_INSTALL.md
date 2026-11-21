# Installing unsigned Orpheus on macOS

Orpheus macOS releases are built for **Apple Silicon** on **macOS 14 or
newer**. The DMGs on the GitHub Release page are **unsigned** and
**unnotarized**, so macOS blocks the first launch until you allow it. That
warning is expected and says nothing about whether the download is safe; the
checksum does.

## 1. Verify the download before you open it

1. From the GitHub Release, download the `.dmg` and `SHA256SUMS`.
2. Put both files in the same folder and run:

   ```bash
   cd /path/to/download-folder
   shasum -a 256 -c --ignore-missing SHA256SUMS
   ```

The DMG line must report `OK`, for example `Orpheus_1.0.0_aarch64.dmg: OK`.
`SHA256SUMS` also lists the release's qualification report;
`--ignore-missing` skips it unless you downloaded it too.

- `FAILED` means the file does not match. Delete the DMG and do not open it.
- `no file was verified` means the DMG is not in that folder under its
  original name.

## 2. Install and open

1. Open the DMG and drag `Orpheus.app` to **Applications**.
2. Double-click `Orpheus.app`. macOS reports that it cannot verify the app;
   close the dialog without moving the app to the Trash (**Done** on macOS 15
   and later).
3. Open **System Settings → Privacy & Security** and scroll to **Security**.
   Next to the message about Orpheus, click **Open Anyway**, then confirm with
   your password or Touch ID.

macOS remembers this choice, so later launches open normally. On macOS 14 you
can instead right-click `Orpheus.app`, choose **Open**, and confirm.

Do not turn off Gatekeeper or strip quarantine flags from downloads; that
weakens protection for every app.

## What you are trusting

The app has no Apple Developer ID and is not notarized. You are trusting the
GitHub Release publisher and the checksum match, not Apple's checks.

## Uninstall and rollback

- **Uninstall:** quit Orpheus, then move `/Applications/Orpheus.app` to the
  Trash. Your library stays in
  `~/Library/Application Support/com.orpheus.desktop/`; delete that folder
  only if you want to wipe it.
- **Rollback:** download the previous release's DMG and `SHA256SUMS`, verify
  and install it the same way, and replace `/Applications/Orpheus.app`.

## Maintainers

Maintainers producing releases should follow
[RELEASING_MACOS.md](RELEASING_MACOS.md). Building from source is covered in
[RUN_APPLICATION.md](RUN_APPLICATION.md).
