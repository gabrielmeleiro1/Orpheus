# Desktop UI Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Library deletion reliable, eliminate Library selection flicker, and align the three primary Logs filters.

**Architecture:** Keep confirmation in the React view instead of relying on a WebView browser dialog. Preserve the last successful Library detail during request transitions and gate mismatched actions with the existing loading state. Scope sizing normalization to the Logs toolbar using the shared control-height token.

**Tech Stack:** React 18, TypeScript, Vitest, happy-dom, Tauri WebView, CSS.

---

### Task 1: Rendered delete confirmation

**Files:**
- Modify: `desktop/src/features/library/LibraryPanel.test.tsx`
- Modify: `desktop/src/features/library/LibraryPanel.tsx`
- Modify: `desktop/src/features/library/useLibraryList.ts`
- Modify: `desktop/src/features/library/library.css`

**Step 1: Write the failing test**

Render `LibraryPanel` into happy-dom, click `#library-delete-btn`, and assert that the model's deletion function has not run and that Cancel and Delete book controls appear. Click Cancel and assert no mutation. Open confirmation again, click Delete book, and assert exactly one deletion call.

**Step 2: Run the test to verify it fails**

Run: `npm test -- --run src/features/library/LibraryPanel.test.tsx`

Expected: FAIL because the first click currently calls `deleteSelectedBook` directly and no inline confirmation controls exist.

**Step 3: Write the minimal implementation**

Add selection-scoped confirmation state to `LibraryPanel`. Replace the single destructive trigger with an inline Cancel/Delete book state and reset it when the selected detail changes. Remove the `window.confirm` gate from `useLibraryList` so that its mutation function performs the requested deletion directly. Add only the small layout rule needed for the inline prompt.

**Step 4: Run the test to verify it passes**

Run: `npm test -- --run src/features/library/LibraryPanel.test.tsx src/features/library/useLibrary.test.tsx`

Expected: PASS.

### Task 2: Smooth Library selection

**Files:**
- Modify: `desktop/src/features/library/useLibrary.test.tsx`
- Modify: `desktop/src/features/library/LibraryPanel.test.tsx`
- Modify: `desktop/src/features/library/useLibraryList.ts`
- Modify: `desktop/src/features/library/LibraryPanel.tsx`
- Modify: `desktop/src/features/library/library.css`

**Step 1: Write the failing hook test**

Load one book, start a deferred request for a second book, and assert that `selectedBookDetail` remains the first detail while `loadingDetail` is true. Resolve the request and assert that the visible detail changes to the second book.

**Step 2: Write the failing panel test**

Render the panel with `loadingDetail` true and a visible detail whose ID differs from `selectedBookId`. Assert `aria-busy=true`, the loading class is present, and metadata/destructive actions are disabled.

**Step 3: Run the tests to verify they fail**

Run: `npm test -- --run src/features/library/useLibrary.test.tsx src/features/library/LibraryPanel.test.tsx`

Expected: FAIL because `selectBook` clears the detail and the panel does not gate mismatched actions.

**Step 4: Write the minimal implementation**

Stop clearing `selectedBookDetail` in `selectBook`. Compute whether the displayed detail matches the current selected ID. Apply `aria-busy`, a loading class, and disabled states while loading or mismatched. Keep existing abort/latest-request behavior intact.

**Step 5: Run the tests to verify they pass**

Run: `npm test -- --run src/features/library/useLibrary.test.tsx src/features/library/LibraryPanel.test.tsx`

Expected: PASS.

### Task 3: Equal Logs toolbar heights

**Files:**
- Create: `desktop/src/features/logs/LogPanel.test.tsx`
- Modify: `desktop/src/features/logs/LogPanel.tsx`
- Modify: `desktop/src/features/logs/logs.css`

**Step 1: Write the failing render-contract test**

Render `LogPanel` and assert the severity group, category selector wrapper, and search field all expose the `log-filter-control` class.

**Step 2: Run the test to verify it fails**

Run: `npm test -- --run src/features/logs/LogPanel.test.tsx`

Expected: FAIL because the shared local sizing class is absent.

**Step 3: Write the minimal implementation**

Allow `TermSelect` to accept an optional wrapper class, apply `log-filter-control` to all three filter controls, and set their interactive children to `height: var(--density-control-height)` with compatible padding.

**Step 4: Run the test to verify it passes**

Run: `npm test -- --run src/features/logs/LogPanel.test.tsx`

Expected: PASS.

### Task 4: Verification

**Files:**
- Verify all modified desktop files.

**Step 1: Run focused regressions**

Run: `npm test -- --run src/features/library/LibraryPanel.test.tsx src/features/library/useLibrary.test.tsx src/features/logs/LogPanel.test.tsx src/api/client.test.ts`

Expected: PASS.

**Step 2: Run the full desktop suite**

Run: `npm test -- --run`

Expected: PASS.

**Step 3: Run static and production checks**

Run: `npm run typecheck && npm run build`

Expected: both commands exit successfully.

**Step 4: Review the diff**

Confirm only the three requested behaviors and their regression coverage changed; preserve the unrelated untracked `data/sources/` content.
