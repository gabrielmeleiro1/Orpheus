// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
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

    expect(html).toContain('<label class="field-label" for="book-title-edit">Title</label>');
    expect(html).toContain('<label class="field-label" for="book-author-edit">Author</label>');
    expect(html).toContain('<label class="field-label" for="book-tags-edit">Tags</label>');
  });

  it("shows a quiet empty state in the detail pane when nothing is selected", () => {
    const model = libraryModel();
    model.selectedBookDetail = null;
    model.selectedBookId = null;
    model.selectedBookIndex = -1;

    const html = renderToString(<LibraryPanel hidden={false} library={model} />);

    expect(html).toContain("No book selected");
    // No disabled metadata form or destructive actions before a book is chosen.
    expect(html).not.toContain('id="book-title-edit"');
    expect(html).not.toContain('id="library-delete-btn"');
  });

  it("omits the pager when the list holds a single book", () => {
    const html = renderToString(<LibraryPanel hidden={false} library={libraryModel()} />);

    expect(html).not.toContain("library-pager");
  });

  it("keeps visible details inert while a different selected book is loading", () => {
    const model = libraryModel();
    model.selectedBookId = 2;
    model.loadingDetail = true;
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LibraryPanel hidden={false} library={model} />);

    const detailPane = container.querySelector(".library-detail-pane");
    expect(detailPane?.classList.contains("is-loading")).toBe(true);
    expect(detailPane?.getAttribute("aria-busy")).toBe("true");
    expect((container.querySelector("#book-title-edit") as HTMLInputElement).disabled).toBe(true);
    expect((container.querySelector("#library-save-metadata-btn") as HTMLButtonElement).disabled).toBe(true);
    expect((container.querySelector("#library-delete-btn") as HTMLButtonElement).disabled).toBe(true);
  });

  it("requires an inline confirmation before deleting the selected book", async () => {
    const model = libraryModel();
    const container = document.createElement("div");
    document.body.append(container);
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const root = createRoot(container);

    try {
      await act(async () => {
        root.render(<LibraryPanel hidden={false} library={model} />);
      });

      await act(async () => {
        (container.querySelector("#library-delete-btn") as HTMLButtonElement).click();
      });

      expect(model.deleteSelectedBook).not.toHaveBeenCalled();
      expect(container.querySelector("#library-delete-cancel-btn")).not.toBeNull();
      expect(container.querySelector("#library-delete-confirm-btn")?.textContent).toBe("Delete book");

      await act(async () => {
        (container.querySelector("#library-delete-cancel-btn") as HTMLButtonElement).click();
      });
      expect(model.deleteSelectedBook).not.toHaveBeenCalled();
      expect(container.querySelector("#library-delete-confirm-btn")).toBeNull();

      await act(async () => {
        (container.querySelector("#library-delete-btn") as HTMLButtonElement).click();
      });
      await act(async () => {
        (container.querySelector("#library-delete-confirm-btn") as HTMLButtonElement).click();
      });
      expect(model.deleteSelectedBook).toHaveBeenCalledTimes(1);
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });
});
