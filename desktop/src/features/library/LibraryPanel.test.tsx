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
    hasMore: false,
    selectedBookId: 1,
    selectedBookIndex: 0,
    selectedBookDetail: selectedBook,
    selectedBookPlayable: false,
    loadingList: false,
    loadingMore: false,
    loadingDetail: false,
    errorMessage: "",
    statusMessage: "",
    bookSummaryTitle: "Book",
    bookSummaryMeta: "Author",
    bookDetailText: "",
    maintenanceReport: null,
    maintenanceLoading: false,
    maintenanceMessage: "",
    canOpenAppData: false,
    refreshLibrary: asyncNoop,
    loadMore: asyncNoop,
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
  it("hides pagination for a single page and keeps it for more results", () => {
    const model = libraryModel();
    model.items = [selectedSummary, { ...selectedSummary, id: 2 }];
    expect(renderToString(<LibraryPanel hidden={false} library={model} />)).not.toContain("library-pager");
    model.hasMore = true;
    expect(renderToString(<LibraryPanel hidden={false} library={model} />)).toContain("library-pager");
    model.hasMore = false;
    model.items = Array.from({ length: 101 }, (_, id) => ({ ...selectedSummary, id }));
    expect(renderToString(<LibraryPanel hidden={false} library={model} />)).toContain("library-pager");
  });

  it("blocks saving a blank title with inline feedback", async () => {
    const model = libraryModel();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} />));
      const input = container.querySelector<HTMLInputElement>("#book-title-edit")!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "   ");
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      expect(container.querySelector("#book-title-error")?.textContent).toBe("Title is required");
      const save = container.querySelector<HTMLButtonElement>("#library-save-metadata-btn")!;
      expect(save.disabled).toBe(true);
      await act(async () => save.click());
      expect(model.updateSelectedBookMetadata).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("explains missing original text for older books", async () => {
    const model = libraryModel();
    model.selectedBookDetail = { ...selectedBook, chapters: [{ id: 1, number: 1, title: "One", duration_ms: 0, mp3_path: null }] };
    model.loadCleaningPreview = vi.fn(async () => ({ book_id: 1, chapter_number: 1, title: "One", available: true, before_text: "", cleaned_text: "Cleaned", llm_input_text: "", trace_rows: [] }));
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} />));
      await act(async () => container.querySelector<HTMLButtonElement>(".chapter-cleaned-btn")!.click());
      expect(container.querySelector('[aria-label="Original text"]')?.textContent).toBe("Original text isn't available for books converted before this version.");
      expect(container.textContent).not.toContain("Loaded cleaning preview");
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("shows full cleaning text in labelled panes and closes the preview", async () => {
    const model = libraryModel();
    model.selectedBookDetail = { ...selectedBook, chapters: [{ id: 1, number: 1, title: "One", duration_ms: 0, mp3_path: null }] };
    const original = "Original ".repeat(400) + "original end";
    const cleaned = "Cleaned ".repeat(400) + "cleaned end";
    model.loadCleaningPreview = vi.fn(async () => ({ book_id: 1, chapter_number: 1, title: "One", available: true, before_text: original, cleaned_text: cleaned, llm_input_text: "", trace_rows: [] }));
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} />));
      await act(async () => (container.querySelector(".chapter-cleaned-btn") as HTMLButtonElement).click());
      expect(container.querySelector('[aria-label="Original text"]')?.textContent).toBe(original);
      expect(container.querySelector('[aria-label="Cleaned text"]')?.textContent).toBe(cleaned);
      await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === "Close preview")!.click());
      expect(container.querySelector(".cleaning-comparison")).toBeNull();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("keeps partial deletion feedback and Open folder visible in an empty library", async () => {
    const model = libraryModel();
    Object.assign(model, { items: [], indexedCount: 0, selectedBookDetail: null, selectedBookIndex: -1, pendingDeletionPath: "temp/delete_staging/trash", openPendingDeletionFolder: vi.fn(async () => {}) });
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} />));
      expect(container.textContent).toContain("Removed from library, but some files couldn't be deleted");
      await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === "Open folder")!.click());
      expect(model.openPendingDeletionFolder).toHaveBeenCalledOnce();
    } finally {
      await act(async () => root.unmount());
    }
  });
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

  it("keeps Next available at a loaded-page boundary and labels counts as loaded", () => {
    const model = libraryModel();
    model.hasMore = true;
    model.indexedCount = 500;

    const container = document.createElement("div");
    container.innerHTML = renderToString(<LibraryPanel hidden={false} library={model} />);

    const next = [...container.querySelectorAll("button")].find((button) => button.textContent === "Next");
    expect(next?.hasAttribute("disabled")).toBe(false);
    expect(container.textContent).toContain("Load more (1 loaded)");
    expect(container.textContent).not.toContain("1 of 500");
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
      expect(container.querySelector(".library-delete-confirmation")?.textContent).toContain("cannot be undone");

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

  it("requires inline confirmation before cleaning storage", async () => {
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
        (container.querySelector("#library-cleanup-btn") as HTMLButtonElement).click();
      });
      expect(model.cleanupStorage).not.toHaveBeenCalled();
      expect(container.querySelector("#library-cleanup-cancel-btn")).not.toBeNull();
      expect(container.querySelector("#library-cleanup-confirm-btn")).not.toBeNull();

      await act(async () => {
        (container.querySelector("#library-cleanup-cancel-btn") as HTMLButtonElement).click();
      });
      expect(model.cleanupStorage).not.toHaveBeenCalled();
      expect(container.querySelector("#library-cleanup-confirm-btn")).toBeNull();

      await act(async () => {
        (container.querySelector("#library-cleanup-btn") as HTMLButtonElement).click();
      });
      await act(async () => {
        (container.querySelector("#library-cleanup-confirm-btn") as HTMLButtonElement).click();
      });
      expect(model.cleanupStorage).toHaveBeenCalledTimes(1);
      expect(container.querySelector("#library-cleanup-confirm-btn")).toBeNull();
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });
});
