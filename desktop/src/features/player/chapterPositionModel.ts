import type { LibraryChapterDetail } from "../../types/api";

// Seconds from the start of the book to the beginning of the target chapter.
export function chapterStartSeconds(chapters: LibraryChapterDetail[], targetNumber: number): number {
  let cursor = 0;
  for (const chapter of chapters) {
    if (chapter.number === targetNumber) {
      return cursor;
    }
    cursor += Math.max(0, Number(chapter.duration_ms || 0) / 1000);
  }
  return 0;
}

// Chapter containing the elapsed offset, falling back to the last non-empty chapter.
export function currentChapterForPosition(
  chapters: LibraryChapterDetail[],
  elapsedSeconds: number,
): LibraryChapterDetail | null {
  if (chapters.length === 0) {
    return null;
  }

  let cursor = 0;
  let fallback = chapters[0];
  for (const chapter of chapters) {
    const duration = Math.max(0, Number(chapter.duration_ms || 0) / 1000);
    if (duration <= 0) {
      fallback = chapter;
      continue;
    }
    if (elapsedSeconds >= cursor && elapsedSeconds < cursor + duration) {
      return chapter;
    }
    cursor += duration;
    fallback = chapter;
  }
  return fallback;
}
