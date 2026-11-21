// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { LibraryBookSummary } from "../../types/api";
import { useLibrary, type UseLibraryResult } from "./useLibrary";

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

const summary = (id: number): LibraryBookSummary => ({
  id, title: `Book ${id}`, author: null, source_path: null, conversion_mode: "audiobook", tags: "", favorite: false,
  source_available: true, total_chapters: 1, completed_chapters: 1,
});

function pagedClient(total: number) {
  const getLibrary = vi.fn(async (_search?: string, limit = 100, filters: { offset?: number } = {}) => {
    const offset = filters.offset ?? 0;
    const items = Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, index) => summary(offset + index + 1));
    // The list response counts only its own page; the total comes from /count.
    return { items, count: items.length, limit, offset, library_id: "library", library_epoch: "epoch" };
  });
  const apiClient = {
    getLibrary,
    getLibraryCount: vi.fn(async () => ({ count: total })),
    getLibraryBook: vi.fn(async (id: number) => ({ ...summary(id), source_type: "epub", playback_path: null,
      playback_status: "ready", playback_position_sec: 0, chapters: [], library_id: "library", library_epoch: "epoch" })),
  } as unknown as ApiClient;
  return { apiClient, getLibrary };
}

it.each([200, 250])("stops paging an unfiltered library of %i books at its known total", async (total) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const { apiClient, getLibrary } = pagedClient(total);
  let library!: UseLibraryResult;
  function Probe() {
    library = useLibrary({ apiClient });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    expect(library.items).toHaveLength(100);
    expect(library.indexedCount).toBe(total);
    expect(library.hasMore).toBe(true);

    await act(async () => { await library.loadMore(); });
    await act(async () => { await library.loadMore(); });
    expect(library.items).toHaveLength(total);
    expect(library.hasMore).toBe(false);
    // No request for a page past the end of the library.
    const offsets = getLibrary.mock.calls.map((call) => (call[2] as { offset?: number } | undefined)?.offset ?? 0);
    expect(offsets).toEqual(total === 200 ? [0, 100] : [0, 100, 200]);
  } finally {
    await act(async () => root.unmount());
  }
});

it("lists the whole library for the sidebar while the panel is searched", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const { apiClient, getLibrary } = pagedClient(3);
  getLibrary.mockImplementation(async (search?: string, limit = 100) => {
    const items = [1, 2, 3].map(summary).filter((item) => !search || item.title === search);
    return { items, count: items.length, limit, offset: 0, library_id: "library", library_epoch: "epoch" };
  });
  let library!: UseLibraryResult;
  function Probe() {
    library = useLibrary({ apiClient });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => library.setSearchQuery("Book 2"));
    await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
    expect(library.items.map((item) => item.id)).toEqual([2]);
    expect(library.unfilteredItems.map((item) => item.id)).toEqual([1, 2, 3]);
  } finally {
    await act(async () => root.unmount());
  }
});

describe("sidebar shelf while the panel is searched", () => {
  function searchableClient() {
    const { apiClient, getLibrary } = pagedClient(3);
    getLibrary.mockImplementation(async (search?: string, limit = 100) => {
      // A slower whole-library read leaves a window between the two responses.
      if (!search) await new Promise((resolve) => setTimeout(resolve, 20));
      const items = [1, 2, 3].map(summary).filter((item) => !search || item.title.includes(search));
      return { items, count: items.length, limit, offset: 0, library_id: "library", library_epoch: "epoch" };
    });
    const unfilteredCalls = () => getLibrary.mock.calls.filter(([search]) => !search).length;
    return { apiClient, getLibrary, unfilteredCalls };
  }

  async function mountLibrary(apiClient: ApiClient, backendDisconnected = false) {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const shelves: number[][] = [];
    const current: { library: UseLibraryResult; disconnected: boolean } = {
      library: undefined as unknown as UseLibraryResult, disconnected: backendDisconnected,
    };
    function Probe() {
      current.library = useLibrary({ apiClient, backendDisconnected: current.disconnected });
      shelves.push(current.library.unfilteredItems.map((item) => item.id));
      return null;
    }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    const search = async (query: string) => {
      await act(async () => current.library.setSearchQuery(query));
      await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
    };
    const rerender = () => act(async () => root.render(<Probe />));
    return { current, shelves, search, rerender, unmount: () => act(async () => root.unmount()) };
  }

  it("keeps the shelf on screen and fetched once while the search changes", async () => {
    const { apiClient, getLibrary, unfilteredCalls } = searchableClient();
    const mounted = await mountLibrary(apiClient);
    try {
      const loaded = mounted.shelves.length;
      // A whole-library read that never answers must not empty the shelf.
      const base = getLibrary.getMockImplementation()!;
      getLibrary.mockImplementation((search?: string, limit?: number) =>
        search ? base(search, limit) : new Promise(() => {}));
      await mounted.search("Book 2");
      await mounted.search("Book");
      await act(async () => mounted.current.library.setModeFilter("audiobook"));
      await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
      expect(mounted.shelves.slice(loaded).every((shelf) => shelf.length === 3)).toBe(true);
      expect(unfilteredCalls()).toBe(1);
    } finally {
      await mounted.unmount();
    }
  });

  it("re-reads the shelf on refresh and keeps the last one when that read fails", async () => {
    const { apiClient, getLibrary, unfilteredCalls } = searchableClient();
    const mounted = await mountLibrary(apiClient);
    try {
      await mounted.search("Book 2");
      const base = getLibrary.getMockImplementation()!;
      getLibrary.mockImplementation(async (search?: string, limit?: number) => {
        if (!search) throw new Error("timed out");
        return base(search, limit);
      });
      await act(async () => { await mounted.current.library.refreshLibrary(); });
      await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
      expect(unfilteredCalls()).toBe(2);
      expect(mounted.current.library.items.map((item) => item.id)).toEqual([2]);
      expect(mounted.current.library.unfilteredItems.map((item) => item.id)).toEqual([1, 2, 3]);
    } finally {
      await mounted.unmount();
    }
  });

  it("shows a renamed book on the shelf before and during a search", async () => {
    const { apiClient, getLibrary } = searchableClient();
    const titles = new Map([1, 2, 3].map((id) => [id, `Book ${id}`]));
    const book = (id: number) => ({ ...summary(id), title: titles.get(id)! });
    getLibrary.mockImplementation(async (search?: string, limit = 100) => {
      const items = [1, 2, 3].map(book).filter((item) => !search || item.title.includes(search));
      return { items, count: items.length, limit, offset: 0, library_id: "library", library_epoch: "epoch" };
    });
    Object.assign(apiClient, {
      updateLibraryBook: vi.fn(async (id: number, payload: { title: string }) => {
        titles.set(id, payload.title);
        return { ...book(id), source_type: "epub", playback_path: null, playback_status: "ready",
          playback_position_sec: 0, chapters: [], library_id: "library", library_epoch: "epoch" };
      }),
    });
    const mounted = await mountLibrary(apiClient);
    const shelfTitles = () => mounted.current.library.unfilteredItems.map((item) => item.title);
    try {
      expect(mounted.current.library.selectedBookId).toBe(1);
      await act(async () => {
        await mounted.current.library.updateSelectedBookMetadata({ title: "Lighthouse", author: "", tags: "" });
      });
      await mounted.search("Book 2");
      expect(shelfTitles()).toEqual(["Lighthouse", "Book 2", "Book 3"]);

      await act(async () => { await mounted.current.library.selectBook(0); });
      expect(mounted.current.library.selectedBookId).toBe(2);
      await act(async () => {
        await mounted.current.library.updateSelectedBookMetadata({ title: "Book 2 Keeper", author: "", tags: "" });
      });
      expect(shelfTitles()).toEqual(["Lighthouse", "Book 2 Keeper", "Book 3"]);
    } finally {
      await mounted.unmount();
    }
  });

  it("keeps the whole shelf offline when the backend drops during a search", async () => {
    const { apiClient } = searchableClient();
    const mounted = await mountLibrary(apiClient);
    try {
      await mounted.search("Book 2");
      mounted.current.disconnected = true;
      await mounted.rerender();
      expect(mounted.current.library.offline).toBe(true);
      expect(mounted.current.library.items.map((item) => item.id)).toEqual([2]);
      expect(mounted.current.library.unfilteredItems.map((item) => item.id)).toEqual([1, 2, 3]);
    } finally {
      await mounted.unmount();
    }
  });
});

it("selects a book by id when it is not on a loaded page", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const { apiClient } = pagedClient(250);
  let library!: UseLibraryResult;
  function Probe() {
    library = useLibrary({ apiClient });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { await library.selectBookById(150); });
    expect(library.selectedBookId).toBe(150);
    expect(library.selectedBookDetail?.id).toBe(150);
  } finally {
    await act(async () => root.unmount());
  }
});
