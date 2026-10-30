// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../../api/client";
import type { LibraryBookDetail, LibraryBookSummary } from "../../types/api";
import { useLibrary, type UseLibraryResult } from "./useLibrary";

const book: LibraryBookSummary = {
  id: 1,
  title: "Indexed book",
  author: "Author",
  source_path: null,
  conversion_mode: "audiobook",
  tags: "",
  favorite: false,
  source_available: true,
  total_chapters: 1,
  completed_chapters: 1,
};
const detail: LibraryBookDetail = {
  ...book,
  source_path: null,
  source_type: "epub",
  playback_path: null,
  playback_status: "ready",
  playback_position_sec: 0,
  chapters: [],
};
const firstPage = Array.from({ length: 100 }, (_, index) => ({ ...book, id: index + 1 }));
const nextBook = { ...book, id: 101 };
type ReadKind = "list" | "count" | "detail" | "load-more";

describe("library read deadlines", () => {
  let root: Root;
  let container: HTMLDivElement;
  let library: UseLibraryResult;
  let stalledRead: ReadKind | null;
  let stalledSignal: AbortSignal | null;
  let countFailure: boolean;

  function Probe({ apiClient }: { apiClient: ApiClient }) {
    library = useLibrary({ apiClient });
    return null;
  }

  async function mount() {
    const apiClient = createApiClient({ baseUrl: "http://desktop.test" });
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe apiClient={apiClient} />);
    });
  }

  beforeEach(() => {
    vi.useFakeTimers();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    stalledRead = null;
    stalledSignal = null;
    countFailure = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      const kind: ReadKind = url.pathname.endsWith("/count") ? "count"
        : /\/library\/\d+$/.test(url.pathname) ? "detail"
        : url.searchParams.has("offset") ? "load-more" : "list";
      if (kind === stalledRead) {
        const signal = init?.signal;
        stalledSignal = signal ?? null;
        return new Promise<Response>((_resolve, reject) => {
          const abort = () => reject(new DOMException("aborted", "AbortError"));
          if (signal?.aborted) abort();
          else signal?.addEventListener("abort", abort, { once: true });
        });
      }
      if (kind === "count" && countFailure) {
        return new Response("count unavailable", { status: 503 });
      }
      const payload = kind === "count" ? { count: 101 }
        : kind === "detail" ? { ...detail, id: Number(url.pathname.split("/").at(-1)) }
        : { items: kind === "load-more" ? [nextBook] : firstPage, count: 101 };
      return new Response(JSON.stringify(payload));
    });
  });

  afterEach(async () => {
    await act(async () => root?.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it.each<ReadKind>(["list", "count", "detail", "load-more"])(
    "library_reads_time_out_release_loading_and_allow_retry (%s)",
    async (kind) => {
      if (kind !== "load-more") stalledRead = kind;
      await mount();
      if (kind === "load-more") {
        stalledRead = kind;
        await act(async () => { void library.loadMore(); });
      }
      const loading = () => kind === "detail" ? library.loadingDetail
        : kind === "load-more" ? library.loadingMore : library.loadingList;
      expect(loading()).toBe(true);
      expect(stalledSignal).not.toBeNull();
      await act(async () => vi.advanceTimersByTimeAsync(14_999));
      expect(loading()).toBe(true);
      expect(stalledSignal?.aborted).toBe(false);
      await act(async () => vi.advanceTimersByTimeAsync(1));
      expect(loading()).toBe(false);
      expect(library.loadingList).toBe(false);
      expect(library.errorMessage).toBe("request timed out");
      expect(stalledSignal?.aborted).toBe(true);

      stalledRead = null;
      await act(async () => kind === "load-more" ? library.loadMore()
        : kind === "detail" ? library.selectBook(0) : library.refreshLibrary());
      expect(loading()).toBe(false);
      expect(library.errorMessage).toBe("");
      expect(library.items).toHaveLength(kind === "load-more" ? 101 : 100);
      expect(library.selectedBookDetail?.id).toBe(1);
      expect(library.indexedCount).toBe(101);
    },
  );

  it("count_timeout_keeps_the_fetched_page", async () => {
    stalledRead = "count";
    await mount();
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(library.items).toHaveLength(100);
    expect(library.items).toEqual(firstPage);
    expect(library.hasMore).toBe(true);
    expect(library.selectedBookDetail?.id).toBe(1);
    expect(library.loadingList).toBe(false);
    expect(library.errorMessage).toBe("request timed out");
    stalledRead = null;
    await act(async () => library.refreshLibrary());
    expect(library.items).toEqual(firstPage);
    expect(library.indexedCount).toBe(101);
    expect(library.errorMessage).toBe("");

    stalledRead = "count";
    await act(async () => { void library.refreshLibrary(); });
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(library.items).toEqual(firstPage);
    expect(library.indexedCount).toBe(101);
    expect(library.loadingList).toBe(false);
    expect(library.errorMessage).toBe("request timed out");
    stalledRead = null;
    await act(async () => library.refreshLibrary());
    expect(library.errorMessage).toBe("");
  });

  it("keeps the fetched page when the count fails", async () => {
    countFailure = true;
    await mount();
    expect(library.items).toHaveLength(100);
    expect(library.items).toEqual(firstPage);
    expect(library.hasMore).toBe(true);
    expect(library.selectedBookDetail?.id).toBe(1);
    expect(library.loadingList).toBe(false);
    expect(library.errorMessage).toBe("count unavailable");
    countFailure = false;
    await act(async () => library.refreshLibrary());
    expect(library.indexedCount).toBe(101);
    expect(library.errorMessage).toBe("");
  });

  it("times out a selected book read outside the refreshed first page", async () => {
    await mount();
    // Select a later page before refreshing back to the first page.
    await act(async () => library.loadMore());
    await act(async () => library.selectBook(100));
    stalledRead = "detail";
    await act(async () => { void library.refreshLibrary(); });
    expect(library.selectedBookIndex).toBe(-1);
    expect(library.loadingDetail).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(library.loadingDetail).toBe(false);
    expect(library.loadingList).toBe(false);
    expect(library.errorMessage).toBe("request timed out");
    stalledRead = null;
    await act(async () => library.refreshLibrary());
    expect(library.selectedBookDetail?.id).toBe(101);
    expect(library.errorMessage).toBe("");
  });
});
