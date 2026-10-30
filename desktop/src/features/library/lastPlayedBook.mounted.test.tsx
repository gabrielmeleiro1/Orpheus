// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { LibraryBookSummary } from "../../types/api";
import { readLastPlayedBookId, rememberLastPlayedBookId } from "./lastPlayedBook";
import { useLibrary, type UseLibraryResult } from "./useLibrary";

const book = (id: number): LibraryBookSummary => ({
  id, title: `Book ${id}`, author: null, conversion_mode: "audiobook", tags: "", favorite: false,
  source_available: true, total_chapters: 1, completed_chapters: 1,
});

async function firstSelection(items: LibraryBookSummary[]): Promise<number | null> {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const apiClient = {
    getLibrary: vi.fn(async () => ({ items, count: items.length })),
    getLibraryBook: vi.fn(async (id: number) => ({ ...book(id), source_path: null, source_type: "epub", playback_path: null, playback_status: "missing", playback_position_sec: 0, chapters: [] })),
  } as unknown as ApiClient;
  let library: UseLibraryResult | null = null;
  function Probe() {
    library = useLibrary({ apiClient });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  await act(async () => root.render(<Probe />));
  const selected = (library as UseLibraryResult | null)?.selectedBookId ?? null;
  await act(async () => root.unmount());
  return selected;
}

describe("last played book", () => {
  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("round-trips through storage and ignores junk", () => {
    expect(readLastPlayedBookId()).toBeNull();
    rememberLastPlayedBookId(12);
    expect(readLastPlayedBookId()).toBe(12);
    window.localStorage.setItem("orpheus.library.last-played-book.v1", "nope");
    expect(readLastPlayedBookId()).toBeNull();
    rememberLastPlayedBookId(null);
    expect(window.localStorage.getItem("orpheus.library.last-played-book.v1")).toBeNull();
  });

  it("opens the library on the last played book", async () => {
    rememberLastPlayedBookId(3);
    expect(await firstSelection([book(1), book(2), book(3)])).toBe(3);
  });

  it("falls back to the newest book when the last played one is gone", async () => {
    rememberLastPlayedBookId(99);
    expect(await firstSelection([book(1), book(2)])).toBe(1);
  });
});
