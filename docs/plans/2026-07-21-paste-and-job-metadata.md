# Paste and Job Metadata Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give pasted documents unambiguous draft metadata and restore active-job labels after frontend reload reconciliation.

**Architecture:** The Paste text card owns local draft title and author state that is transferred to a new queue item only after a successful upload. Conversion jobs carry request/library metadata through the backend active-job contract, and the frontend reconciliation path returns that contract to `useConversionJob` for display recovery.

**Tech Stack:** React 18, TypeScript, Vitest, FastAPI, Python dataclasses, pytest.

---

### Task 1: Dedicated paste metadata draft

**Files:**
- Modify: `desktop/src/features/conversion/conversionPanelStateTypes.ts`
- Modify: `desktop/src/features/conversion/useConversionSourceQueue.ts`
- Modify: `desktop/src/features/conversion/useConversionPanelState.ts`
- Modify: `desktop/src/features/conversion/views/SourceQueueView.tsx`
- Test: `desktop/src/features/conversion/ConversionPanelView.test.tsx`
- Test: `desktop/src/features/conversion/useConversionSourceQueue.test.tsx`

**Step 1: Write failing view tests**

Add assertions that the Paste text card renders dedicated `Paste title` and `Paste author` controls and calls their setters without calling selected-row edit actions.

**Step 2: Run the view test and verify RED**

Run: `npm test -- --run desktop/src/features/conversion/ConversionPanelView.test.tsx`

Expected: FAIL because the dedicated controls and view-model properties do not exist.

**Step 3: Add the draft view-model contract and controls**

Add `pastedTitle`, `pastedAuthor`, and their setters to the source-queue view model/actions. Render both inputs inside the paste card, adjacent to the paste textarea.

**Step 4: Write failing hook behavior tests**

Cover these behaviors through the real hook:

- A paste uses its dedicated title/author even when the selected editable row has identical metadata.
- Successful upload clears paste text/title/author and selects the new queue item.
- Failed upload preserves every draft field.

**Step 5: Run the hook test and verify RED**

Run: `npm test -- --run desktop/src/features/conversion/useConversionSourceQueue.test.tsx`

Expected: FAIL because paste creation still reads shared selected-row overrides.

**Step 6: Implement minimal draft ownership**

Keep paste title/author state beside `pastedText`. In `onAddPastedText`, trim and use only the paste draft, apply it to the new queue item, and clear all paste draft fields only after success. Remove the string-comparison inference.

**Step 7: Run focused frontend tests and verify GREEN**

Run: `npm test -- --run desktop/src/features/conversion/ConversionPanelView.test.tsx desktop/src/features/conversion/useConversionSourceQueue.test.tsx desktop/src/features/conversion/conversionQueueReducer.test.ts`

Expected: PASS.

### Task 2: Backend active-job metadata contract

**Files:**
- Modify: `modules/app/controller.py`
- Modify: `modules/app/services/conversion_service.py`
- Modify: `modules/web/serializers.py`
- Test: `tests/test_conversion_service.py`
- Test: `tests/test_web_api.py`

**Step 1: Write failing backend tests**

Assert that a new conversion job stores the requested title and author, a repair job stores the library book title and author, and `/api/conversions/active` serializes both fields.

**Step 2: Run focused backend tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_conversion_service.py tests/test_web_api.py -q --no-cov`

Expected: FAIL because `ConversionJob` and `job_payload` do not expose metadata.

**Step 3: Implement job metadata**

Add optional `title` and `author` fields to `ConversionJob`. Populate them when constructing conversion and repair jobs. Add the fields to `job_payload` without changing mutable job-state synchronization.

**Step 4: Run focused backend tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_conversion_service.py tests/test_web_api.py -q --no-cov`

Expected: PASS.

### Task 3: Frontend metadata reconciliation

**Files:**
- Modify: `desktop/src/types/conversion.ts`
- Modify: `desktop/src/features/events/useEventStream.ts`
- Modify: `desktop/src/features/conversion/useConversionJob.ts`
- Test: `desktop/src/features/conversion/useConversionJob.test.tsx`

**Step 1: Write failing reconciliation test**

Mount `useConversionJob` with an active-job API response containing title and author. Assert that mount reconciliation exposes them as `activeJobTitle` and `activeJobAuthor`.

**Step 2: Run the test and verify RED**

Run: `npm test -- --run desktop/src/features/conversion/useConversionJob.test.tsx`

Expected: FAIL because active-job responses have no typed metadata and refresh returns no response.

**Step 3: Implement reconciliation**

Extend the active-job response type. Make `refreshActiveConversionState` return the fetched response (or `null` when unavailable) after updating stream state. In `useConversionJob`, consume the response and store metadata paired with the reconciled job; retain the immediate metadata update after a successful start.

**Step 4: Run focused frontend tests and verify GREEN**

Run: `npm test -- --run desktop/src/features/conversion/useConversionJob.test.tsx desktop/src/features/events/eventStreamModel.test.ts`

Expected: PASS.

### Task 4: Full verification and implementation commit

**Files:**
- Verify all modified files.

**Step 1: Run frontend gates**

Run: `cd desktop && npm test -- --run && npm run typecheck`

Expected: 0 failed and both TypeScript projects clean.

**Step 2: Run backend gates**

Run: `.venv/bin/python -m pytest -q`

Expected: 1,584 collected with 0 failed and repository coverage above 85%.

**Step 3: Run lint and whitespace checks**

Run: `.venv/bin/ruff check modules tests && git diff --check`

Expected: no findings.

**Step 4: Review scoped diff and commit**

Stage only implementation and regression-test files, preserving `data/sources/`, then commit with a focused fix message.
