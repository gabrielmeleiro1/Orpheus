import type { LibraryBookDetail } from "../../types/api";

interface PlaybackCheckpointStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

interface PlaybackCheckpointRecord {
  version: 2;
  bookIdentity: string;
  positionSec: number;
  updatedAtMs: number;
}

const PLAYBACK_CHECKPOINT_PREFIX = "orpheus.playback.v2.";

export function playbackCheckpointBookIdentity(
  book: Pick<LibraryBookDetail, "id" | "created_at" | "source_path">,
): string {
  return JSON.stringify([
    book.id,
    book.created_at ?? "",
    book.source_path ?? "",
  ]);
}

function checkpointKey(bookId: number): string | null {
  return Number.isInteger(bookId) && bookId > 0
    ? `${PLAYBACK_CHECKPOINT_PREFIX}${bookId}`
    : null;
}

function defaultStorage(): PlaybackCheckpointStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function readPlaybackCheckpoint(
  bookId: number,
  bookIdentity: string,
  storage: PlaybackCheckpointStorage | null = defaultStorage(),
): number | null {
  const key = checkpointKey(bookId);
  if (!key || !bookIdentity || !storage) {
    return null;
  }

  try {
    const raw = storage.getItem(key);
    if (!raw) {
      return null;
    }
    const record = JSON.parse(raw) as Partial<PlaybackCheckpointRecord>;
    return record.version === 2
      && record.bookIdentity === bookIdentity
      && Number.isFinite(record.positionSec)
      && Number(record.positionSec) >= 0
      ? Number(record.positionSec)
      : null;
  } catch {
    return null;
  }
}

export function writePlaybackCheckpoint(
  bookId: number,
  positionSec: number,
  bookIdentity: string,
  storage: PlaybackCheckpointStorage | null = defaultStorage(),
): boolean {
  const key = checkpointKey(bookId);
  if (!key || !bookIdentity || !storage || !Number.isFinite(positionSec) || positionSec < 0) {
    return false;
  }

  const record: PlaybackCheckpointRecord = {
    version: 2,
    bookIdentity,
    positionSec,
    updatedAtMs: Date.now(),
  };
  try {
    storage.setItem(key, JSON.stringify(record));
    return true;
  } catch {
    return false;
  }
}

export function clearPlaybackCheckpoint(
  bookId: number,
  storage: PlaybackCheckpointStorage | null = defaultStorage(),
): void {
  const key = checkpointKey(bookId);
  if (!key || !storage) {
    return;
  }

  try {
    storage.removeItem(key);
  } catch {
    // Playback remains usable when browser storage is unavailable.
  }
}
