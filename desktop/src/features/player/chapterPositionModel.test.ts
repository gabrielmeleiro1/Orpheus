import { describe, expect, it } from "vitest";

import type { LibraryChapterDetail } from "../../types/api";
import { chapterMarks, chapterStartSeconds, createChapterTimeline, currentChapterForPosition, playableChapterForPosition } from "./chapterPositionModel";

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

describe("playableChapterForPosition", () => {
  const chapters = [
    { ...chapter(1, 10_000), mp3_path: "1.mp3" },
    chapter(2, 20_000),
    { ...chapter(3, 20_000), mp3_path: "3.mp3" },
    { ...chapter(4, 20_000), mp3_path: "4.mp3" },
    chapter(5, 20_000),
  ];

  it("preserves missing chapters' offsets when locating resume audio", () => {
    expect(playableChapterForPosition(chapters, 33)?.number).toBe(3);
    expect(playableChapterForPosition(chapters, 53)?.number).toBe(4);
  });

  it("skips unavailable audio forward, or back to the last playable chapter at the end", () => {
    expect(playableChapterForPosition(chapters, 15)?.number).toBe(3);
    expect(playableChapterForPosition(chapters, 75)?.number).toBe(4);
    expect(playableChapterForPosition([chapter(1, 10_000)], 0)).toBeNull();
    expect(playableChapterForPosition([], 0)).toBeNull();
  });
});

const packagedChapters = [
  { ...chapter(1, 300), packaged_start_ms: 0 },
  { ...chapter(2, 400), packaged_start_ms: 350 },
  { ...chapter(3, 500), packaged_start_ms: 800 },
];

describe("packaged chapter positions", () => {
  it("seeks to the encoded chapter starts", () => {
    expect(chapterStartSeconds(packagedChapters, 2)).toBe(0.35);
    expect(chapterStartSeconds(packagedChapters, 3)).toBe(0.8);
  });

  it("highlights chapters using the next encoded boundary", () => {
    expect(currentChapterForPosition(packagedChapters, 0.32)?.number).toBe(1);
    expect(currentChapterForPosition(packagedChapters, 0.35)?.number).toBe(2);
    expect(currentChapterForPosition(packagedChapters, 0.75)?.number).toBe(2);
    expect(currentChapterForPosition(packagedChapters, 0.8)?.number).toBe(3);
    expect(currentChapterForPosition(packagedChapters, 10)?.number).toBe(3);
  });

  it("maps a saved packaged position to the chapter and local offset", () => {
    const savedPosition = 0.85;
    const current = currentChapterForPosition(packagedChapters, savedPosition)!;
    expect(current.number).toBe(3);
    expect(savedPosition - chapterStartSeconds(packagedChapters, current.number)).toBeCloseTo(0.05);
  });

  it("uses summed durations when stored offsets are null", () => {
    const legacy = packagedChapters.map((item) => ({ ...item, packaged_start_ms: null }));
    expect(chapterStartSeconds(legacy, 3)).toBe(0.7);
    expect(currentChapterForPosition(legacy, 0.72)?.number).toBe(3);
  });
});

it("keeps chapter-file positions on the source duration timeline", () => {
  expect(chapterStartSeconds(packagedChapters, 3, false)).toBe(0.7);
  expect(currentChapterForPosition(packagedChapters, 0.72, false)?.number).toBe(3);
});

it("skips empty chapters sharing the next packaged boundary", () => {
  const chapters = [
    { ...chapter(1, 300), packaged_start_ms: 0 },
    { ...chapter(2, 0), packaged_start_ms: 350 },
    { ...chapter(3, 400), packaged_start_ms: 350 },
  ];
  expect(currentChapterForPosition(chapters, 0.32)?.number).toBe(1);
  expect(currentChapterForPosition(chapters, 0.35)?.number).toBe(3);
});

describe("reusable chapter timeline", () => {
  it("keeps legacy behavior for irregular packaged offsets and invalid positions", () => {
    const chapters = [
      { ...chapter(1, 10_000), packaged_start_ms: 10_000 },
      { ...chapter(2, 10_000), packaged_start_ms: 0 },
      { ...chapter(3, 10_000), packaged_start_ms: 20_000 },
    ];
    const timeline = createChapterTimeline(chapters);
    expect(timeline.at(5)?.number).toBe(2);
    expect(timeline.at(15)?.number).toBe(2);
    expect(timeline.at(20)?.number).toBe(3);
    for (const position of [-1, Number.NaN, Infinity]) expect(timeline.at(position)?.number).toBe(3);
    expect(createChapterTimeline([]).at(0)).toBeNull();
  });
});

describe("chapterMarks", () => {
  const chapter = (number: number, durationMs: number, packagedStartMs?: number) => ({
    id: number, number, title: `Chapter ${number}`, duration_ms: durationMs, mp3_path: null,
    packaged_start_ms: packagedStartMs ?? null,
  });

  it("marks where each chapter after the first begins", () => {
    expect(chapterMarks([chapter(1, 60_000), chapter(2, 30_000), chapter(3, 10_000)], 100)).toEqual([
      { number: 2, title: "Chapter 2", pct: 60 },
      { number: 3, title: "Chapter 3", pct: 90 },
    ]);
  });

  it("prefers packaged offsets and drops marks outside the scrubber", () => {
    expect(chapterMarks([chapter(1, 60_000, 0), chapter(2, 60_000, 50_000), chapter(3, 60_000, 200_000)], 100)
      .map((mark) => mark.pct)).toEqual([50]);
  });

  it("has nothing to mark for one chapter or an unknown length", () => {
    expect(chapterMarks([chapter(1, 60_000)], 60)).toEqual([]);
    expect(chapterMarks([chapter(1, 60_000), chapter(2, 60_000)], 0)).toEqual([]);
  });
});
