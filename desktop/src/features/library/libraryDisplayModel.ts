import { formatClock } from "../../format/time";
import type { LibraryBookDetail, LibraryBookSummary } from "../../types/api";

interface BookSummaryView {
  title: string;
  meta: string;
  extra: string;
  raw: string;
}

const EMPTY_SUMMARY: BookSummaryView = {
  title: "No book selected",
  meta: "Select a book to inspect details.",
  extra: "—",
  raw: "Select a book to inspect details.",
};

// Preformatted display strings for the selected-book detail header.
export function buildBookSummary(detail: LibraryBookDetail | null): BookSummaryView {
  if (!detail) {
    return EMPTY_SUMMARY;
  }

  const chapterCount = Array.isArray(detail.chapters) ? detail.chapters.length : 0;
  const resumeTime = formatClock(detail.playback_position_sec || 0);

  return {
    title: detail.title || "Unknown",
    meta: `${detail.author || "Unknown"} · ${chapterCount} ${chapterCount === 1 ? "chapter" : "chapters"}`,
    extra:
      detail.playback_path && detail.playback_url
        ? detail.playback_position_sec > 0
          ? `Playback ready · resume ${resumeTime}`
          : "Playback ready"
        : "Playback output pending",
    raw: JSON.stringify(detail, null, 2),
  };
}

/**
 * Why a book is not a normal, playable book, in a few words; null when it is.
 * Books from backends that do not report audio_available are judged by their
 * chapter counts alone.
 */
type BookAvailability = Pick<LibraryBookSummary, "total_chapters" | "audio_available"> & {
  completed_chapters?: number;
  source_available?: boolean;
};

function availabilityProblem(book: BookAvailability): "not_converted" | "audio_missing" | "unfinished" | null {
  const completed = book.completed_chapters ?? 0;
  const total = book.total_chapters || 0;
  if (completed === 0 && book.audio_available !== true && (total > 0 || book.audio_available === false)) {
    return "not_converted";
  }
  if (book.audio_available === false) return "audio_missing";
  if (total > 0 && completed < total) return "unfinished";
  return null;
}

/** The one description of a problem book, shared by its card and its album page. */
export function bookAvailabilityNote(book: BookAvailability): string | null {
  switch (availabilityProblem(book)) {
    case "not_converted": return "Not converted";
    case "audio_missing": return "Audio missing";
    case "unfinished": return `Unfinished · ${book.completed_chapters ?? 0}/${book.total_chapters || 0} chapters`;
    default: return null;
  }
}

/**
 * What can be done about the problem bookAvailabilityNote names. Repair audio
 * needs the source file; without it the only ways out are deleting the book or
 * converting the original file again.
 */
export function bookAvailabilityAdvice(book: BookAvailability): string | null {
  const problem = availabilityProblem(book);
  if (!problem) return null;
  const [repairs, done] = REPAIR_WORDING[problem];
  return book.source_available === false
    ? `The source file is missing, so the audio cannot be ${done}. Delete the book, or create it again from the original file.`
    : `Repair audio ${repairs}.`;
}

const REPAIR_WORDING = {
  not_converted: ["creates the audio", "created"],
  audio_missing: ["rebuilds it", "rebuilt"],
  unfinished: ["finishes it", "finished"],
} as const;
