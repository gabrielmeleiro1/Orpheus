# Player Chapter Selection and Resume Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Highlight a selected chapter immediately and reliably resume every book from its latest stopped position after reopening the app.

**Architecture:** Preserve fractional media time throughout the player UI so chapter lookup uses the true seek offset. Add a small synchronous local-storage checkpoint layer per book alongside the existing throttled SQLite API persistence, and restore that checkpoint only when audio is loaded by Play.

**Tech Stack:** React 18, TypeScript, Vitest, browser `HTMLAudioElement`, browser `localStorage`, existing authenticated backend API.

---

### Task 1: Fractional chapter position regression

**Files:**
- Modify: `desktop/src/features/player/useAudioPlayer.test.tsx`
- Modify: `desktop/src/features/player/useAudioPlayer.ts`
- Modify: `desktop/src/features/player/useAudioElementBindings.ts`

1. Add a hook test with a fractional first-chapter duration and assert that
   jumping to chapter 2 immediately exposes chapter 2 as `currentChapter`.
2. Run the focused test and confirm it fails because `elapsedSec` is floored.
3. Preserve finite, non-negative fractional `currentTime` values in player
   state and time-update bindings.
4. Run the focused test and confirm it passes.

### Task 2: Per-book synchronous checkpoint model

**Files:**
- Create: `desktop/src/features/player/playbackCheckpoint.ts`
- Create: `desktop/src/features/player/playbackCheckpoint.test.ts`

1. Add failing unit tests for per-book read/write/reset behavior, malformed
   entries, invalid positions, and storage exceptions.
2. Run the tests and confirm they fail because the model does not exist.
3. Implement a versioned JSON record under a book-specific key with fail-soft
   reads and writes.
4. Run the checkpoint unit tests and confirm they pass.

### Task 3: Integrate checkpoints with playback persistence

**Files:**
- Modify: `desktop/src/features/player/playbackPersistence.test.tsx`
- Modify: `desktop/src/features/player/playbackPersistence.ts`
- Modify: `desktop/src/features/player/useAudioElementBindings.ts`
- Modify: `desktop/src/features/player/useAudioPlayer.ts`

1. Add failing hook tests showing that pending resume prefers the local
   checkpoint and that a position event writes the latest book position.
2. Run focused tests and confirm the expected failures.
3. Resolve pending resume from the local checkpoint, checkpoint explicit and
   media-driven position changes synchronously, and reset at book completion.
4. Run all player tests and confirm they pass.

### Task 4: Verification

**Files:**
- Verify only.

1. Run `npm test -- --run src/features/player`.
2. Run `npm test -- --run`.
3. Run `npm run typecheck`.
4. Run `npm run build`.
5. Review `git diff --check` and the final diff for scope.
