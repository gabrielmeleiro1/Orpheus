import { describe, expect, it } from "vitest";

import type { LibraryChapterDetail } from "../../types/api";
import { chapterStartSeconds, currentChapterForPosition } from "./chapterPositionModel";

function chapter(number: number, durationMs: number | null): LibraryChapterDetail {
  return { id: number, number, title: `Chapter ${number}`, duration_ms: durationMs, mp3_path: null };
}

describe("chapterStartSeconds", () => {
  const chapters = [chapter(1, 60_000), chapter(2, 30_000), chapter(3, 90_000)];

  it("returns 0 for the first chapter", () => {
    expect(chapterStartSeconds(chapters, 1)).toBe(0);
  });

  it("accumulates prior durations in seconds", () => {
    expect(chapterStartSeconds(chapters, 2)).toBe(60);
    expect(chapterStartSeconds(chapters, 3)).toBe(90);
  });

  it("returns 0 for unknown chapter numbers", () => {
    expect(chapterStartSeconds(chapters, 99)).toBe(0);
    expect(chapterStartSeconds([], 1)).toBe(0);
  });

  it("treats missing durations as zero length", () => {
    expect(chapterStartSeconds([chapter(1, null), chapter(2, 40_000)], 2)).toBe(0);
  });
});

describe("currentChapterForPosition", () => {
  const chapters = [chapter(1, 60_000), chapter(2, 30_000), chapter(3, 90_000)];

  it("returns null for an empty list", () => {
    expect(currentChapterForPosition([], 10)).toBeNull();
  });

  it("locates the chapter containing the elapsed offset", () => {
    expect(currentChapterForPosition(chapters, 0)?.number).toBe(1);
    expect(currentChapterForPosition(chapters, 59)?.number).toBe(1);
    expect(currentChapterForPosition(chapters, 60)?.number).toBe(2);
    expect(currentChapterForPosition(chapters, 95)?.number).toBe(3);
  });

  it("falls back to the last chapter when past the end", () => {
    expect(currentChapterForPosition(chapters, 100_000)?.number).toBe(3);
  });

  it("skips zero-length chapters but keeps them as a fallback", () => {
    const withEmpty = [chapter(1, 0), chapter(2, 20_000)];
    expect(currentChapterForPosition(withEmpty, 0)?.number).toBe(2);
  });
});
