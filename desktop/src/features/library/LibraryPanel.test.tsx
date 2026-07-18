import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { LibraryBookDetail, LibraryBookSummary } from "../../types/api";
import { LibraryPanel } from "./LibraryPanel";
import type { UseLibraryResult } from "./useLibrary";

const selectedBook: LibraryBookDetail = {
  id: 1,
  title: "Book",
  author: "Author",
  source_path: null,
  source_type: "epub",
  conversion_mode: "audiobook",
  tags: "",
  favorite: false,
  source_available: true,
  total_chapters: 0,
  playback_path: null,
  playback_status: "ready",
  playback_position_sec: 0,
  chapters: [],
};

const selectedSummary: LibraryBookSummary = {
  id: selectedBook.id,
  title: selectedBook.title,
  author: selectedBook.author,
  source_path: selectedBook.source_path,
  conversion_mode: selectedBook.conversion_mode,
  tags: selectedBook.tags,
  favorite: selectedBook.favorite,
  source_available: selectedBook.source_available,
  total_chapters: selectedBook.total_chapters,
  completed_chapters: 0,
};

function libraryModel(): UseLibraryResult {
  const asyncNoop = vi.fn(async () => undefined);
  return {
    searchQuery: "",
    setSearchQuery: vi.fn(),
    modeFilter: "all",
    setModeFilter: vi.fn(),
    items: [selectedSummary],
    indexedCount: 1,
    selectedBookId: 1,
    selectedBookIndex: 0,
    selectedBookDetail: selectedBook,
    selectedBookPlayable: false,
    loadingList: false,
    loadingDetail: false,
    errorMessage: "",
    statusMessage: "",
    bookSummaryTitle: "Book",
    bookSummaryMeta: "Author",
    bookSummaryExtra: "",
    bookDetailText: "",
    maintenanceReport: null,
    maintenanceLoading: false,
    maintenanceMessage: "",
    canOpenAppData: false,
    refreshLibrary: asyncNoop,
    selectBook: vi.fn(async () => selectedBook),
    selectRelative: vi.fn(async () => selectedBook),
    updateSelectedBookMetadata: asyncNoop,
    loadCleaningPreview: vi.fn(async () => null),
    repairSelectedBook: asyncNoop,
    loadMaintenanceDiagnostics: asyncNoop,
    backupDatabase: asyncNoop,
    cleanupStorage: asyncNoop,
    openSelectedBookFolder: asyncNoop,
    openAppDataFolder: asyncNoop,
    deleteSelectedBook: vi.fn(async () => false),
    setSelectedPlaybackPosition: vi.fn(),
  };
}

describe("LibraryPanel", () => {
  it("associates metadata labels with their editable controls", () => {
    const html = renderToString(<LibraryPanel hidden={false} library={libraryModel()} />);

    expect(html).toContain('<label class="form-row-label" for="book-title-edit">Title</label>');
    expect(html).toContain('<label class="form-row-label" for="book-author-edit">Author</label>');
    expect(html).toContain('<label class="form-row-label" for="book-tags-edit">Tags</label>');
  });
});
