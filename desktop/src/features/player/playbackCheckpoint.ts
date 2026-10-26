import type { LibraryBookDetail } from "../../types/api";
import { chapterSetIdentity, resolveChapterPlaybackAnchor, type ChapterPlaybackAnchor } from "./chapterPositionModel";

interface PlaybackCheckpointStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
  readonly length?: number;
  key?(index: number): string | null;
}

interface PlaybackCheckpointRecord {
  version: 3;
  bookIdentity: string;
  positionSec: number;
  updatedAtMs: number;
  chapterAnchor?: ChapterPlaybackAnchor;
}

const PLAYBACK_CHECKPOINT_PREFIX = "orpheus.playback.v3.";
// Legacy records cannot establish which durable epoch produced their position.
// Resume from the server until a checkpoint has been saved for this identity.
const LEGACY_CHECKPOINT_PREFIX = "orpheus.playback.v2.";

export function playbackCheckpointBookIdentity(
  book: Pick<LibraryBookDetail, "id" | "created_at" | "source_path" | "library_id" | "library_epoch">,
): string {
  if (!book.library_id || !book.library_epoch) return "";
  return JSON.stringify([
    book.library_id,
    book.library_epoch,
    book.id,
    book.created_at ?? "",
    book.source_path ?? "",
  ]);
}

function checkpointKey(bookId: number, bookIdentity: string): string | null {
  return Number.isInteger(bookId) && bookId > 0
    ? `${PLAYBACK_CHECKPOINT_PREFIX}${bookId}.${encodeURIComponent(bookIdentity)}`
    : null;
}

function defaultStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function readCheckpointRecord(
  bookId: number,
  bookIdentity: string,
  storage: PlaybackCheckpointStorage | null = defaultStorage(),
): PlaybackCheckpointRecord | null {
  const key = checkpointKey(bookId, bookIdentity);
  if (!key || !bookIdentity || !storage) {
    return null;
  }

  try {
    const raw = storage.getItem(key);
    if (!raw) {
      return null;
    }
    const record = JSON.parse(raw) as Partial<PlaybackCheckpointRecord>;
    return record.version === 3
      && record.bookIdentity === bookIdentity
      && Number.isFinite(record.positionSec)
      && Number(record.positionSec) >= 0
      ? record as PlaybackCheckpointRecord
      : null;
  } catch {
    return null;
  }
}

export function readPlaybackCheckpoint(
  bookId: number,
  bookIdentity: string,
  storage: PlaybackCheckpointStorage | null = defaultStorage(),
): number | null {
  return readCheckpointRecord(bookId, bookIdentity, storage)?.positionSec ?? null;
}

export function readPlaybackResumePosition(
  book: Pick<LibraryBookDetail, "id" | "created_at" | "source_path" | "library_id" | "library_epoch" | "playback_position_sec" | "playback_status" | "chapters">,
  storage: PlaybackCheckpointStorage | null = defaultStorage(),
): number {
  const record = readCheckpointRecord(book.id, playbackCheckpointBookIdentity(book), storage);
  if (!record) return Math.max(0, Number(book.playback_position_sec) || 0);
  const anchor = record.chapterAnchor;
  if (
    anchor && Number.isInteger(anchor.chapterNumber) && anchor.chapterNumber > 0 &&
    Number.isFinite(anchor.secondsWithinChapter) && anchor.secondsWithinChapter >= 0 &&
    typeof anchor.chapterSet === "string" && anchor.chapterSet !== chapterSetIdentity(book.chapters)
  ) {
    return resolveChapterPlaybackAnchor(book.chapters, anchor, book.playback_status !== "chapter_audio")
      ?? record.positionSec;
  }
  return record.positionSec;
}

export function writePlaybackCheckpoint(
  bookId: number,
  positionSec: number,
  bookIdentity: string,
  storage: PlaybackCheckpointStorage | null = defaultStorage(),
  chapterAnchor?: ChapterPlaybackAnchor | null,
): boolean {
  const key = checkpointKey(bookId, bookIdentity);
  if (!key || !bookIdentity || !storage || !Number.isFinite(positionSec) || positionSec < 0) {
    return false;
  }

  const record: PlaybackCheckpointRecord = {
    version: 3,
    bookIdentity,
    positionSec,
    updatedAtMs: Date.now(),
    ...(chapterAnchor ? { chapterAnchor } : {}),
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
  bookIdentity?: string,
): void {
  if (!storage || !Number.isInteger(bookId) || bookId <= 0) return;
  try {
    storage.removeItem(`${LEGACY_CHECKPOINT_PREFIX}${bookId}`);
    if (bookIdentity) {
      storage.removeItem(checkpointKey(bookId, bookIdentity)!);
    } else {
      for (let index = (storage.length ?? 0) - 1; index >= 0; index -= 1) {
        const key = storage.key?.(index);
        if (key?.startsWith(`${PLAYBACK_CHECKPOINT_PREFIX}${bookId}.`)) storage.removeItem(key);
      }
    }
  } catch {
    // Playback remains usable when browser storage is unavailable.
  }
}

export function clearLibraryPlaybackCheckpoints(
  storage: Pick<Storage, "length" | "key" | "removeItem"> | null = defaultStorage(),
): void {
  if (!storage) return;
  try {
    for (let index = storage.length - 1; index >= 0; index -= 1) {
      const key = storage.key(index);
      if (key?.startsWith(PLAYBACK_CHECKPOINT_PREFIX) || key?.startsWith(LEGACY_CHECKPOINT_PREFIX)) storage.removeItem(key);
    }
  } catch {
    // Playback remains usable when browser storage is unavailable.
  }
}
