import type { LibraryChapterDetail } from "../../types/api";

export interface ChapterPlaybackAnchor {
  chapterNumber: number;
  secondsWithinChapter: number;
  chapterSet: string;
}

export function chapterSetIdentity(chapters: LibraryChapterDetail[]): string {
  return JSON.stringify(chapters.map((chapter) => [
    chapter.number,
    chapter.duration_ms ?? 0,
    Boolean(chapter.mp3_path),
    chapter.packaged_start_ms ?? null,
  ]));
}

export function chapterPlaybackAnchor(
  chapters: LibraryChapterDetail[],
  positionSec: number,
  chapterNumber?: number | null,
): ChapterPlaybackAnchor | null {
  const chapter = chapterNumber != null
    ? chapters.find((item) => item.number === chapterNumber)
    : playableChapterForPosition(chapters, positionSec);
  if (!chapter) return null;
  return {
    chapterNumber: chapter.number,
    secondsWithinChapter: Math.max(0, positionSec - chapterStartSeconds(chapters, chapter.number, false)),
    chapterSet: chapterSetIdentity(chapters),
  };
}

export function resolveChapterPlaybackAnchor(
  chapters: LibraryChapterDetail[],
  anchor: ChapterPlaybackAnchor,
  usePackagedOffsets = true,
): number | null {
  const chapter = chapters.find((item) => item.number === anchor.chapterNumber);
  if (!chapter || !(Number(chapter.duration_ms) > 0)) return null;
  return chapterStartSeconds(chapters, chapter.number, usePackagedOffsets)
    + Math.min(anchor.secondsWithinChapter, Number(chapter.duration_ms) / 1000);
}

function chapterStarts(chapters: LibraryChapterDetail[], usePackagedOffsets: boolean): number[] {
  let cursor = 0;
  return chapters.map((chapter) => {
    const packagedStart = chapter.packaged_start_ms;
    const start = usePackagedOffsets && packagedStart != null && Number.isFinite(packagedStart) && packagedStart >= 0
      ? packagedStart / 1000
      : cursor;
    cursor = start + Math.max(0, Number(chapter.duration_ms || 0) / 1000);
    return start;
  });
}

// Seconds from the start of the book to the beginning of the target chapter.
export function chapterStartSeconds(
  chapters: LibraryChapterDetail[],
  targetNumber: number,
  usePackagedOffsets = true,
): number {
  const index = chapters.findIndex((chapter) => chapter.number === targetNumber);
  return index < 0 ? 0 : chapterStarts(chapters, usePackagedOffsets)[index];
}

// Chapter containing the elapsed offset, falling back to the last non-empty chapter.
export function currentChapterForPosition(
  chapters: LibraryChapterDetail[],
  elapsedSeconds: number,
  usePackagedOffsets = true,
): LibraryChapterDetail | null {
  return createChapterTimeline(chapters, usePackagedOffsets).at(elapsedSeconds);
}

/** Missing audio retains its duration on the persisted book timeline. Locate
 * the saved chapter before skipping unavailable files, or resume can jump ahead. */
export function playableChapterForPosition(
  chapters: LibraryChapterDetail[],
  elapsedSeconds: number,
): LibraryChapterDetail | null {
  const current = currentChapterForPosition(chapters, elapsedSeconds, false);
  const index = chapters.findIndex((chapter) => chapter === current);
  for (let next = Math.max(0, index); next < chapters.length; next += 1) {
    if (chapters[next].mp3_path) return chapters[next];
  }
  for (let previous = index - 1; previous >= 0; previous -= 1) {
    if (chapters[previous].mp3_path) return chapters[previous];
  }
  return null;
}

/** Build once per chapter snapshot, then look up audio ticks without allocating
 * or scanning the whole book. Irregular legacy offsets retain the old lookup. */
export function createChapterTimeline(chapters: LibraryChapterDetail[], usePackagedOffsets = true) {
  const starts = chapterStarts(chapters, usePackagedOffsets);
  const ordered = starts.every((start, index) => Number.isFinite(start) && (index === 0 || start >= starts[index - 1]));
  const fallback = chapters[chapters.length - 1] ?? null;
  return {
    at(elapsedSeconds: number): LibraryChapterDetail | null {
      if (!ordered) {
        for (let index = 0; index < chapters.length; index += 1) {
          const end = starts[index + 1] ?? starts[index] + Math.max(0, Number(chapters[index].duration_ms || 0) / 1000);
          if (elapsedSeconds >= starts[index] && elapsedSeconds < end) return chapters[index];
        }
        return fallback;
      }
      // Upper bound skips zero-length chapters sharing a boundary.
      let low = 0;
      let high = starts.length;
      while (low < high) {
        const middle = low + Math.floor((high - low) / 2);
        if (starts[middle] <= elapsedSeconds) low = middle + 1;
        else high = middle;
      }
      return low > 0 ? chapters[low - 1] : fallback;
    },
  };
}

export interface ChapterMark {
  number: number;
  title: string;
  pct: number;
}

/** Where each chapter after the first begins along a whole-book scrubber, as percentages. */
export function chapterMarks(chapters: LibraryChapterDetail[], durationSec: number): ChapterMark[] {
  if (!(durationSec > 0) || chapters.length < 2) return [];
  const starts = chapterStarts(chapters, true);
  return chapters.flatMap((chapter, index) => {
    const pct = (starts[index] / durationSec) * 100;
    // The first chapter starts at zero; marks past the end mean the durations disagree.
    return index > 0 && Number.isFinite(pct) && pct > 0 && pct < 100
      ? [{ number: chapter.number, title: chapter.title || `Chapter ${chapter.number}`, pct }]
      : [];
  });
}
