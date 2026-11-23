// @vitest-environment happy-dom

import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { useAudioPlayer } from "../player/useAudioPlayer";
import { playbackCheckpointBookIdentity, writePlaybackCheckpoint } from "../player/playbackCheckpoint";
import { useLibrary } from "./useLibrary";
import type { LibraryBookDetail, LibraryBookSummary } from "../../types/api";
import { LibraryMaintenanceSection } from "./LibraryMaintenanceSection";
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
    unfilteredItems: [selectedSummary],
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
    bookDetailText: "",
    maintenanceReport: null,
    maintenanceLoading: false,
    maintenanceMessage: "",
    canOpenAppData: false,
    refreshLibrary: asyncNoop,
    loadMore: asyncNoop,
    selectBook: vi.fn(async () => selectedBook),
    selectBookById: vi.fn(async () => selectedBook),
    selectRelative: vi.fn(async () => selectedBook),
    updateSelectedBookMetadata: asyncNoop,
    loadCleaningPreview: vi.fn(async () => null),
    repairSelectedBook: asyncNoop,
    loadMaintenanceDiagnostics: vi.fn(async () => undefined),
    backupDatabase: asyncNoop,
    cleanupStorage: asyncNoop,
    openSelectedBookFolder: asyncNoop,
    openAppDataFolder: asyncNoop,
    deleteSelectedBook: vi.fn(async () => false),
    setSelectedPlaybackPosition: vi.fn(),
  };
}

const ALBUM = { albumOpen: true, onOpenAlbum: vi.fn(), onCloseAlbum: vi.fn() };

describe("LibraryPanel", () => {
  it("shows whole-book progress while playing separate chapter files", () => {
    const model = libraryModel();
    model.selectedBookPlayable = true;
    model.selectedBookDetail = { ...selectedBook, playback_status: "chapter_audio", chapters: [
      { id: 1, number: 1, title: "One", duration_ms: 60_000, mp3_path: "a.mp3" },
      { id: 2, number: 2, title: "Two", duration_ms: 60_000, mp3_path: "b.mp3" },
    ] };
    const props = { ...ALBUM, hidden: false, library: model, playerLoaded: true,
      currentChapterNumber: 2, elapsedSec: 30, durationSec: 60, onTogglePlay: vi.fn() };
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LibraryPanel {...props} albumOpen={false} />);
    expect(container.querySelector(".continue-progress-text")?.textContent).toBe("75% · 00:30 left");
    container.innerHTML = renderToString(<LibraryPanel {...props} elapsedSec={0} />);
    expect(container.querySelector("#library-play-btn")?.textContent).toContain("Resume");
  });

  it("disables repair when the original source is unavailable", async () => {
    const model = libraryModel();
    model.selectedBookDetail = { ...selectedBook, source_available: false };
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel {...ALBUM} hidden={false} library={model} />));
      const repair = container.querySelector<HTMLButtonElement>("#library-repair-btn")!;
      expect(repair.disabled).toBe(true);
      await act(async () => repair.click());
      expect(model.repairSelectedBook).not.toHaveBeenCalled();
      expect(container.querySelector<HTMLButtonElement>("#library-open-folder-btn")!.disabled).toBe(false);
      expect(container.querySelector("#book-source-status")?.textContent).toBe("Source file missing");
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("describes an unfinished book the same way on its card and its album page", () => {
    const model = libraryModel();
    const unfinished = { total_chapters: 4, completed_chapters: 1 };
    model.items = [{ ...selectedSummary, ...unfinished }];
    model.selectedBookDetail = { ...selectedBook, ...unfinished };
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LibraryPanel {...ALBUM} albumOpen={false} hidden={false} library={model} />);
    const onCard = container.querySelector(".book-card-partial")!.textContent!;
    container.innerHTML = renderToString(<LibraryPanel {...ALBUM} hidden={false} library={model} />);
    expect(onCard).toBe("Unfinished · 1/4 chapters");
    expect(container.querySelector(".album-meta")?.textContent).toContain(onCard);
    expect(container.querySelector("#book-availability-advice")?.textContent).toBe("Repair audio finishes it.");
  });

  it("offers neither Play nor repair advice for a book with no audio and no source file", () => {
    const model = libraryModel();
    const chapters = [{ id: 1, number: 1, title: "Chapter", duration_ms: 60_000, mp3_path: "a.mp3" }];
    model.selectedBookDetail = { ...selectedBook, total_chapters: 1, completed_chapters: 1, playback_position_sec: 30,
      audio_available: false, source_available: false, chapters };
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LibraryPanel {...ALBUM} hidden={false} library={model} onTogglePlay={vi.fn()} />);
    expect(container.querySelector("#library-play-btn")).toBeNull();
    expect(container.querySelector(".album-meta")?.textContent).toContain("Audio missing");
    const advice = container.querySelector("#book-availability-advice")!.textContent!;
    expect(advice).toBe("The source file is missing, so the audio cannot be rebuilt. Delete the book, or create it again from the original file.");

    // With the source file present the same book is repairable, and says so.
    model.selectedBookDetail = { ...model.selectedBookDetail, source_available: true };
    container.innerHTML = renderToString(<LibraryPanel {...ALBUM} hidden={false} library={model} onTogglePlay={vi.fn()} />);
    expect(container.querySelector("#book-availability-advice")?.textContent).toBe("Repair audio rebuilds it.");
    expect(container.querySelector("#library-play-btn")).toBeNull();
  });

  it("shows a length count only near a field's limit and confirms a save beside the Save button", async () => {
    const model = libraryModel();
    const container = document.createElement("div");
    const root = createRoot(container);
    const type = async (selector: string, value: string) => {
      const input = container.querySelector<HTMLInputElement>(selector)!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      return input;
    };
    try {
      await act(async () => root.render(<LibraryPanel {...ALBUM} hidden={false} library={model} />));
      expect(container.querySelector(".field-limit")).toBeNull();
      expect(container.querySelector("#book-title-edit")?.hasAttribute("aria-describedby")).toBe(false);

      const title = await type("#book-title-edit", "x".repeat(200));
      expect(container.querySelector("#book-title-limit")?.textContent).toBe("200/200");
      expect(title.getAttribute("aria-describedby")).toBe("book-title-limit");
      await type("#book-title-edit", "x".repeat(179));
      expect(container.querySelector("#book-title-limit")).toBeNull();
      const tags = await type("#book-tags-edit", "t".repeat(450));
      expect(container.querySelector("#book-tags-limit")?.textContent).toBe("450/500");
      expect(tags.getAttribute("aria-describedby")).toBe("book-tags-limit");
      const author = await type("#book-author-edit", "a".repeat(199));
      expect(container.querySelector("#book-author-limit")?.textContent).toBe("199/200");
      expect(author.getAttribute("aria-describedby")).toBe("book-author-limit");

      const status = container.querySelector(".library-metadata-footer [role='status']")!;
      expect(status.textContent).toBe("");
      const saved = { ...model, statusMessage: "Metadata saved" };
      await act(async () => root.render(<LibraryPanel {...ALBUM} hidden={false} library={saved} />));
      // Unsaved edits are still in the fields, so nothing claims they were saved.
      expect(status.textContent).toBe("");
      saved.selectedBookDetail = { ...selectedBook, title: "x".repeat(179), author: "a".repeat(199), tags: "t".repeat(450) };
      await act(async () => root.render(<LibraryPanel {...ALBUM} hidden={false} library={{ ...saved }} />));
      expect(status.textContent).toBe("Saved");
      expect(status.parentElement?.querySelector("#library-save-metadata-btn")).not.toBeNull();
      // The confirmation appears once, by the button, not also at the top of the page.
      expect(container.textContent).not.toContain("Metadata saved");
    } finally {
      await act(async () => root.unmount());
    }
  });

  it.each(["chapter_audio", "ready"] as const)("gates unavailable chapter files for %s playback", async (status) => {
    const model = libraryModel();
    model.selectedBookPlayable = true;
    model.selectedBookDetail = { ...selectedBook, playback_status: status, chapters: [
      { id: 1, number: 1, title: "Ready chapter", duration_ms: 60_000, mp3_path: "a.mp3" },
      { id: 2, number: 2, title: "Missing chapter", duration_ms: 60_000, mp3_path: null },
    ] };
    const jump = vi.fn();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel {...ALBUM} hidden={false} library={model} onJumpToChapter={jump} />));
      const buttons = container.querySelectorAll<HTMLButtonElement>(".chapter-main");
      expect(buttons[0].disabled).toBe(false);
      expect(buttons[1].disabled).toBe(status === "chapter_audio");
      await act(async () => buttons[1].click());
      expect(jump).toHaveBeenCalledTimes(status === "ready" ? 1 : 0);
      expect(container.querySelectorAll<HTMLButtonElement>(".chapter-cleaned-btn")[1].disabled).toBe(false);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("disables album Play for chapters without a playable audio URL", () => {
    const model = libraryModel();
    model.selectedBookDetail = { ...selectedBook, chapters: [{ id: 1, number: 1, title: "Chapter", duration_ms: 60_000, mp3_path: null }] };
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LibraryPanel {...ALBUM} hidden={false} library={model} onTogglePlay={vi.fn()} />);
    expect(container.querySelector<HTMLButtonElement>("#library-play-btn")!.disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>(".chapter-main")!.disabled).toBe(true);
  });

  it("uses the local playback checkpoint for Continue listening and album Resume", () => {
    const model = libraryModel();
    const detail = { ...selectedBook, library_id: "library", library_epoch: "current", playback_url: "/audio",
      chapters: [{ id: 1, number: 1, title: "Chapter", duration_ms: 120_000, mp3_path: "chapter.mp3" }] };
    model.selectedBookDetail = detail;
    model.selectedBookPlayable = true;
    writePlaybackCheckpoint(detail.id, 60, playbackCheckpointBookIdentity(detail));
    const container = document.createElement("div");
    try {
      container.innerHTML = renderToString(<LibraryPanel {...ALBUM} albumOpen={false} hidden={false} library={model} onTogglePlay={vi.fn()} />);
      expect(container.querySelector('[aria-label="Continue listening"]')).not.toBeNull();
      expect(container.querySelector(".continue-progress-text")?.textContent).toContain("50%");
      container.innerHTML = renderToString(<LibraryPanel {...ALBUM} hidden={false} library={model} onTogglePlay={vi.fn()} />);
      expect(container.querySelector("#library-play-btn")?.textContent).toContain("Resume");
      model.selectedBookDetail = { ...detail, library_epoch: "replacement" };
      container.innerHTML = renderToString(<LibraryPanel {...ALBUM} albumOpen={false} hidden={false} library={model} onTogglePlay={vi.fn()} />);
      expect(container.querySelector('[aria-label="Continue listening"]')).toBeNull();
    } finally { localStorage.clear(); }
  });

  it.each([true, false])("does not close or focus a newer album after an older deletion settles (%s)", async (deleted) => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const model = libraryModel();
    let settle!: (deleted: boolean) => void;
    model.deleteSelectedBook = vi.fn(() => new Promise<boolean>((resolve) => { settle = resolve; }));
    const close = vi.fn();
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const render = (generation: number) => root.render(<LibraryPanel {...ALBUM} hidden={false} library={model} navigationGeneration={generation} onCloseAlbum={close} />);
    try {
      await act(async () => render(0));
      await act(async () => container.querySelector<HTMLButtonElement>("#library-delete-btn")!.click());
      await act(async () => container.querySelector<HTMLButtonElement>("#library-delete-confirm-btn")!.click());
      model.selectedBookId = 2;
      model.selectedBookDetail = { ...selectedBook, id: 2, title: "New album" };
      await act(async () => render(1));
      const focused = container.querySelector<HTMLButtonElement>(".album-back")!;
      focused.focus();
      await act(async () => settle(deleted));
      expect(close).not.toHaveBeenCalled();
      expect(container.querySelector(".album-title")?.textContent).toBe("New album");
      expect(document.activeElement).toBe(focused);
      expect(container.textContent).not.toContain("Book deleted");
      expect(container.textContent).not.toContain("Couldn't delete");
    } finally {
      await act(async () => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });

  it("retires deletion feedback when transport navigation selects another book", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    let settle!: (value: { book_id: number; deleted: boolean }) => void;
    const books = [1, 2, 3].map((id) => ({ ...selectedSummary, id, title: `Book ${id}` }));
    const apiClient = {
      getLibrary: vi.fn().mockResolvedValueOnce({ items: books }).mockResolvedValue({ items: books.slice(1) }),
      getLibraryBook: vi.fn(async (id: number) => ({ ...selectedBook, id, title: `Book ${id}` })),
      deleteBook: vi.fn(() => new Promise((resolve) => { settle = resolve; })),
    } as unknown as ApiClient;
    let library!: UseLibraryResult;
    const close = vi.fn();
    function Probe() {
      library = useLibrary({ apiClient });
      return <LibraryPanel {...ALBUM} hidden={false} library={library} onCloseAlbum={close} />;
    }
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    try {
      await act(async () => root.render(<Probe />));
      await act(async () => container.querySelector<HTMLButtonElement>("#library-delete-btn")!.click());
      await act(async () => container.querySelector<HTMLButtonElement>("#library-delete-confirm-btn")!.click());
      await act(async () => library.selectRelative(1));
      expect(library.selectedBookId).toBe(3);
      const target = container.querySelector<HTMLButtonElement>(".album-back")!;
      target.focus();
      await act(async () => settle({ book_id: 1, deleted: true }));
      expect(close).not.toHaveBeenCalled();
      expect(container.querySelector(".album-title")?.textContent).toBe("Book 3");
      expect(container.textContent).not.toContain("Deleting book");
      expect(container.textContent).not.toContain("Book deleted");
      expect(document.activeElement).toBe(target);
    } finally {
      await act(async () => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });
  it("saves the edited metadata", async () => {
    const model = libraryModel();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />));
      const input = container.querySelector<HTMLInputElement>("#book-title-edit")!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "New title");
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => container.querySelector<HTMLButtonElement>("#library-save-metadata-btn")!.click());
      expect(model.updateSelectedBookMetadata).toHaveBeenCalledWith({ title: "New title", author: "Author", tags: "" });
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("marks the current shelf card as playing only while playback runs", async () => {
    const model = libraryModel();
    const container = document.createElement("div");
    const root = createRoot(container);
    const draw = (playing: boolean) => root.render(<LibraryPanel hidden={false} library={model} albumOpen={false}
      onOpenAlbum={vi.fn()} onCloseAlbum={vi.fn()} playing={playing} playerLoaded currentChapterNumber={1} />);
    try {
      await act(async () => draw(true));
      expect(container.querySelector('.book-card[aria-current="true"] .book-card-playing')).not.toBeNull();
      await act(async () => draw(false));
      expect(container.querySelector(".book-card-playing")).toBeNull();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("shows the shelf as a cover grid and opens a book's album", async () => {
    const model = libraryModel();
    model.items = [selectedSummary, { ...selectedSummary, id: 2, title: "Second", author: null }, { ...selectedSummary, id: 3, title: "Third", completed_chapters: 1, total_chapters: 4 }];
    model.hasMore = true;
    const onOpenAlbum = vi.fn();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} albumOpen={false} onOpenAlbum={onOpenAlbum} onCloseAlbum={vi.fn()} />));
      const cards = [...container.querySelectorAll<HTMLButtonElement>(".book-card")];
      expect(cards.map((card) => card.querySelector(".book-card-title")?.textContent)).toEqual(["Book", "Second", "Third"]);
      expect(cards[2].querySelector(".book-card-partial")?.textContent).toBe("Unfinished · 1/4 chapters");
      expect(cards[2].querySelector(".book-card-meta")?.textContent).toBe("Unfinished · 1/4 chapters · Author");
      // The advice does not fit a card; the shared tooltip carries it.
      expect(cards[2].getAttribute("data-tip")).toBe("Unfinished · 1/4 chapters. Repair audio finishes it.");
      expect(cards[0].hasAttribute("data-tip")).toBe(false);
      // A book without an author shows no placeholder line.
      expect(cards[1].querySelector(".book-card-meta")).toBeNull();
      expect(cards[1].getAttribute("aria-label")).toBe("Second");
      expect(container.querySelector(".album-view")).toBeNull();
      expect(container.querySelector(".library-load-more")?.textContent).toBe("Load more (3 loaded)");
      expect(container.querySelector(".shelf-count")?.textContent).toBe("3+");
      await act(async () => cards[1].click());
      expect(onOpenAlbum).toHaveBeenCalledWith(1);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("offers to clear a search that matches nothing", async () => {
    const model = libraryModel();
    Object.assign(model, { items: [], indexedCount: 4, searchQuery: "zzz", selectedBookDetail: null, selectedBookIndex: -1 });
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} albumOpen={false} onOpenAlbum={vi.fn()} onCloseAlbum={vi.fn()} />));
      expect(container.querySelector(".empty-state-title")?.textContent).toBe("No matches");
      await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === "Clear filters")!.click());
      expect(model.setSearchQuery).toHaveBeenCalledWith("");
      expect(model.setModeFilter).toHaveBeenCalledWith("all");
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("keeps book actions in one row and opens the details form on request", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={libraryModel()} {...ALBUM} />));
      const bar = container.querySelector(".album-manage-bar")!;
      expect([...bar.querySelectorAll("button")].map((button) => button.textContent)).toEqual(["Edit details", "Open folder", "Repair audio", "Delete book"]);
      const toggle = container.querySelector<HTMLButtonElement>("#library-edit-toggle")!;
      expect(toggle.getAttribute("aria-expanded")).toBe("false");
      await act(async () => toggle.click());
      expect(toggle.getAttribute("aria-expanded")).toBe("true");
      expect(container.querySelector("#library-detail-form")?.hasAttribute("hidden")).toBe(false);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("shows placeholder cards while the first page loads", () => {
    const model = libraryModel();
    model.items = [];
    model.loadingList = true;
    const html = renderToString(<LibraryPanel hidden={false} library={model} albumOpen={false} onOpenAlbum={vi.fn()} onCloseAlbum={vi.fn()} />);
    expect(html.match(/book-card is-skeleton/g)).toHaveLength(8);
    expect(html).toContain("Loading library…");
  });

  it("shows the album of a book opened by id from outside the loaded page", async () => {
    const model = libraryModel();
    Object.assign(model, { items: [], selectedBookIndex: -1 });
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />));
      expect(container.querySelector(".album-title")?.textContent).toBe("Book");
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("returns from the album to the shelf", async () => {
    const onCloseAlbum = vi.fn();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={libraryModel()} albumOpen onOpenAlbum={vi.fn()} onCloseAlbum={onCloseAlbum} />));
      expect(container.querySelector(".album-title")?.textContent).toBe("Book");
      expect(container.querySelector(".book-grid")).toBeNull();
      await act(async () => container.querySelector<HTMLButtonElement>(".album-back")!.click());
      expect(onCloseAlbum).toHaveBeenCalledOnce();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("blocks saving a blank title with inline feedback", async () => {
    const model = libraryModel();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />));
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

  it("enables Save changes only after the metadata differs from the stored book", async () => {
    const model = libraryModel();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />));
      const save = container.querySelector<HTMLButtonElement>("#library-save-metadata-btn")!;
      expect(save.disabled).toBe(true);
      const author = container.querySelector<HTMLInputElement>("#book-author-edit")!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(author, "Another author");
        author.dispatchEvent(new Event("input", { bubbles: true }));
      });
      expect(save.disabled).toBe(false);
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(author, "Author");
        author.dispatchEvent(new Event("input", { bubbles: true }));
      });
      expect(save.disabled).toBe(true);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("disables Save changes while the selected book metadata is saving", async () => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const model = libraryModel();
    model.savingMetadata = true;
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />));
      expect(container.querySelector<HTMLButtonElement>("#library-save-metadata-btn")!.disabled).toBe(true);
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
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />));
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
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />));
      await act(async () => (container.querySelector(".chapter-cleaned-btn") as HTMLButtonElement).click());
      expect(container.querySelector('[aria-label="Original text"]')?.textContent).toBe(original);
      expect(container.querySelector('[aria-label="Cleaned text"]')?.textContent).toBe(cleaned);
      await act(async () => container.querySelector<HTMLButtonElement>("[aria-label=\"Close text comparison\"]")!.click());
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
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />));
      expect(container.textContent).toContain("Removed from library, but some files couldn't be deleted");
      await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === "Open folder")!.click());
      expect(model.openPendingDeletionFolder).toHaveBeenCalledOnce();
    } finally {
      await act(async () => root.unmount());
    }
  });
  it.each(["delete", "repair"])("releases loaded audio before the %s request", async (action) => {
    const detail = { ...selectedBook, playback_url: "/api/library/1/audio" };
    const audio = {
      src: "", paused: true, currentTime: 0, duration: 20, readyState: 1,
      volume: 1, muted: false, crossOrigin: null,
      pause: vi.fn(), play: vi.fn(async () => {}), load: vi.fn(),
      removeAttribute: vi.fn(() => { audio.src = ""; }),
    } as unknown as HTMLAudioElement;
    let releasedAtRequest = false;
    const mutation = vi.fn(async () => {
      releasedAtRequest = audio.src === "" && vi.mocked(audio.pause).mock.calls.length > 0 &&
        vi.mocked(audio.removeAttribute).mock.calls.length > 0 && vi.mocked(audio.load).mock.calls.length > 0;
      return { book_id: 1, deleted: true, job_id: "repair" };
    });
    const apiClient = {
      getLibrary: vi.fn(async () => ({ items: [selectedSummary], count: 1 })),
      getLibraryBook: vi.fn(async () => detail),
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
      deleteBook: mutation, repairConversion: mutation,
    } as unknown as ApiClient;
    let player!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      const library = useLibrary({ apiClient });
      player = useAudioPlayer({ apiClient, library });
      return <LibraryPanel hidden={false} library={library} {...ALBUM} onReleaseBookAudio={player.releaseBookAudio} />;
    }
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<Probe />));
      player.audioRef.current = audio;
      await act(async () => player.togglePlay());
      expect(audio.src).toContain("http://desktop.test/book");
      vi.mocked(audio.load).mockClear();
      if (action === "delete") {
        await act(async () => (container.querySelector("#library-delete-btn") as HTMLButtonElement).click());
        await act(async () => (container.querySelector("#library-delete-confirm-btn") as HTMLButtonElement).click());
      } else {
        await act(async () => (container.querySelector("#library-repair-btn") as HTMLButtonElement).click());
      }
      expect(mutation).toHaveBeenCalledOnce();
      expect(releasedAtRequest).toBe(true);
      expect(player.audioProps.src).toBeNull();
    } finally {
      await act(async () => root.unmount());
      window.localStorage.clear();
    }
  });

  it("shows later status messages alongside pending deletion feedback", () => {
    const model = libraryModel();
    model.pendingDeletionPath = "temp/delete_staging/trash";
    model.statusMessage = "Repair started: repair";
    const html = renderToString(<LibraryPanel hidden={false} library={model} {...ALBUM} />);
    expect(html).toContain("Repair started: repair");
  });

  it("allows dismissing pending deletion feedback", async () => {
    const model = libraryModel();
    model.pendingDeletionPath = "temp/delete_staging/trash";
    model.dismissPendingDeletion = vi.fn();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />));
      const dismiss = [...container.querySelectorAll("button")].find((button) => button.textContent === "Dismiss");
      expect(dismiss).toBeDefined();
      await act(async () => dismiss!.click());
      expect(model.dismissPendingDeletion).toHaveBeenCalledOnce();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("associates metadata labels with their editable controls", () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LibraryPanel hidden={false} library={libraryModel()} {...ALBUM} />);
    for (const [id, label] of [["book-title-edit", "Title"], ["book-author-edit", "Author"], ["book-tags-edit", "Tags"]]) {
      expect(container.querySelector(`label[for="${id}"]`)?.textContent).toBe(label);
      expect(container.querySelector(`#${id}`)).not.toBeNull();
    }
    expect(container.querySelector(".album-hero h2")?.textContent).toBe("Book");
    expect(container.querySelector(".album-author")?.textContent).toBe("Author");
    // The kind lives in the one meta line; the source only speaks up when it is missing.
    expect(container.querySelector(".album-meta")?.textContent).toBe("Audiobook · 0 chapters");
    expect(container.querySelector("#book-source-status")).toBeNull();
    expect(container.querySelector("#library-detail-form")?.hasAttribute("hidden")).toBe(true);
  });

  it("falls back to the shelf when the album's book is not selected", () => {
    const model = libraryModel();
    model.selectedBookDetail = null;
    model.selectedBookId = null;
    model.selectedBookIndex = -1;

    const html = renderToString(<LibraryPanel hidden={false} library={model} {...ALBUM} />);

    expect(html).toContain("book-grid");
    // No disabled metadata form or destructive actions before a book is chosen.
    expect(html).not.toContain('id="book-title-edit"');
    expect(html).not.toContain('id="library-delete-btn"');
  });

  it("offers Continue listening for a book that has been started", () => {
    const model = libraryModel();
    model.selectedBookPlayable = true;
    model.selectedBookDetail = { ...selectedBook, playback_position_sec: 600, chapters: [
      { id: 1, number: 1, title: "One", duration_ms: 1_200_000, mp3_path: "a.mp3" },
      { id: 2, number: 2, title: "Two", duration_ms: 1_200_000, mp3_path: "b.mp3" },
    ] };
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LibraryPanel hidden={false} library={model} albumOpen={false} onOpenAlbum={vi.fn()} onCloseAlbum={vi.fn()} onTogglePlay={vi.fn()} />);

    const card = container.querySelector('[aria-label="Continue listening"]');
    expect(card?.querySelector(".continue-title")?.textContent).toBe("Book");
    expect(card?.querySelector(".continue-progress-text")?.textContent).toBe("25% · 30:00 left");
    model.selectedBookDetail = { ...model.selectedBookDetail, playback_position_sec: 0 };
    container.innerHTML = renderToString(<LibraryPanel hidden={false} library={model} albumOpen={false} onOpenAlbum={vi.fn()} onCloseAlbum={vi.fn()} />);
    expect(container.querySelector('[aria-label="Continue listening"]')).toBeNull();
  });

  it("keeps visible details inert while a different selected book is loading", () => {
    const model = libraryModel();
    model.selectedBookId = 2;
    model.loadingDetail = true;
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LibraryPanel hidden={false} library={model} {...ALBUM} />);

    const album = container.querySelector(".album-view");
    expect(album?.getAttribute("aria-busy")).toBe("true");
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
        root.render(<LibraryPanel hidden={false} library={model} {...ALBUM} />);
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

  it.each(["next book", "empty library", "failure"])("keeps meaningful focus while deleting and after %s", async (outcome) => {
    let resolveDelete!: (response: { book_id: number; deleted: boolean }) => void;
    let rejectDelete!: (error: Error) => void;
    const pending = new Promise<{ book_id: number; deleted: boolean }>((resolve, reject) => { resolveDelete = resolve; rejectDelete = reject; });
    const next = { ...selectedSummary, id: 2, title: "Next book" };
    const initialItems = outcome === "next book" ? [selectedSummary, next] : [selectedSummary];
    const apiClient = {
      getLibrary: vi.fn().mockResolvedValueOnce({ items: initialItems, count: initialItems.length })
        .mockResolvedValue({ items: outcome === "next book" ? [next] : [], count: outcome === "next book" ? 1 : 0 }),
      getLibraryBook: vi.fn(async (id: number) => ({ ...selectedBook, id })),
      deleteBook: vi.fn(() => pending),
    } as unknown as ApiClient;
    function Probe() {
      const library = useLibrary({ apiClient });
      const [albumOpen, setAlbumOpen] = useState(true);
      return <LibraryPanel hidden={false} library={library} albumOpen={albumOpen} onOpenAlbum={() => setAlbumOpen(true)} onCloseAlbum={() => setAlbumOpen(false)} onStartConvert={() => {}} />;
    }
    const container = document.createElement("div");
    document.body.append(container);
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const root = createRoot(container);
    try {
      await act(async () => root.render(<Probe />));
      await act(async () => container.querySelector<HTMLButtonElement>("#library-delete-btn")!.click());
      await act(async () => {
        const confirm = container.querySelector<HTMLButtonElement>("#library-delete-confirm-btn")!;
        confirm.focus();
        confirm.click();
      });
      expect(document.activeElement?.getAttribute("role")).toBe("status");
      expect(document.activeElement?.textContent).toContain("Deleting");
      await act(async () => {
        if (outcome === "failure") rejectDelete(new Error("Cannot delete"));
        else resolveDelete({ book_id: 1, deleted: true });
      });
      const target = outcome === "failure" ? "#library-delete-btn"
        : outcome === "next book" ? ".book-card" : ".library-empty button";
      expect(document.activeElement).toBe(container.querySelector(target));
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });

  it.each([
    ["delete", "cancel"],
    ["delete", "escape"],
    ["cleanup", "cancel"],
    ["cleanup", "escape"],
  ])("focuses Cancel and restores the %s trigger after %s", async (action, dismissal) => {
    const model = libraryModel();
    const container = document.createElement("div");
    document.body.append(container);
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const root = createRoot(container);

    try {
      await act(async () => root.render(action === "cleanup"
        ? <LibraryMaintenanceSection library={model} selectedDetailId={1} />
        : <LibraryPanel hidden={false} library={model} {...ALBUM} />));
      await act(async () => {
        const trigger = container.querySelector(`#library-${action}-btn`) as HTMLButtonElement;
        trigger.focus();
        trigger.click();
      });

      const cancel = container.querySelector(`#library-${action}-cancel-btn`) as HTMLButtonElement;
      const confirm = container.querySelector(`#library-${action}-confirm-btn`) as HTMLButtonElement;
      expect(document.activeElement).toBe(cancel);

      await act(async () => {
        if (dismissal === "escape") {
          confirm.focus();
          confirm.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        } else {
          cancel.click();
        }
      });

      expect(container.querySelector(`#library-${action}-confirm-btn`)).toBeNull();
      expect(document.activeElement).toBe(container.querySelector(`#library-${action}-btn`));
      expect(model.deleteSelectedBook).not.toHaveBeenCalled();
      expect(model.cleanupStorage).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });

  it("returns focus to the opened book's card when its album closes before its detail arrived", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const model = libraryModel();
    const second = { ...selectedSummary, id: 2, title: "Second" };
    model.items = [selectedSummary, second];
    model.unfilteredItems = model.items;
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const render = (albumOpen: boolean) => root.render(<LibraryPanel {...ALBUM} albumOpen={albumOpen} hidden={false} library={{ ...model }} />);
    try {
      await act(async () => render(false));
      // Opening the second album selects it at once; its detail is still the first book's.
      model.selectedBookId = 2;
      model.selectedBookIndex = 1;
      await act(async () => render(true));
      model.selectedBookDetail = { ...selectedBook, id: 2, title: "Second" };
      await act(async () => render(true));
      await act(async () => render(false));
      const cards = container.querySelectorAll<HTMLElement>(".book-card");
      expect(document.activeElement).toBe(cards[1]);
    } finally {
      await act(async () => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });

  it("does not read the stored resume position while the book is loaded in the player", () => {
    const model = libraryModel();
    const detail = { ...selectedBook, library_id: "library", library_epoch: "current", playback_url: "/audio",
      chapters: [{ id: 1, number: 1, title: "Chapter", duration_ms: 120_000, mp3_path: "chapter.mp3" }] };
    model.selectedBookDetail = detail;
    model.selectedBookPlayable = true;
    const getItem = vi.spyOn(window.localStorage, "getItem");
    const checkpointReads = () => getItem.mock.calls.filter(([key]) => String(key).startsWith("orpheus.playback.")).length;
    try {
      renderToString(<LibraryPanel {...ALBUM} albumOpen={false} hidden={false} library={model} playerLoaded elapsedSec={12} onTogglePlay={vi.fn()} />);
      expect(checkpointReads()).toBe(0);
      renderToString(<LibraryPanel {...ALBUM} albumOpen={false} hidden={false} library={model} onTogglePlay={vi.fn()} />);
      expect(checkpointReads()).toBeGreaterThan(0);
    } finally {
      getItem.mockRestore();
    }
  });

});
