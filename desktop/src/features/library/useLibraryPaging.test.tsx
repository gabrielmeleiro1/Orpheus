// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

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
