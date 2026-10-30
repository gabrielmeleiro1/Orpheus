import { describe, expect, it } from "vitest";

import {
  clearPlaybackCheckpoint,
  playbackCheckpointBookIdentity,
  readPlaybackCheckpoint,
  readPlaybackResumePosition,
  writePlaybackCheckpoint,
} from "./playbackCheckpoint";
import { chapterPlaybackAnchor } from "./chapterPositionModel";

class MemoryStorage {
  private readonly values = new Map<string, string>();

  get length(): number { return this.values.size; }

  key(index: number): string | null { return [...this.values.keys()][index] ?? null; }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

describe("playback checkpoints", () => {
  function createPartialBook() {
    return {
      id: 1,
      library_id: "library",
      library_epoch: "epoch",
      source_path: null,
      playback_status: "chapter_audio" as const,
      playback_position_sec: 15,
      chapters: [
        { id: 1, number: 1, title: "one", duration_ms: 10000, mp3_path: "1.mp3" },
        { id: 2, number: 2, title: "two", duration_ms: null, mp3_path: null },
        { id: 3, number: 3, title: "three", duration_ms: 10000, mp3_path: "3.mp3" },
      ],
    };
  }

  it("keeps checkpoints separate across restores and different libraries with identical books", () => {
    const storage = new MemoryStorage();
    const book = { ...createPartialBook(), library_id: "one", library_epoch: "before" };
    writePlaybackCheckpoint(book.id, 90, playbackCheckpointBookIdentity(book), storage);
    const restored = { ...book, library_epoch: "after" };
    expect(readPlaybackResumePosition(restored, storage)).toBe(15);
    writePlaybackCheckpoint(book.id, 25, playbackCheckpointBookIdentity(restored), storage);
    expect(readPlaybackResumePosition(book, storage)).toBe(90);
    expect(readPlaybackResumePosition(restored, storage)).toBe(25);
    expect(readPlaybackResumePosition({ ...restored, library_id: "two" }, storage)).toBe(15);
  });

  it("prefers the server position when a legacy checkpoint cannot prove its epoch", () => {
    const storage = new MemoryStorage();
    const book = createPartialBook();
    storage.setItem("orpheus.playback.v2.1", JSON.stringify({
      version: 2, bookIdentity: JSON.stringify([1, "", ""]), positionSec: 90, updatedAtMs: Date.now(),
    }));
    expect(readPlaybackResumePosition(book, storage)).toBe(15);
    expect(readPlaybackResumePosition({ ...book, library_id: "one", library_epoch: "after" }, storage)).toBe(15);
    expect(readPlaybackResumePosition({ ...book, library_id: "one", library_epoch: "again" }, storage)).toBe(15);
  });

  it("does not create an unscoped checkpoint when the server identity is unavailable", () => {
    const storage = new MemoryStorage();
    const book = { ...createPartialBook(), library_id: undefined, library_epoch: undefined };
    expect(writePlaybackCheckpoint(book.id, 90, playbackCheckpointBookIdentity(book), storage)).toBe(false);
    expect(readPlaybackResumePosition(book, storage)).toBe(15);
  });

  it("clears the selected identity without deleting another epoch's position", () => {
    const storage = new MemoryStorage();
    const book = createPartialBook();
    const restored = { ...book, library_epoch: "restored" };
    const originalIdentity = playbackCheckpointBookIdentity(book);
    const restoredIdentity = playbackCheckpointBookIdentity(restored);
    writePlaybackCheckpoint(book.id, 90, originalIdentity, storage);
    writePlaybackCheckpoint(book.id, 25, restoredIdentity, storage);
    clearPlaybackCheckpoint(book.id, storage, restoredIdentity);
    expect(readPlaybackCheckpoint(book.id, originalIdentity, storage)).toBe(90);
    expect(readPlaybackCheckpoint(book.id, restoredIdentity, storage)).toBeNull();
  });

  it("stores a chapter anchor alongside the absolute position and resolves it after repair", () => {
    const storage = new MemoryStorage();
    const book = createPartialBook();
    const identity = playbackCheckpointBookIdentity(book);
    const anchor = chapterPlaybackAnchor(book.chapters, 15, 3);
    writePlaybackCheckpoint(book.id, 15, identity, storage, anchor);
    const repaired = {
      ...book,
      playback_status: "ready" as const,
      chapters: book.chapters.map((chapter) => chapter.number === 2
        ? { ...chapter, duration_ms: 20000, mp3_path: "2.mp3" }
        : chapter),
    };

    expect(readPlaybackCheckpoint(book.id, identity, storage)).toBe(15);
    expect(readPlaybackResumePosition(book, storage)).toBe(15);
    expect(readPlaybackResumePosition(repaired, storage)).toBe(35);
  });

  it("uses the absolute checkpoint when the chapter set is unchanged", () => {
    const storage = new MemoryStorage();
    const book = { ...createPartialBook(), playback_position_sec: 7 };
    const anchor = chapterPlaybackAnchor(book.chapters, 15, 3)!;
    writePlaybackCheckpoint(book.id, 15, playbackCheckpointBookIdentity(book), storage, {
      ...anchor,
      secondsWithinChapter: 1,
    });

    expect(readPlaybackResumePosition(book, storage)).toBe(15);
  });

  it.each([
    { chapterNumber: 3, secondsWithinChapter: -5, chapterSet: "old" },
    { chapterNumber: 3, secondsWithinChapter: "5", chapterSet: "old" },
    { chapterNumber: 3.5, secondsWithinChapter: 5, chapterSet: "old" },
    { chapterNumber: 3, secondsWithinChapter: 5 },
    { chapterNumber: 4, secondsWithinChapter: 5, chapterSet: "old" },
    { chapterNumber: 2, secondsWithinChapter: 5, chapterSet: "old" },
  ])("uses the absolute checkpoint when the chapter anchor cannot be resolved: %j", (chapterAnchor) => {
    const storage = new MemoryStorage();
    const book = { ...createPartialBook(), playback_position_sec: 7 };
    storage.setItem(`orpheus.playback.v3.1.${encodeURIComponent(playbackCheckpointBookIdentity(book))}`, JSON.stringify({
      version: 3,
      bookIdentity: playbackCheckpointBookIdentity(book),
      positionSec: 15,
      chapterAnchor,
    }));

    expect(readPlaybackResumePosition(book, storage)).toBe(15);
  });

  it("uses the server position without a matching local checkpoint and supports absolute-only checkpoints", () => {
    const storage = new MemoryStorage();
    const book = createPartialBook();
    expect(readPlaybackResumePosition(book, storage)).toBe(15);
    writePlaybackCheckpoint(book.id, 5, "other-book", storage);
    expect(readPlaybackResumePosition(book, storage)).toBe(15);
    writePlaybackCheckpoint(book.id, 12.5, playbackCheckpointBookIdentity(book), storage);
    expect(readPlaybackResumePosition(book, storage)).toBe(12.5);
    expect(readPlaybackResumePosition(book, null)).toBe(15);
  });

  it("stores independent fractional positions for each book", () => {
    const storage = new MemoryStorage();

    expect(writePlaybackCheckpoint(1, 183.75, "book-a", storage)).toBe(true);
    expect(writePlaybackCheckpoint(2, 42.5, "book-b", storage)).toBe(true);

    expect(readPlaybackCheckpoint(1, "book-a", storage)).toBe(183.75);
    expect(readPlaybackCheckpoint(2, "book-b", storage)).toBe(42.5);
  });

  it("clears only the requested book", () => {
    const storage = new MemoryStorage();
    writePlaybackCheckpoint(1, 10, "book-a", storage);
    writePlaybackCheckpoint(2, 20, "book-b", storage);

    clearPlaybackCheckpoint(1, storage);

    expect(readPlaybackCheckpoint(1, "book-a", storage)).toBeNull();
    expect(readPlaybackCheckpoint(2, "book-b", storage)).toBe(20);
  });

  it("does not restore a checkpoint from a different book with the same numeric id", () => {
    const storage = new MemoryStorage();
    writePlaybackCheckpoint(1, 183.75, "original-library-book", storage);

    expect(readPlaybackCheckpoint(1, "replacement-library-book", storage)).toBeNull();
  });

  it("rejects malformed records and invalid positions", () => {
    const storage = new MemoryStorage();
    const records = [
      "{broken",
      JSON.stringify({ version: 2, bookIdentity: "book-a", positionSec: 10 }),
      JSON.stringify({ version: 3, bookIdentity: "other-book", positionSec: 10 }),
      JSON.stringify({ version: 3, bookIdentity: "book-a", positionSec: -1 }),
      JSON.stringify({ version: 3, bookIdentity: "book-a", positionSec: "10" }),
    ];
    for (const raw of records) {
      storage.setItem("orpheus.playback.v3.1.book-a", JSON.stringify({
        version: 3, bookIdentity: "book-a", positionSec: 10,
      }));
      expect(readPlaybackCheckpoint(1, "book-a", storage)).toBe(10);
      storage.setItem("orpheus.playback.v3.1.book-a", raw);
      expect(readPlaybackCheckpoint(1, "book-a", storage)).toBeNull();
    }
    expect(writePlaybackCheckpoint(4, Number.NaN, "book-d", storage)).toBe(false);
    expect(writePlaybackCheckpoint(4, -1, "book-d", storage)).toBe(false);
  });

  it("fails soft when browser storage is unavailable", () => {
    const storage = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };

    expect(readPlaybackCheckpoint(1, "book-a", storage)).toBeNull();
    expect(writePlaybackCheckpoint(1, 10, "book-a", storage)).toBe(false);
    expect(() => clearPlaybackCheckpoint(1, storage)).not.toThrow();
  });
});
