// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
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

function LibraryProbe({ apiClient, backendDisconnected = false }: { apiClient: ApiClient | null; backendDisconnected?: boolean }): null {
  const library = useLibrary({ apiClient, backendDisconnected });
  (globalThis as typeof globalThis & { libraryProbe?: UseLibraryResult }).libraryProbe = library;
  return null;
}

function getLibraryProbe(): UseLibraryResult {
  return (globalThis as typeof globalThis & { libraryProbe: UseLibraryResult }).libraryProbe;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

describe("useLibrary indexed total", () => {
  let root: Root;
  let container: HTMLDivElement;
  let libraryProbe: UseLibraryResult;

  beforeEach(() => {
    vi.useFakeTimers();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    delete (globalThis as typeof globalThis & { libraryProbe?: UseLibraryResult }).libraryProbe;
    Reflect.deleteProperty(window, "confirm");
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("keeps the indexed total stable when a search has no matches", async () => {
    const getLibrary = vi.fn(async (search?: string) => ({
      items: search ? [] : [book],
      count: search ? 0 : 1,
    }));
    const getLibraryCount = vi.fn(async () => ({ count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    libraryProbe = getLibraryProbe();

    expect(libraryProbe.items).toHaveLength(1);
    expect((libraryProbe as UseLibraryResult & { indexedCount?: number }).indexedCount).toBe(1);

    await act(async () => {
      libraryProbe.setSearchQuery("missing");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    libraryProbe = getLibraryProbe();
    expect(libraryProbe.items).toHaveLength(0);
    expect((libraryProbe as UseLibraryResult & { indexedCount?: number }).indexedCount).toBe(1);
  });

  it("does not repopulate cleared state from an in-flight list after disconnect", async () => {
    const listRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
    let listSignal: AbortSignal | undefined;
    const getLibrary = vi.fn((_search?: string, _limit?: number, _filters?: unknown, options?: { signal?: AbortSignal }) => {
      listSignal = options?.signal;
      return listRequest.promise;
    });
    const apiClient = { getLibrary } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibrary).toHaveBeenCalledTimes(1);

    await act(async () => {
      root.render(<LibraryProbe apiClient={null} backendDisconnected />);
    });
    expect(listSignal?.aborted).toBe(true);
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);

    await act(async () => {
      listRequest.resolve({ items: [book], count: 1 });
      await listRequest.promise;
    });
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);
  });

  it("does not repopulate indexedCount from an in-flight count after disconnect", async () => {
    const countRequest = deferred<{ count: number }>();
    const getLibrary = vi.fn(async (search?: string) => ({
      items: search ? [] : [book],
      count: search ? 0 : 1,
    }));
    let countSignal: AbortSignal | undefined;
    const getLibraryCount = vi.fn((options?: { signal?: AbortSignal }) => {
      countSignal = options?.signal;
      return countRequest.promise;
    });
    const getLibraryBook = vi.fn(async () => detail);
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    await act(async () => {
      getLibraryProbe().setSearchQuery("missing");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryCount).toHaveBeenCalledTimes(1);

    await act(async () => {
      root.render(<LibraryProbe apiClient={null} backendDisconnected />);
    });
    expect(countSignal?.aborted).toBe(true);
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);

    await act(async () => {
      countRequest.resolve({ count: 1 });
      await countRequest.promise;
    });
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);
  });

  it("aborts and fences an in-flight detail request when a search returns no results", async () => {
    const detailRequest = deferred<LibraryBookDetail>();
    let detailSignal: AbortSignal | undefined;
    const getLibrary = vi.fn(async (search?: string) => ({
      items: search ? [] : [book],
      count: search ? 0 : 1,
    }));
    const getLibraryCount = vi.fn(async () => ({ count: 1 }));
    const getLibraryBook = vi.fn((_bookId: number, options?: { signal?: AbortSignal }) => {
      detailSignal = options?.signal;
      return detailRequest.promise;
    });
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().loadingDetail).toBe(true);

    await act(async () => {
      getLibraryProbe().setSearchQuery("missing");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    expect(detailSignal?.aborted).toBe(true);
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().selectedBookId).toBeNull();
    expect(getLibraryProbe().selectedBookDetail).toBeNull();

    await act(async () => {
      detailRequest.resolve(detail);
      await detailRequest.promise;
    });
    expect(getLibraryProbe().selectedBookDetail).toBeNull();
  });

  it("keeps a successful delete when the follow-up refresh fails", async () => {
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockRejectedValueOnce(new Error("refresh failed"));
    const getLibraryCount = vi.fn(async () => ({ count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi.fn(async () => ({ book_id: book.id, deleted: true }));
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let deleted = false;
    await act(async () => {
      deleted = await getLibraryProbe().deleteSelectedBook();
    });

    expect(deleted).toBe(true);
    expect(deleteBook).toHaveBeenCalledWith(book.id, undefined);
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);
  });

  it("resumes the selected status after searching once a delete completes", async () => {
    const book2 = { ...book, id: 2, title: "Second book" };
    const detail2 = { ...detail, id: 2, title: "Second book" };
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book, book2], count: 2 })
      .mockResolvedValueOnce({ items: [book2], count: 1 })
      .mockResolvedValueOnce({ items: [book2], count: 1 });
    const getLibraryCount = vi.fn(async () => ({ count: 1 }));
    const getLibraryBook = vi.fn(async (bookId: number) => (bookId === 2 ? detail2 : detail));
    const deleteBook = vi.fn(async () => ({ book_id: book.id, deleted: true }));
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().statusMessage).toBe("selected: Indexed book");

    await act(async () => {
      await getLibraryProbe().deleteSelectedBook();
    });
    expect(getLibraryProbe().statusMessage).toBe("deleted Indexed book");

    // Searching after the delete must resume the normal selected-status flow
    // instead of staying pinned to the preserved "deleted" message.
    await act(async () => {
      getLibraryProbe().setSearchQuery("Second");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().statusMessage).toBe("selected: Second book");
  });

  it("aborts and fences an in-flight detail request when its book is deleted", async () => {
    const detailRequest = deferred<LibraryBookDetail>();
    const refreshRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
    let detailSignal: AbortSignal | undefined;
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockImplementationOnce(() => refreshRequest.promise);
    const getLibraryBook = vi.fn((_bookId: number, options?: { signal?: AbortSignal }) => {
      detailSignal = options?.signal;
      return detailRequest.promise;
    });
    const deleteBook = vi.fn(async () => ({ book_id: book.id, deleted: true }));
    const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let deletePromise!: Promise<boolean>;
    await act(async () => {
      deletePromise = getLibraryProbe().deleteSelectedBook();
    });
    expect(detailSignal?.aborted).toBe(true);
    expect(getLibraryProbe().selectedBookDetail).toBeNull();

    await act(async () => {
      detailRequest.resolve(detail);
      await detailRequest.promise;
    });
    expect(getLibraryProbe().selectedBookDetail).toBeNull();

    await act(async () => {
      refreshRequest.resolve({ items: [], count: 0 });
      await deletePromise;
    });
  });

  it("rolls back optimistic state when delete fails", async () => {
    const getLibrary = vi.fn().mockResolvedValue({ items: [book], count: 1 });
    const getLibraryCount = vi.fn(async () => ({ count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi.fn().mockRejectedValue(new Error("delete failed"));
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let deleted = true;
    await act(async () => {
      deleted = await getLibraryProbe().deleteSelectedBook();
    });

    expect(deleted).toBe(false);
    expect(getLibraryProbe().items).toEqual([book]);
    expect(getLibraryProbe().indexedCount).toBe(1);
  });

  it("does not refresh or repopulate after delete succeeds following disconnect", async () => {
    const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const getLibrary = vi.fn().mockResolvedValue({ items: [book], count: 1 });
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi.fn(() => deleteRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let deletePromise!: Promise<boolean>;
    await act(async () => {
      deletePromise = getLibraryProbe().deleteSelectedBook();
    });
    await act(async () => {
      root.render(<LibraryProbe apiClient={null} backendDisconnected />);
    });

    await act(async () => {
      deleteRequest.resolve({ book_id: book.id, deleted: true });
      expect(await deletePromise).toBe(true);
    });

    expect(getLibrary).toHaveBeenCalledTimes(1);
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);
  });

  it("does not restore a snapshot after delete fails following disconnect", async () => {
    const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const getLibrary = vi.fn().mockResolvedValue({ items: [book], count: 1 });
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi.fn(() => deleteRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let deletePromise!: Promise<boolean>;
    await act(async () => {
      deletePromise = getLibraryProbe().deleteSelectedBook();
    });
    await act(async () => {
      root.render(<LibraryProbe apiClient={null} backendDisconnected />);
    });

    await act(async () => {
      deleteRequest.reject(new Error("delete failed"));
      expect(await deletePromise).toBe(false);
    });

    expect(getLibrary).toHaveBeenCalledTimes(1);
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);
  });

  it("revalidates the current query after a newer search starts during delete", async () => {
    const newerBook = { ...book, id: 2, title: "Newer result" };
    const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const newerListRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
    let newerListSignal: AbortSignal | undefined;
    let newerListCalls = 0;
    const getLibrary = vi.fn(
      (search?: string, _limit?: number, _filters?: unknown, options?: { signal?: AbortSignal }) => {
        if (search) {
          newerListCalls += 1;
          if (newerListCalls > 1) {
            return Promise.resolve({ items: [newerBook], count: 1 });
          }
          newerListSignal = options?.signal;
          return newerListRequest.promise;
        }
        return Promise.resolve({ items: [book], count: 1 });
      },
    );
    const getLibraryCount = vi.fn(async () => ({ count: 2 }));
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi.fn(() => deleteRequest.promise);
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let deletePromise!: Promise<boolean>;
    await act(async () => {
      deletePromise = getLibraryProbe().deleteSelectedBook();
    });
    await act(async () => {
      getLibraryProbe().setSearchQuery("new");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    expect(getLibrary).toHaveBeenCalledTimes(2);
    expect(newerListSignal?.aborted).toBe(false);

    await act(async () => {
      deleteRequest.resolve({ book_id: book.id, deleted: true });
      expect(await deletePromise).toBe(true);
    });

    expect(getLibrary).toHaveBeenCalledTimes(3);
    expect(newerListSignal?.aborted).toBe(true);
    expect(getLibraryProbe().items).toEqual([newerBook]);

    await act(async () => {
      newerListRequest.resolve({ items: [newerBook], count: 1 });
      await newerListRequest.promise;
    });
    expect(getLibraryProbe().items).toEqual([newerBook]);
  });

  it("revalidates a newer search that captured a pre-delete snapshot", async () => {
    const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const revalidationRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockImplementationOnce(() => revalidationRequest.promise);
    const getLibraryCount = vi
      .fn()
      .mockResolvedValueOnce({ count: 1 })
      .mockResolvedValueOnce({ count: 0 });
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi.fn(() => deleteRequest.promise);
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let deletePromise!: Promise<boolean>;
    await act(async () => {
      deletePromise = getLibraryProbe().deleteSelectedBook();
    });

    await act(async () => {
      getLibraryProbe().setSearchQuery("new");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibrary.mock.calls.map(([query]) => query)).toEqual([undefined, "new"]);
    expect(getLibraryProbe().items).toEqual([book]);

    await act(async () => {
      deleteRequest.resolve({ book_id: book.id, deleted: true });
      await Promise.resolve();
    });
    expect(getLibrary.mock.calls.map(([query]) => query)).toEqual([undefined, "new", "new"]);
    expect(getLibraryProbe().loadingList).toBe(true);

    await act(async () => {
      revalidationRequest.resolve({ items: [], count: 0 });
      expect(await deletePromise).toBe(true);
    });

    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);
    expect(getLibraryProbe().loadingList).toBe(false);
    expect(getLibraryProbe().loadingDetail).toBe(false);
  });

  it("reloads the same query when a refresh returns a pre-delete snapshot", async () => {
    const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const refreshRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockImplementationOnce(() => refreshRequest.promise)
      .mockResolvedValueOnce({ items: [], count: 0 });
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi.fn(() => deleteRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let deletePromise!: Promise<boolean>;
    await act(async () => {
      deletePromise = getLibraryProbe().deleteSelectedBook();
    });

    let refreshPromise!: Promise<void>;
    await act(async () => {
      refreshPromise = getLibraryProbe().refreshLibrary();
    });
    expect(getLibrary).toHaveBeenCalledTimes(2);

    await act(async () => {
      refreshRequest.resolve({ items: [book], count: 1 });
      await refreshPromise;
    });
    expect(getLibraryProbe().items).toEqual([book]);
    expect(getLibraryProbe().indexedCount).toBe(1);

    await act(async () => {
      deleteRequest.resolve({ book_id: book.id, deleted: true });
      expect(await deletePromise).toBe(true);
    });

    expect(getLibrary).toHaveBeenCalledTimes(3);
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);
  });

  it("aborts an invalidated list and clears loading after delete failure", async () => {
    const listRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
    let listSignal: AbortSignal | undefined;
    const getLibrary = vi.fn(
      (search?: string, _limit?: number, _filters?: unknown, options?: { signal?: AbortSignal }) => {
        if (search) {
          listSignal = options?.signal;
          return listRequest.promise;
        }
        return Promise.resolve({ items: [book], count: 1 });
      },
    );
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi.fn().mockRejectedValue(new Error("delete failed"));
    const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;
    Object.defineProperty(window, "confirm", { configurable: true, value: () => true });

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    await act(async () => {
      getLibraryProbe().setSearchQuery("pending");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().loadingList).toBe(true);

    let deleted = true;
    await act(async () => {
      deleted = await getLibraryProbe().deleteSelectedBook();
    });

    expect(deleted).toBe(false);
    expect(listSignal?.aborted).toBe(true);
    expect(getLibraryProbe().loadingList).toBe(false);
    expect(getLibraryProbe().items).toEqual([book]);

    await act(async () => {
      listRequest.resolve({ items: [], count: 0 });
      await listRequest.promise;
    });
  });
});
