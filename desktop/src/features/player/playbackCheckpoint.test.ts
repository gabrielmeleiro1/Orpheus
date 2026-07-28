import { describe, expect, it } from "vitest";

import {
  clearPlaybackCheckpoint,
  readPlaybackCheckpoint,
  writePlaybackCheckpoint,
} from "./playbackCheckpoint";

class MemoryStorage {
  private readonly values = new Map<string, string>();

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
    storage.setItem("orpheus.playback.v2.1", "{broken");
    storage.setItem("orpheus.playback.v2.2", JSON.stringify({ version: 1, positionSec: 10 }));
    storage.setItem("orpheus.playback.v2.3", JSON.stringify({
      version: 2,
      bookIdentity: "book-c",
      positionSec: -1,
    }));

    expect(readPlaybackCheckpoint(1, "book-a", storage)).toBeNull();
    expect(readPlaybackCheckpoint(2, "book-b", storage)).toBeNull();
    expect(readPlaybackCheckpoint(3, "book-c", storage)).toBeNull();
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
