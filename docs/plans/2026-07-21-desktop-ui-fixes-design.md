# Desktop UI Fixes Design

## Scope

Fix three desktop UI regressions:

1. Library deletion does nothing in the packaged Tauri app.
2. The Library detail pane flickers when switching books.
3. The Logs severity control, source selector, and search field have different heights.

## Design

### Reliable library deletion

The delete hook currently relies on `window.confirm`, but the packaged macOS WebView does not present that browser dialog. Move confirmation into `LibraryPanel` as inline React state. The first Delete click exposes Cancel and Delete book controls; only the explicit second click calls `deleteSelectedBook`. The service and repository deletion behavior remains unchanged, including preservation of external source files.

### Smooth book selection

`selectBook` currently clears `selectedBookDetail` before the next detail request resolves, unmounting the metadata form and chapter list. Keep the last successful detail rendered while a new detail is loading. Mark the pane busy, visually soften it, and disable controls whose actions could target the newly selected ID while old details are still displayed. Atomically replace the visible detail when the latest request succeeds; existing request fencing continues to prevent stale results from winning.

### Consistent Logs controls

Normalize the three primary Logs filters locally to the shared `--density-control-height` token. A Logs-specific control class will give the segmented buttons, `TermSelect` trigger, and search field the same explicit height without changing controls elsewhere in the application.

## Error Handling and Accessibility

- Canceling inline deletion performs no mutation.
- Failed deletion keeps the existing optimistic rollback and visible error message.
- Detail loading uses `aria-busy`; controls are disabled while visible details do not match the selected row.
- Rapid book changes retain the existing abort and latest-request guards.
- Inline confirmation uses ordinary focusable buttons and a status message; it does not depend on a browser-native dialog.

## Testing

- Add a rendered click-path test for Delete, Cancel, and Delete book.
- Add a deferred-detail hook test proving the previous detail stays visible until the next detail arrives.
- Add a panel test proving detail actions are disabled while the pane is busy.
- Add a Logs render contract proving all three filter groups receive the shared toolbar-control class.
- Run targeted Vitest tests, the full desktop test suite, typecheck, and production build.
