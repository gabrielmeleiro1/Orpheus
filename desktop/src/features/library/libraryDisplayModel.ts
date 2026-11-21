import type { LibraryBookSummary } from "../../types/api";

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
