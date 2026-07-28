# Player Chapter Selection and Resume Design

## Problem

Chapter offsets are stored with millisecond precision, but the player floors its
UI position to a whole second. A seek to a fractional chapter boundary therefore
plays the correct audio while briefly highlighting the preceding chapter.

Playback positions are already stored per book in SQLite. Periodic saves are
throttled to 15 seconds, however, and the final `beforeunload` API request may be
terminated with the window. The most recent position can consequently be lost
when the desktop app closes.

## Design

Keep playback position as a fractional number in React state and use that exact
value for chapter detection. Formatting may continue to render whole seconds,
but selecting a chapter must immediately classify the position at its true
fractional boundary.

Retain the SQLite API as the durable canonical store and add a synchronous,
versioned local-storage checkpoint keyed by book ID and validated against a
stable book fingerprint. Update this checkpoint whenever the media element
reports a position change and on explicit seeks, pause, close, end, and unload.
When audio is loaded after the user presses Play, prefer a valid local checkpoint
for that book over the server position. Continue the existing throttled server
saves so the database remains current. Reset both stores to zero when playback
finishes and clear the checkpoint when a book is deleted.

Fence loaded audio, in-flight URL requests, and checkpoint writes to the active
API-client generation so replacing the backend/library cannot associate an old
audio element with a new book that reuses the same numeric ID.

Malformed, missing, or inaccessible local storage must fail soft and fall back
to the server position.

## Testing

- Reproduce a fractional chapter start and assert the clicked chapter becomes
  current immediately.
- Verify per-book checkpoint isolation, validation, and storage failures.
- Verify Play restores a newer local checkpoint rather than an older server
  position.
- Verify playback events update the synchronous checkpoint.
- Run focused Vitest tests, the full desktop suite, type checking, and build.
