import { formatClock } from "../../format/time";
import type { LibraryBookDetail, LibraryBookSummary } from "../../types/api";

export interface BookSummaryView {
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

// One-line meta shown under each book row in the library list.
export function formatSummaryMeta(item: LibraryBookSummary): string {
  const mode = item.conversion_mode === "document_audio" ? "document" : "audiobook";
  const source = item.source_available ? "" : " · source missing";
  return `${item.author || "unknown author"} · ${mode} · ${item.completed_chapters || 0}/${item.total_chapters || 0}${source}`;
}

// Preformatted display strings for the selected-book detail header.
export function buildBookSummary(detail: LibraryBookDetail | null): BookSummaryView {
  if (!detail) {
    return EMPTY_SUMMARY;
  }

  const chapterCount = Array.isArray(detail.chapters) ? detail.chapters.length : 0;
  const resumeTime = formatClock(detail.playback_position_sec || 0);

  return {
    title: detail.title || "unknown",
    meta: `${detail.author || "unknown"} · ${chapterCount} ${chapterCount === 1 ? "chapter" : "chapters"}`,
    extra:
      detail.playback_path && detail.playback_url
        ? detail.playback_position_sec > 0
          ? `Playback ready · resume ${resumeTime}`
          : "Playback ready"
        : "Playback output pending",
    raw: JSON.stringify(detail, null, 2),
  };
}
