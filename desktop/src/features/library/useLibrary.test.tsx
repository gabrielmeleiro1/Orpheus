// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, createApiClient } from "../../api/client";
import type { ApiClient } from "../../api/client";
import type { CleaningPreviewResponse, LibraryBookDetail, LibraryBookSummary } from "../../types/api";
import {
  playbackCheckpointBookIdentity,
  readPlaybackCheckpoint,
  writePlaybackCheckpoint,
} from "../player/playbackCheckpoint";
import { useLibraryPanelState } from "./useLibraryPanelState";
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

const cleaningPreview: CleaningPreviewResponse = {
  book_id: book.id,
  chapter_number: 1,
  title: "Chapter 1",
  available: true,
  before_text: "before",
  llm_input_text: "input",
  cleaned_text: "cleaned",
  trace_rows: [],
};

function LibraryProbe({
  apiClient,
  backendDisconnected = false,
  renderNonce = 0,
}: {
  apiClient: ApiClient | null;
  backendDisconnected?: boolean;
  renderNonce?: number;
}): null {
  void renderNonce;
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

const queryModeChanges: Array<[string, (library: UseLibraryResult) => void]> = [
  ["search query", (library) => library.setSearchQuery("new context")],
  ["mode filter", (library) => library.setModeFilter("document_audio")],
];

describe("useLibrary indexed total", () => {
  let root: Root;
  let container: HTMLDivElement;
  let libraryProbe: UseLibraryResult;

  beforeEach(() => {
    vi.useFakeTimers();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.localStorage.clear();
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    window.localStorage.clear();
    container.remove();
    delete (globalThis as typeof globalThis & { libraryProbe?: UseLibraryResult }).libraryProbe;
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("clears a stalled cleaning preview spinner and allows another preview", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementationOnce((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }))
      .mockImplementation(async () => new Response(JSON.stringify(cleaningPreview)));
    const apiClient = {
      ...createApiClient({ baseUrl: "http://desktop.test" }),
      getLibrary: vi.fn(async () => ({ items: [book], count: 1 })),
      getLibraryBook: vi.fn(async () => detail),
      getLibraryCount: vi.fn(async () => ({ count: 1 })),
    } as ApiClient;
    let panel!: ReturnType<typeof useLibraryPanelState>;
    let library!: UseLibraryResult;
    function PreviewProbe() {
      library = useLibrary({ apiClient });
      panel = useLibraryPanelState({ library });
      return null;
    }
    await act(async () => { root = createRoot(container); root.render(<PreviewProbe />); });
    await act(async () => { void panel.loadCleaningPreview(1); });
    expect(panel.cleaningPreviewLoading).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(30000));
    expect(panel.cleaningPreviewLoading).toBe(false);
    expect(panel.cleaningPreview).toBeNull();
    expect(library.errorMessage).toBe("request timed out");
    await act(async () => panel.loadCleaningPreview(1));
    expect(panel.cleaningPreview).toEqual(cleaningPreview);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retains a valid selected book outside the refreshed first page", async () => {
    const offPage = { ...book, id: 151, title: "Later book" };
    const getLibrary = vi.fn().mockResolvedValueOnce({ items: [book, offPage], count: 2 })
      .mockResolvedValue({ items: [book], count: 2 });
    const getLibraryBook = vi.fn(async (id: number) => ({ ...detail, id, title: id === 151 ? "Later book" : detail.title }));
    const apiClient = { getLibrary, getLibraryBook } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<LibraryProbe apiClient={apiClient} />); });
    await act(async () => { await getLibraryProbe().selectBook(1); });
    getLibraryBook.mockClear();
    await act(async () => getLibraryProbe().refreshLibrary());
    expect(getLibraryProbe().selectedBookId).toBe(151);
    expect(getLibraryProbe().selectedBookDetail?.title).toBe("Later book");
    expect(getLibraryProbe().selectedBookIndex).toBe(-1);
    expect(getLibraryBook).toHaveBeenCalledWith(151, expect.anything());
  });

  it("clears a filtered selection when the selected book no longer exists", async () => {
    const getLibrary = vi.fn().mockResolvedValueOnce({ items: [book], count: 1 })
      .mockResolvedValue({ items: [], count: 0 });
    const getLibraryBook = vi.fn().mockResolvedValue(detail);
    const apiClient = { getLibrary, getLibraryCount: vi.fn(async () => ({ count: 1 })), getLibraryBook } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<LibraryProbe apiClient={apiClient} />); });
    await act(async () => getLibraryProbe().setSearchQuery("other book"));
    await act(async () => vi.advanceTimersByTimeAsync(220));
    expect(getLibraryProbe().selectedBookId).toBe(1);
    getLibraryBook.mockRejectedValue(new ApiError({ message: "Book not found", status: 404, code: "http", detail: "Book not found", retriable: false }));
    await act(async () => getLibraryProbe().refreshLibrary());
    expect(getLibraryProbe().selectedBookId).toBeNull();
    expect(getLibraryProbe().selectedBookDetail).toBeNull();
    expect(getLibraryProbe().selectedBookPlayable).toBe(false);
  });

  it("allows only one metadata save per book until its request settles", async () => {
    const save = deferred<LibraryBookDetail>();
    const updateLibraryBook = vi.fn().mockImplementationOnce(() => save.promise)
      .mockResolvedValue({ ...detail, title: "Second" });
    const apiClient = {
      getLibrary: vi.fn(async () => ({ items: [book], count: 1 })),
      getLibraryBook: vi.fn(async () => detail),
      updateLibraryBook,
    } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<LibraryProbe apiClient={apiClient} />); });
    let first!: Promise<void>;
    await act(async () => {
      first = getLibraryProbe().updateSelectedBookMetadata({ title: "First", author: "Author", tags: "" });
      void getLibraryProbe().updateSelectedBookMetadata({ title: "Second", author: "Author", tags: "" });
    });
    expect(updateLibraryBook).toHaveBeenCalledTimes(1);
    expect(getLibraryProbe().savingMetadata).toBe(true);
    await act(async () => { save.resolve({ ...detail, title: "First" }); await first; });
    expect(getLibraryProbe().savingMetadata).toBe(false);
    await act(async () => getLibraryProbe().updateSelectedBookMetadata({ title: "Second", author: "Author", tags: "" }));
    expect(updateLibraryBook).toHaveBeenCalledTimes(2);
  });

  it.each(["selection", "metadata", "repair", "delete", "dismiss"])("clears pending deletion feedback on %s", async (action) => {
    const second = { ...book, id: 2, title: "Second" };
    const getLibrary = vi.fn().mockResolvedValueOnce({ items: [book, second], count: 2 })
      .mockResolvedValue({ items: [second], count: 1 });
    const apiClient = {
      getLibrary,
      getLibraryBook: vi.fn(async (id: number) => ({ ...detail, id })),
      deleteBook: vi.fn().mockResolvedValueOnce({ book_id: 1, deleted: true, files_pending: true, path: "temp/delete_staging/trash" })
        .mockResolvedValue({ book_id: 2, deleted: true }),
      updateBook: vi.fn(async () => ({ ...detail, id: 2, title: "Updated" })),
      repairConversion: vi.fn(async () => ({ job_id: "repair" })),
    } as unknown as ApiClient;
    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => getLibraryProbe().deleteSelectedBook());
    expect(getLibraryProbe().pendingDeletionPath).toBe("temp/delete_staging/trash");
    await act(async () => {
      const library = getLibraryProbe();
      if (action === "selection") await library.selectBook(0);
      if (action === "metadata") await library.updateSelectedBookMetadata({ title: "Updated", author: "", tags: "" });
      if (action === "repair") await library.repairSelectedBook();
      if (action === "delete") await library.deleteSelectedBook();
      if (action === "dismiss") library.dismissPendingDeletion!();
    });
    expect(getLibraryProbe().pendingDeletionPath).toBeNull();
    // The status names the book, never the job id.
    if (action === "repair") expect(getLibraryProbe().statusMessage).toBe("Repair started for Indexed book.");
    if (action === "dismiss") expect(getLibraryProbe().statusMessage).toBe("");
  });

  it.each([false, true])("keeps selection and detail consistent when filtering a pending selection (has results: %s)", async (hasResults) => {
    const pending = deferred<LibraryBookDetail>();
    const second = { ...book, id: 2, title: "Second" };
    const third = { ...book, id: 3, title: "Third" };
    const apiClient = {
      getLibrary: vi.fn(async (search?: string) => ({ items: search ? (hasResults ? [third] : []) : [book, second], count: 3 })),
      getLibraryCount: vi.fn(async () => ({ count: 3 })),
      getLibraryBook: vi.fn((id: number) => id === 2 ? pending.promise : Promise.resolve({ ...detail, id, playback_url: `/audio/${id}` })),
    } as unknown as ApiClient;
    root = createRoot(container);
    await act(async () => root.render(<LibraryProbe apiClient={apiClient} />));
    await act(async () => { await vi.advanceTimersByTimeAsync(220); });
    expect(getLibraryProbe().selectedBookDetail?.id).toBe(1);
    let selection: Promise<LibraryBookDetail | null>;
    await act(async () => { selection = getLibraryProbe().selectBook(1); });
    expect(getLibraryProbe().selectedBookPlayable).toBe(false);
    await act(async () => getLibraryProbe().setSearchQuery("filtered"));
    await act(async () => { await vi.advanceTimersByTimeAsync(220); });
    expect(getLibraryProbe().selectedBookId).toBe(hasResults ? 3 : null);
    expect(getLibraryProbe().selectedBookDetail?.id ?? null).toBe(hasResults ? 3 : null);
    expect(getLibraryProbe().loadingDetail).toBe(false);
    await act(async () => { pending.resolve({ ...detail, id: 2, playback_url: "/audio/2" }); await selection; });
    expect(getLibraryProbe().selectedBookId).toBe(hasResults ? 3 : null);
    expect(getLibraryProbe().selectedBookDetail?.id ?? null).toBe(hasResults ? 3 : null);
  });

  it("loads cleaning preview without a redundant success status", async () => {
    const apiClient = {
      getLibrary: vi.fn(async () => ({ items: [book], count: 1 })),
      getLibraryCount: vi.fn(async () => ({ count: 1 })),
      getLibraryBook: vi.fn(async () => detail),
      getCleaningPreview: vi.fn(async () => cleaningPreview),
    } as unknown as ApiClient;
    root = createRoot(container);
    await act(async () => root.render(<LibraryProbe apiClient={apiClient} />));
    await act(async () => { await vi.advanceTimersByTimeAsync(220); });
    await act(async () => { expect(await getLibraryProbe().loadCleaningPreview(1)).toEqual(cleaningPreview); });
    expect(getLibraryProbe().statusMessage).toBe("");
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
    expect(libraryProbe.selectedBookId).toBe(book.id);
    expect(libraryProbe.selectedBookDetail).toEqual(detail);
    expect(libraryProbe.selectedBookIndex).toBe(-1);

    await act(async () => {
      libraryProbe.setSearchQuery("");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    libraryProbe = getLibraryProbe();
    expect(libraryProbe.selectedBookIndex).toBe(0);
    expect(getLibraryBook).toHaveBeenCalledTimes(3);
  });

  it("retains pending-file feedback and opens the returned deletion folder", async () => {
    const getLibrary = vi.fn().mockResolvedValueOnce({ items: [book], count: 1 }).mockResolvedValue({ items: [], count: 0 });
    const openDeletionFolder = vi.fn(async () => ({ folder_path: "trash" }));
    const apiClient = {
      getLibrary, getLibraryCount: vi.fn(async () => ({ count: 0 })),
      getLibraryBook: vi.fn(async () => detail), openDeletionFolder,
      deleteBook: vi.fn(async () => ({ book_id: book.id, deleted: true, files_pending: true, path: "trash" })),
    } as unknown as ApiClient;
    root = createRoot(container);
    await act(async () => root.render(<LibraryProbe apiClient={apiClient} />));
    await act(async () => { await getLibraryProbe().deleteSelectedBook(); });
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().pendingDeletionPath).toBe("trash");
    await act(async () => getLibraryProbe().openPendingDeletionFolder!());
    expect(openDeletionFolder).toHaveBeenCalledWith("trash");
  });

  it("clears a stale selection when an unfiltered refresh finds an empty library", async () => {
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockResolvedValueOnce({ items: [], count: 0 });
    const getLibraryBook = vi.fn().mockResolvedValueOnce(detail)
      .mockRejectedValue(new ApiError({ message: "Book not found", status: 404, code: "http", detail: "Book not found", retriable: false }));
    const apiClient = { getLibrary, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().selectedBookId).toBe(book.id);

    await act(async () => {
      await getLibraryProbe().refreshLibrary();
    });

    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().selectedBookId).toBeNull();
    expect(getLibraryProbe().selectedBookDetail).toBeNull();
    expect(getLibraryProbe().selectedBookIndex).toBe(-1);
  });

  it("refreshes same-id detail data after an explicit library refresh", async () => {
    const refreshedDetail: LibraryBookDetail = {
      ...detail,
      title: "Updated indexed book",
      chapters: [{ id: 9, number: 1, title: "New chapter", duration_ms: 1200, mp3_path: null }],
    };
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockResolvedValueOnce({ items: [book], count: 1 });
    const getLibraryBook = vi
      .fn()
      .mockResolvedValueOnce(detail)
      .mockResolvedValueOnce(refreshedDetail);
    const apiClient = { getLibrary, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().selectedBookDetail?.title).toBe(detail.title);

    await act(async () => {
      await getLibraryProbe().refreshLibrary();
    });

    expect(getLibraryBook).toHaveBeenCalledTimes(2);
    expect(getLibraryProbe().selectedBookDetail).toEqual(refreshedDetail);
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
    // An empty library leaves no offline shelf to stand in for the reset list.
    const getLibrary = vi.fn(async () => ({ items: [], count: 0 }));
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

  it("invalidates an in-flight list immediately when the API client identity changes", async () => {
    const oldListRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
    const oldGetLibrary = vi.fn(() => oldListRequest.promise);
    const newGetLibrary = vi.fn(async () => ({ items: [], count: 0 }));
    const oldClient = { getLibrary: oldGetLibrary } as unknown as ApiClient;
    const newClient = { getLibrary: newGetLibrary } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={oldClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(oldGetLibrary).toHaveBeenCalledTimes(1);

    await act(async () => {
      root.render(<LibraryProbe apiClient={newClient} />);
    });
    await act(async () => {
      oldListRequest.resolve({ items: [book], count: 1 });
      await oldListRequest.promise;
    });

    // A new client identity loads immediately rather than waiting out the
    // search debounce, but the superseded client's response must still be
    // discarded rather than rendered.
    expect(newGetLibrary).toHaveBeenCalledTimes(1);
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().selectedBookId).toBeNull();
  });

  it.each(queryModeChanges)(
    "invalidates an in-flight list immediately when the %s changes",
    async (_label, changeContext) => {
      const oldListRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
      const getLibrary = vi.fn(() => oldListRequest.promise);
      const apiClient = { getLibrary } as unknown as ApiClient;

      await act(async () => {
        root = createRoot(container);
        root.render(<LibraryProbe apiClient={apiClient} />);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(220);
      });

      await act(async () => {
        changeContext(getLibraryProbe());
      });
      await act(async () => {
        oldListRequest.resolve({ items: [book], count: 1 });
        await oldListRequest.promise;
      });

      expect(getLibraryProbe().items).toEqual([]);
      expect(getLibraryProbe().selectedBookId).toBeNull();
    },
  );

  it("invalidates an in-flight detail immediately when the API client identity changes", async () => {
    const staleDetail: LibraryBookDetail = { ...detail, title: "Stale book" };
    const freshDetail: LibraryBookDetail = { ...detail, title: "Fresh book" };
    const oldDetailRequest = deferred<LibraryBookDetail>();
    const oldClient = {
      getLibrary: vi.fn(async () => ({ items: [book], count: 1 })),
      getLibraryBook: vi.fn(() => oldDetailRequest.promise),
    } as unknown as ApiClient;
    const newClient = {
      getLibrary: vi.fn(async () => ({ items: [book], count: 1 })),
      getLibraryBook: vi.fn(async () => freshDetail),
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={oldClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().loadingDetail).toBe(true);

    await act(async () => {
      root.render(<LibraryProbe apiClient={newClient} />);
    });
    await act(async () => {
      oldDetailRequest.resolve(staleDetail);
      await oldDetailRequest.promise;
    });

    // The superseded client's detail must never be shown; only the new
    // client's own response may reach the UI.
    expect(getLibraryProbe().selectedBookDetail?.title).not.toBe("Stale book");
    expect(getLibraryProbe().statusMessage).not.toBe("Selected: Stale book");
  });

  it.each(queryModeChanges)(
    "invalidates an in-flight detail immediately when the %s changes",
    async (_label, changeContext) => {
      const oldDetailRequest = deferred<LibraryBookDetail>();
      const getLibrary = vi.fn(async () => ({ items: [book], count: 1 }));
      const getLibraryBook = vi.fn(() => oldDetailRequest.promise);
      const apiClient = { getLibrary, getLibraryBook } as unknown as ApiClient;

      await act(async () => {
        root = createRoot(container);
        root.render(<LibraryProbe apiClient={apiClient} />);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(220);
      });

      await act(async () => {
        changeContext(getLibraryProbe());
      });
      await act(async () => {
        oldDetailRequest.resolve(detail);
        await oldDetailRequest.promise;
      });

      expect(getLibraryProbe().selectedBookDetail).toBeNull();
      expect(getLibraryProbe().statusMessage).not.toBe("Selected: Indexed book");
    },
  );

  it("keeps the previous book detail visible until the next selection loads", async () => {
    const secondBook = { ...book, id: 2, title: "Second book" };
    const secondDetail = { ...detail, id: 2, title: "Second book" };
    const secondDetailRequest = deferred<LibraryBookDetail>();
    const getLibrary = vi.fn(async () => ({ items: [book, secondBook], count: 2 }));
    const getLibraryBook = vi.fn((bookId: number) => (
      bookId === book.id ? Promise.resolve(detail) : secondDetailRequest.promise
    ));
    const apiClient = { getLibrary, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().selectedBookDetail).toEqual(detail);

    let selectPromise!: Promise<LibraryBookDetail | null>;
    await act(async () => {
      selectPromise = getLibraryProbe().selectBook(1);
      await Promise.resolve();
    });

    expect(getLibraryProbe().selectedBookId).toBe(secondBook.id);
    expect(getLibraryProbe().loadingDetail).toBe(true);
    expect(getLibraryProbe().selectedBookDetail).toEqual(detail);

    await act(async () => {
      secondDetailRequest.resolve(secondDetail);
      expect(await selectPromise).toEqual(secondDetail);
    });
    expect(getLibraryProbe().loadingDetail).toBe(false);
    expect(getLibraryProbe().selectedBookDetail).toEqual(secondDetail);
  });

  it("ignores metadata from a prior selection while the next detail is loading", async () => {
    const secondBook = { ...book, id: 2, title: "Second book" };
    const secondDetail = { ...detail, id: 2, title: "Second book" };
    const secondDetailRequest = deferred<LibraryBookDetail>();
    const metadataRequest = deferred<LibraryBookDetail>();
    const getLibrary = vi.fn(async () => ({ items: [book, secondBook], count: 2 }));
    const getLibraryBook = vi.fn((bookId: number) => (
      bookId === book.id ? Promise.resolve(detail) : secondDetailRequest.promise
    ));
    const updateLibraryBook = vi.fn(() => metadataRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, updateLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let metadataPromise!: Promise<void>;
    await act(async () => {
      metadataPromise = getLibraryProbe().updateSelectedBookMetadata({ title: "A changed", author: "Author", tags: "" });
      await Promise.resolve();
    });
    let selectionPromise!: Promise<LibraryBookDetail | null>;
    await act(async () => {
      selectionPromise = getLibraryProbe().selectBook(1);
      await Promise.resolve();
    });
    expect(getLibraryProbe().selectedBookId).toBe(secondBook.id);
    expect(getLibraryProbe().loadingDetail).toBe(true);

    await act(async () => {
      metadataRequest.resolve({ ...detail, title: "A changed" });
      await metadataPromise;
    });
    expect(getLibraryProbe().selectedBookDetail).toEqual(detail);
    expect(getLibraryProbe().loadingDetail).toBe(true);
    expect(getLibraryProbe().statusMessage).not.toBe("Metadata saved");

    await act(async () => {
      secondDetailRequest.resolve(secondDetail);
      await selectionPromise;
    });
    expect(getLibraryProbe().selectedBookDetail).toEqual(secondDetail);
  });

  it("invalidates metadata preview and entity responses when the API client identity changes", async () => {
    const metadataRequest = deferred<LibraryBookDetail>();
    const previewRequest = deferred<CleaningPreviewResponse>();
    const folderRequest = deferred<{ book_id: number; folder_path: string }>();
    const oldClient = {
      getLibrary: vi.fn(async () => ({ items: [book], count: 1 })),
      getLibraryBook: vi.fn(async () => detail),
      updateLibraryBook: vi.fn(() => metadataRequest.promise),
      getCleaningPreview: vi.fn(() => previewRequest.promise),
      openBookFolder: vi.fn(() => folderRequest.promise),
    } as unknown as ApiClient;
    const newClient = {
      getLibrary: vi.fn(async () => ({ items: [book], count: 1 })),
      getLibraryBook: vi.fn(async () => detail),
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={oldClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let metadataPromise!: Promise<void>;
    let previewPromise!: Promise<CleaningPreviewResponse | null>;
    let folderPromise!: Promise<void>;
    await act(async () => {
      metadataPromise = getLibraryProbe().updateSelectedBookMetadata({
        title: "Old client title",
        author: "Author",
        tags: "",
      });
      previewPromise = getLibraryProbe().loadCleaningPreview(1);
      folderPromise = getLibraryProbe().openSelectedBookFolder();
      await Promise.resolve();
    });

    await act(async () => {
      root.render(<LibraryProbe apiClient={newClient} />);
    });
    let previewResult: CleaningPreviewResponse | null = cleaningPreview;
    await act(async () => {
      metadataRequest.resolve({ ...detail, title: "Old client title" });
      previewRequest.resolve(cleaningPreview);
      folderRequest.resolve({ book_id: book.id, folder_path: "/old-client" });
      previewResult = await previewPromise;
      await Promise.all([metadataPromise, folderPromise]);
    });

    expect(previewResult).toBeNull();
    expect(getLibraryProbe().selectedBookDetail?.title).toBe("Indexed book");
    expect(getLibraryProbe().items[0]?.title).toBe("Indexed book");
    expect(getLibraryProbe().statusMessage).not.toContain("old-client");
    expect(getLibraryProbe().statusMessage).not.toBe("Metadata saved");
  });

  it("invalidates metadata preview and open-folder responses when the search query changes", async () => {
    const metadataRequest = deferred<LibraryBookDetail>();
    const previewRequest = deferred<CleaningPreviewResponse>();
    const folderRequest = deferred<{ book_id: number; folder_path: string }>();
    const getLibrary = vi.fn(async () => ({ items: [book], count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const updateLibraryBook = vi.fn(() => metadataRequest.promise);
    const getCleaningPreview = vi.fn(() => previewRequest.promise);
    const openBookFolder = vi.fn(() => folderRequest.promise);
    const apiClient = {
      getLibrary,
      getLibraryBook,
      updateLibraryBook,
      getCleaningPreview,
      openBookFolder,
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let metadataPromise!: Promise<void>;
    let previewPromise!: Promise<CleaningPreviewResponse | null>;
    let folderPromise!: Promise<void>;
    await act(async () => {
      metadataPromise = getLibraryProbe().updateSelectedBookMetadata({
        title: "Stale title",
        author: "Author",
        tags: "",
      });
      previewPromise = getLibraryProbe().loadCleaningPreview(1);
      folderPromise = getLibraryProbe().openSelectedBookFolder();
      getLibraryProbe().setSearchQuery("new context");
      await Promise.resolve();
    });

    let previewResult: CleaningPreviewResponse | null = cleaningPreview;
    await act(async () => {
      metadataRequest.resolve({ ...detail, title: "Stale title" });
      previewRequest.resolve(cleaningPreview);
      folderRequest.resolve({ book_id: book.id, folder_path: "/stale-folder" });
      previewResult = await previewPromise;
      await Promise.all([metadataPromise, folderPromise]);
    });

    expect(previewResult).toBeNull();
    expect(getLibraryProbe().selectedBookDetail?.title).toBe("Indexed book");
    expect(getLibraryProbe().items[0]?.title).toBe("Indexed book");
    expect(getLibraryProbe().statusMessage).not.toContain("stale-folder");
    expect(getLibraryProbe().statusMessage).not.toBe("Metadata saved");
  });

  it("invalidates a repair response when the mode filter changes", async () => {
    const repairRequest = deferred<{ job_id: string }>();
    const getLibrary = vi.fn(async () => ({ items: [book], count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const repairConversion = vi.fn(() => repairRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, repairConversion } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let repairPromise!: Promise<void>;
    await act(async () => {
      repairPromise = getLibraryProbe().repairSelectedBook();
      getLibraryProbe().setModeFilter("document_audio");
      await Promise.resolve();
    });
    await act(async () => {
      repairRequest.resolve({ job_id: "stale-repair" });
      await repairPromise;
    });

    expect(getLibraryProbe().statusMessage).not.toMatch(/Repair started/);
  });

  it("ignores an open-folder success from a previously selected book", async () => {
    const secondBook = { ...book, id: 2, title: "Second book" };
    const secondDetail = { ...detail, id: 2, title: "Second book" };
    const openRequest = deferred<{ book_id: number; folder_path: string }>();
    const getLibrary = vi.fn(async () => ({ items: [book, secondBook], count: 2 }));
    const getLibraryBook = vi.fn(async (bookId: number) => (bookId === book.id ? detail : secondDetail));
    const openBookFolder = vi.fn(() => openRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, openBookFolder } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let openPromise!: Promise<void>;
    await act(async () => {
      openPromise = getLibraryProbe().openSelectedBookFolder();
      await Promise.resolve();
    });
    await act(async () => {
      await getLibraryProbe().selectBook(1);
    });
    expect(getLibraryProbe().statusMessage).toBe("Selected: Second book");

    await act(async () => {
      openRequest.resolve({ book_id: book.id, folder_path: "/old-book" });
      await openPromise;
    });

    expect(getLibraryProbe().statusMessage).toBe("Selected: Second book");
    expect(getLibraryProbe().errorMessage).toBe("");
  });

  it("ignores a repair failure from a previously selected book", async () => {
    const secondBook = { ...book, id: 2, title: "Second book" };
    const secondDetail = { ...detail, id: 2, title: "Second book" };
    const repairRequest = deferred<{ job_id: string }>();
    const getLibrary = vi.fn(async () => ({ items: [book, secondBook], count: 2 }));
    const getLibraryBook = vi.fn(async (bookId: number) => (bookId === book.id ? detail : secondDetail));
    const repairConversion = vi.fn(() => repairRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, repairConversion } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let repairPromise!: Promise<void>;
    await act(async () => {
      repairPromise = getLibraryProbe().repairSelectedBook();
      await Promise.resolve();
    });
    await act(async () => {
      await getLibraryProbe().selectBook(1);
    });

    await act(async () => {
      repairRequest.reject(new Error("old repair failed"));
      await repairPromise;
    });

    expect(getLibraryProbe().statusMessage).toBe("Selected: Second book");
    expect(getLibraryProbe().errorMessage).toBe("");
  });

  it("keeps the newest open-folder result when an older request rejects later", async () => {
    const firstOpen = deferred<{ book_id: number; folder_path: string }>();
    const secondOpen = deferred<{ book_id: number; folder_path: string }>();
    const getLibrary = vi.fn(async () => ({ items: [book], count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const openBookFolder = vi.fn()
      .mockImplementationOnce(() => firstOpen.promise)
      .mockImplementationOnce(() => secondOpen.promise);
    const apiClient = { getLibrary, getLibraryBook, openBookFolder } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let firstPromise!: Promise<void>;
    let secondPromise!: Promise<void>;
    await act(async () => {
      firstPromise = getLibraryProbe().openSelectedBookFolder();
      secondPromise = getLibraryProbe().openSelectedBookFolder();
      await Promise.resolve();
    });
    await act(async () => {
      secondOpen.resolve({ book_id: book.id, folder_path: "/new" });
      await secondPromise;
    });
    expect(getLibraryProbe().statusMessage).toBe("opened folder: /new");

    await act(async () => {
      firstOpen.reject(new Error("old open failed"));
      await firstPromise;
    });
    expect(getLibraryProbe().statusMessage).toBe("opened folder: /new");
    expect(getLibraryProbe().errorMessage).toBe("");
  });

  it("rejects an old preview after disconnect and reconnect select the same book id", async () => {
    const previewRequest = deferred<CleaningPreviewResponse>();
    const getLibrary = vi.fn(async () => ({ items: [book], count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const getCleaningPreview = vi.fn(() => previewRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, getCleaningPreview } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let previewPromise!: Promise<CleaningPreviewResponse | null>;
    await act(async () => {
      previewPromise = getLibraryProbe().loadCleaningPreview(1);
      await Promise.resolve();
    });
    await act(async () => {
      root.render(<LibraryProbe apiClient={apiClient} backendDisconnected />);
    });
    await act(async () => {
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().selectedBookId).toBe(book.id);
    expect(getLibraryProbe().statusMessage).toBe("Selected: Indexed book");

    let previewResult: CleaningPreviewResponse | null = cleaningPreview;
    await act(async () => {
      previewRequest.resolve(cleaningPreview);
      previewResult = await previewPromise;
    });

    expect(previewResult).toBeNull();
    expect(getLibraryProbe().statusMessage).toBe("Selected: Indexed book");
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
    const checkpointIdentity = playbackCheckpointBookIdentity(detail);
    writePlaybackCheckpoint(book.id, 120, checkpointIdentity);

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
    expect(deleteBook).toHaveBeenCalledWith(book.id, {
      signal: expect.any(AbortSignal),
    });
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().indexedCount).toBe(0);
    expect(readPlaybackCheckpoint(book.id, checkpointIdentity)).toBeNull();
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

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().statusMessage).toBe("Selected: Indexed book");

    await act(async () => {
      await getLibraryProbe().deleteSelectedBook();
    });
    expect(getLibraryProbe().statusMessage).toBe("Deleted Indexed book");

    // Searching after the delete must resume the normal selected-status flow
    // instead of staying pinned to the preserved "deleted" message.
    await act(async () => {
      getLibraryProbe().setSearchQuery("Second");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });
    expect(getLibraryProbe().statusMessage).toBe("Selected: Second book");
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

  it("ignores a duplicate delete while the same book delete is in flight", async () => {
    const firstDeleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockResolvedValueOnce({ items: [], count: 0 });
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi
      .fn()
      .mockImplementationOnce(() => firstDeleteRequest.promise)
      .mockResolvedValueOnce({ book_id: book.id, deleted: true });
    const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    const renderedLibrary = getLibraryProbe();
    let firstDelete!: Promise<boolean>;
    let duplicateDelete!: Promise<boolean>;
    await act(async () => {
      firstDelete = renderedLibrary.deleteSelectedBook();
      duplicateDelete = renderedLibrary.deleteSelectedBook();
      await Promise.resolve();
    });

    expect(deleteBook).toHaveBeenCalledTimes(1);

    await act(async () => {
      firstDeleteRequest.reject(new Error("delete failed"));
      expect(await firstDelete).toBe(false);
      expect(await duplicateDelete).toBe(false);
    });

    expect(getLibraryProbe().items).toEqual([book]);

    let retryDeleted = false;
    await act(async () => {
      retryDeleted = await getLibraryProbe().deleteSelectedBook();
    });

    expect(retryDeleted).toBe(true);
    expect(deleteBook).toHaveBeenCalledTimes(2);
    expect(getLibraryProbe().items).toEqual([]);
  });

  it("releases a stale delete owner when the API client is replaced", async () => {
    const deleteRequestA = deferred<{ book_id: number; deleted: boolean }>();
    const getLibraryA = vi.fn(async () => ({ items: [book], count: 1 }));
    const getLibraryBookA = vi.fn(async () => detail);
    const deleteBookA = vi.fn(() => deleteRequestA.promise);
    const apiClientA = { getLibrary: getLibraryA, getLibraryBook: getLibraryBookA, deleteBook: deleteBookA } as unknown as ApiClient;

    const getLibraryB = vi
      .fn()
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockResolvedValueOnce({ items: [], count: 0 });
    const getLibraryBookB = vi.fn(async () => detail);
    const deleteBookB = vi.fn(async () => ({ book_id: book.id, deleted: true }));
    const apiClientB = { getLibrary: getLibraryB, getLibraryBook: getLibraryBookB, deleteBook: deleteBookB } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClientA} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let staleDelete!: Promise<boolean>;
    await act(async () => {
      staleDelete = getLibraryProbe().deleteSelectedBook();
      await Promise.resolve();
    });
    expect(deleteBookA).toHaveBeenCalledTimes(1);

    await act(async () => {
      root.render(<LibraryProbe apiClient={apiClientB} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    expect(getLibraryProbe().selectedBookId).toBe(book.id);
    let replacementDeleted = false;
    await act(async () => {
      replacementDeleted = await getLibraryProbe().deleteSelectedBook();
    });
    expect(replacementDeleted).toBe(true);
    expect(deleteBookB).toHaveBeenCalledTimes(1);
    expect(getLibraryProbe().items).toEqual([]);

    await act(async () => {
      deleteRequestA.resolve({ book_id: book.id, deleted: true });
      expect(await staleDelete).toBe(true);
    });
    expect(getLibraryProbe().items).toEqual([]);
    expect(getLibraryProbe().selectedBookId).toBeNull();
  });

  it("reconciles a failed delete without clobbering a newer user selection", async () => {
    const secondBook = { ...book, id: 2, title: "Second book" };
    const secondDetail = { ...detail, id: 2, title: "Second book" };
    const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const getLibrary = vi.fn(async () => ({ items: [book, secondBook], count: 2 }));
    const getLibraryBook = vi.fn(async (bookId: number) => (bookId === secondBook.id ? secondDetail : detail));
    const deleteBook = vi.fn(() => deleteRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;

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
      await Promise.resolve();
    });
    expect(getLibraryProbe().items.map((item) => item.id)).toEqual([2]);

    await act(async () => {
      await getLibraryProbe().selectBook(0);
    });
    expect(getLibraryProbe().selectedBookId).toBe(secondBook.id);
    expect(getLibraryProbe().statusMessage).toBe("Selected: Second book");

    let deleted = true;
    await act(async () => {
      deleteRequest.reject(new Error("delete failed"));
      deleted = await deletePromise;
    });

    expect(deleted).toBe(false);
    expect(getLibraryProbe().items.map((item) => item.id)).toEqual([1, 2]);
    expect(getLibraryProbe().selectedBookId).toBe(secondBook.id);
    expect(getLibraryProbe().selectedBookDetail).toEqual(secondDetail);
    expect(getLibraryProbe().statusMessage).toBe("Selected: Second book");
    expect(getLibraryProbe().errorMessage).toBe("");
  });

  it("preserves a newer selected-book error while reconciling a failed delete", async () => {
    const secondBook = { ...book, id: 2, title: "Second book" };
    const thirdBook = { ...book, id: 3, title: "Third book" };
    const secondDetail = { ...detail, id: 2, title: "Second book" };
    const thirdDetail = { ...detail, id: 3, title: "Third book" };
    const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const getLibrary = vi.fn(async () => ({ items: [book, secondBook, thirdBook], count: 3 }));
    const getLibraryBook = vi.fn(async (bookId: number) => {
      if (bookId === secondBook.id) {
        return secondDetail;
      }
      if (bookId === thirdBook.id) {
        return thirdDetail;
      }
      return detail;
    });
    const deleteBook = vi.fn(() => deleteRequest.promise);
    const repairConversion = vi.fn().mockRejectedValue(new Error("newer repair failed"));
    const apiClient = { getLibrary, getLibraryBook, deleteBook, repairConversion } as unknown as ApiClient;

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
      await Promise.resolve();
    });
    await act(async () => {
      await getLibraryProbe().selectBook(1);
    });
    await act(async () => {
      await getLibraryProbe().repairSelectedBook();
    });
    expect(getLibraryProbe().errorMessage).toBe("newer repair failed");

    await act(async () => {
      deleteRequest.reject(new Error("delete failed"));
      await deletePromise;
    });

    expect(getLibraryProbe().items.map((item) => item.id)).toEqual([1, 2, 3]);
    expect(getLibraryProbe().selectedBookId).toBe(thirdBook.id);
    expect(getLibraryProbe().statusMessage).toBe("newer repair failed");
    expect(getLibraryProbe().errorMessage).toBe("newer repair failed");
  });

  it("does not publish a deleted status over a newer selection after delete succeeds", async () => {
    const secondBook = { ...book, id: 2, title: "Second book" };
    const secondDetail = { ...detail, id: 2, title: "Second book" };
    const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book, secondBook], count: 2 })
      .mockResolvedValueOnce({ items: [secondBook], count: 1 });
    const getLibraryBook = vi.fn(async (bookId: number) => (bookId === secondBook.id ? secondDetail : detail));
    const deleteBook = vi.fn(() => deleteRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;

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
      await Promise.resolve();
    });
    await act(async () => {
      await getLibraryProbe().selectBook(0);
    });
    expect(getLibraryProbe().statusMessage).toBe("Selected: Second book");

    await act(async () => {
      deleteRequest.resolve({ book_id: book.id, deleted: true });
      await deletePromise;
    });

    expect(getLibraryProbe().items).toEqual([secondBook]);
    expect(getLibraryProbe().selectedBookId).toBe(secondBook.id);
    expect(getLibraryProbe().statusMessage).toBe("Selected: Second book");
    expect(getLibraryProbe().errorMessage).toBe("");
  });

  it.each(queryModeChanges)(
    "does not publish or reconcile a stale delete when the %s changes",
    async (_label, changeContext) => {
      const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
      const getLibrary = vi
        .fn()
        .mockResolvedValueOnce({ items: [book], count: 1 })
        .mockResolvedValueOnce({ items: [], count: 0 });
      const getLibraryBook = vi.fn(async () => detail);
      const deleteBook = vi.fn(() => deleteRequest.promise);
      const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;

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
        changeContext(getLibraryProbe());
        await Promise.resolve();
      });
      await act(async () => {
        deleteRequest.resolve({ book_id: book.id, deleted: true });
        await deletePromise;
      });

      // The initial list, the filtered list and the sidebar's unfiltered page.
      expect(getLibrary).toHaveBeenCalledTimes(3);
      expect(getLibraryProbe().items).toEqual([]);
      expect(getLibraryProbe().statusMessage).not.toBe("Deleted Indexed book");
      expect(getLibraryProbe().statusMessage).not.toBe("Deleting Indexed book…");
    },
  );

  it("does not refresh or repopulate after delete succeeds following disconnect", async () => {
    const deleteRequest = deferred<{ book_id: number; deleted: boolean }>();
    const getLibrary = vi.fn().mockResolvedValue({ items: [book], count: 1 });
    const getLibraryBook = vi.fn(async () => detail);
    const deleteBook = vi.fn(() => deleteRequest.promise);
    const apiClient = { getLibrary, getLibraryBook, deleteBook } as unknown as ApiClient;

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

  it("invalidates cleanup continuation when the backend disconnects", async () => {
    const cleanupRequest = deferred<{ deleted_files: number; deleted_rows: number }>();
    const getLibrary = vi.fn(async () => ({ items: [book], count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const getMaintenanceDiagnostics = vi.fn(async () => ({
      database_path: "db",
      output_path: "out",
      temp_path: "tmp",
      app_data_path: "app",
      audit: {},
    }));
    const cleanupStorage = vi.fn(() => cleanupRequest.promise);
    const apiClient = {
      getLibrary,
      getLibraryBook,
      getMaintenanceDiagnostics,
      cleanupStorage,
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let cleanupPromise!: Promise<void>;
    await act(async () => {
      cleanupPromise = getLibraryProbe().cleanupStorage();
      await Promise.resolve();
    });
    await act(async () => {
      root.render(<LibraryProbe apiClient={apiClient} backendDisconnected />);
    });
    await act(async () => {
      cleanupRequest.resolve({ deleted_files: 0, deleted_rows: 0 });
      await cleanupPromise;
    });

    expect(getMaintenanceDiagnostics).not.toHaveBeenCalled();
    expect(getLibrary).toHaveBeenCalledTimes(1);
  });

  it("invalidates cleanup continuation when the current search changes", async () => {
    const cleanupRequest = deferred<{ deleted_files: number; deleted_rows: number }>();
    const getLibrary = vi.fn(async () => ({ items: [book], count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const getMaintenanceDiagnostics = vi.fn(async () => ({
      database_path: "db",
      output_path: "out",
      temp_path: "tmp",
      app_data_path: "app",
      audit: {},
    }));
    const cleanupStorage = vi.fn(() => cleanupRequest.promise);
    const apiClient = {
      getLibrary,
      getLibraryBook,
      getMaintenanceDiagnostics,
      cleanupStorage,
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let cleanupPromise!: Promise<void>;
    await act(async () => {
      cleanupPromise = getLibraryProbe().cleanupStorage();
      getLibraryProbe().setSearchQuery("new context");
      await Promise.resolve();
    });
    await act(async () => {
      cleanupRequest.resolve({ deleted_files: 0, deleted_rows: 0 });
      await cleanupPromise;
    });

    expect(getMaintenanceDiagnostics).not.toHaveBeenCalled();
    expect(getLibrary).toHaveBeenCalledTimes(1);
  });

  it("invalidates cleanup continuation when a newer list refresh starts", async () => {
    const cleanupRequest = deferred<{ deleted_files: number; deleted_rows: number }>();
    const refreshRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: [book], count: 1 })
      .mockImplementationOnce(() => refreshRequest.promise)
      .mockResolvedValue({ items: [book], count: 1 });
    const getLibraryBook = vi.fn(async () => detail);
    const getMaintenanceDiagnostics = vi.fn(async () => ({
      database_path: "db",
      output_path: "out",
      temp_path: "tmp",
      app_data_path: "app",
      audit: {},
    }));
    const cleanupStorage = vi.fn(() => cleanupRequest.promise);
    const apiClient = {
      getLibrary,
      getLibraryBook,
      getMaintenanceDiagnostics,
      cleanupStorage,
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(220);
    });

    let cleanupPromise!: Promise<void>;
    let refreshPromise!: Promise<void>;
    await act(async () => {
      cleanupPromise = getLibraryProbe().cleanupStorage();
      refreshPromise = getLibraryProbe().refreshLibrary();
      await Promise.resolve();
    });
    await act(async () => {
      cleanupRequest.resolve({ deleted_files: 0, deleted_rows: 0 });
      await cleanupPromise;
    });

    expect(getMaintenanceDiagnostics).not.toHaveBeenCalled();
    expect(getLibrary).toHaveBeenCalledTimes(2);

    await act(async () => {
      refreshRequest.resolve({ items: [book], count: 1 });
      await refreshPromise;
    });
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

    // The revalidated search, then the sidebar's unfiltered page.
    expect(getLibrary).toHaveBeenCalledTimes(4);
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
    // The sidebar keeps the unfiltered page it already has.
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

  it("starts the first library page immediately without the search debounce", async () => {
    const getLibrary = vi.fn(async () => ({ items: [book], count: 1 }));
    const getLibraryBook = vi.fn(async () => detail);
    const apiClient = { getLibrary, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
      await Promise.resolve();
    });

    expect(getLibrary).toHaveBeenCalledTimes(1);
  });

  it("keeps the facade identity stable across unrelated parent renders", async () => {
    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={null} backendDisconnected renderNonce={0} />);
    });
    const first = getLibraryProbe();

    await act(async () => {
      root.render(<LibraryProbe apiClient={null} backendDisconnected renderNonce={1} />);
    });

    expect(getLibraryProbe()).toBe(first);
  });

  it.each(["success", "error"])("keeps pagination occupied when an obsolete page settles with %s", async (outcome) => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({ ...book, id: index + 1 }));
    const oldPage = deferred<{ items: LibraryBookSummary[]; count: number }>();
    const newPage = deferred<{ items: LibraryBookSummary[]; count: number }>();
    const getLibrary = vi.fn()
      .mockResolvedValueOnce({ items: firstPage, count: 100 })
      .mockReturnValueOnce(oldPage.promise)
      .mockResolvedValueOnce({ items: firstPage, count: 100 })
      .mockReturnValue(newPage.promise);
    const apiClient = { getLibrary, getLibraryCount: vi.fn(async () => ({ count: 101 })),
      getLibraryBook: vi.fn(async () => detail) } as unknown as ApiClient;
    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
    });
    let oldLoad!: Promise<void>;
    let newLoad!: Promise<void>;
    await act(async () => { oldLoad = getLibraryProbe().loadMore(); });
    await act(async () => { await getLibraryProbe().refreshLibrary(); });
    await act(async () => { newLoad = getLibraryProbe().loadMore(); });
    expect(getLibrary).toHaveBeenCalledTimes(4);
    await act(async () => {
      if (outcome === "success") oldPage.resolve({ items: [{ ...book, id: 999 }], count: 1 });
      else oldPage.reject(new Error("old page failed"));
      await oldLoad;
    });
    expect(getLibraryProbe().loadingMore).toBe(true);
    let duplicateLoad!: Promise<void>;
    await act(async () => { duplicateLoad = getLibraryProbe().loadMore(); });
    expect(getLibrary).toHaveBeenCalledTimes(4);
    await act(async () => {
      newPage.resolve({ items: [firstPage[99], { ...book, id: 101 }], count: 2 });
      await Promise.all([newLoad, duplicateLoad]);
    });
    expect(getLibraryProbe().loadingMore).toBe(false);
    expect(getLibraryProbe().items.map((item) => item.id)).toEqual([...firstPage.map((item) => item.id), 101]);
  });

  it("appends one load-more page without duplicate rows or duplicate requests", async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      ...book,
      id: index + 1,
      title: `Book ${index + 1}`,
    }));
    const moreRequest = deferred<{ items: LibraryBookSummary[]; count: number }>();
    const getLibrary = vi.fn(
      (_search?: string, _limit?: number, filters?: { offset?: number }) => (
        filters?.offset === 100
          ? moreRequest.promise
          : Promise.resolve({ items: firstPage, count: firstPage.length })
      ),
    );
    const getLibraryCount = vi.fn(async () => ({ count: 101 }));
    const getLibraryBook = vi.fn(async (bookId: number) => ({ ...detail, id: bookId }));
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
      await Promise.resolve();
    });

    let firstLoad!: Promise<void>;
    let duplicateLoad!: Promise<void>;
    await act(async () => {
      firstLoad = getLibraryProbe().loadMore();
      duplicateLoad = getLibraryProbe().loadMore();
    });
    expect(getLibrary).toHaveBeenCalledTimes(2);

    await act(async () => {
      moreRequest.resolve({ items: [firstPage[99], { ...book, id: 101, title: "Book 101" }], count: 2 });
      await Promise.all([firstLoad, duplicateLoad]);
    });

    expect(getLibraryProbe().items).toHaveLength(101);
    expect(new Set(getLibraryProbe().items.map((item) => item.id)).size).toBe(101);
    expect(getLibraryProbe().hasMore).toBe(false);
  });

  it("loads and selects the first book on the next page at the pager boundary", async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      ...book,
      id: index + 1,
      title: `Book ${index + 1}`,
    }));
    const nextBook = { ...book, id: 101, title: "Book 101" };
    const getLibrary = vi.fn(
      (_search?: string, _limit?: number, filters?: { offset?: number }) => Promise.resolve(
        filters?.offset === 100
          ? { items: [nextBook], count: 1 }
          : { items: firstPage, count: firstPage.length },
      ),
    );
    const getLibraryCount = vi.fn(async () => ({ count: 101 }));
    const getLibraryBook = vi.fn(async (bookId: number) => ({
      ...detail,
      id: bookId,
      title: `Book ${bookId}`,
    }));
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
      await Promise.resolve();
    });
    await act(async () => {
      await getLibraryProbe().selectBook(99);
    });
    expect(getLibraryProbe().selectedBookIndex).toBe(99);

    await act(async () => {
      await getLibraryProbe().selectRelative(1);
    });

    expect(getLibraryProbe().items).toHaveLength(101);
    expect(getLibraryProbe().selectedBookIndex).toBe(100);
    expect(getLibraryProbe().selectedBookId).toBe(101);
    expect(getLibraryProbe().selectedBookDetail?.id).toBe(101);
  });

  it.each([0, 99])("preserves a newer selection at row %s while next-page navigation is pending", async (newIndex) => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({ ...book, id: index + 1 }));
    const nextPage = deferred<{ items: LibraryBookSummary[]; count: number }>();
    const apiClient = {
      getLibrary: vi.fn((_search?: string, _limit?: number, filters?: { offset?: number }) =>
        filters?.offset === 100 ? nextPage.promise : Promise.resolve({ items: firstPage, count: 100 })),
      getLibraryCount: vi.fn(async () => ({ count: 101 })),
      getLibraryBook: vi.fn(async (id: number) => ({ ...detail, id })),
    } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<LibraryProbe apiClient={apiClient} />); });
    await act(async () => { await getLibraryProbe().selectBook(99); });
    let navigation!: Promise<LibraryBookDetail | null>;
    await act(async () => { navigation = getLibraryProbe().selectRelative(1); });
    await act(async () => { await getLibraryProbe().selectBook(newIndex); });
    await act(async () => {
      nextPage.resolve({ items: [{ ...book, id: 101 }], count: 1 });
      expect(await navigation).toBeNull();
    });
    expect(getLibraryProbe().items).toHaveLength(101);
    expect(getLibraryProbe().selectedBookIndex).toBe(newIndex);
    expect(getLibraryProbe().selectedBookId).toBe(newIndex + 1);
    expect(getLibraryProbe().selectedBookDetail?.id).toBe(newIndex + 1);
  });

  it("invalidates an in-flight load-more page as soon as the query changes", async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      ...book,
      id: index + 1,
      title: `Book ${index + 1}`,
    }));
    const stalePage = deferred<{ items: LibraryBookSummary[]; count: number }>();
    let staleSignal: AbortSignal | undefined;
    const getLibrary = vi.fn(
      (
        search?: string,
        _limit?: number,
        filters?: { offset?: number },
        options?: { signal?: AbortSignal },
      ) => {
        if (filters?.offset === 100) {
          staleSignal = options?.signal;
          return stalePage.promise;
        }
        if (search === "new") {
          return Promise.resolve({ items: [{ ...book, id: 999, title: "New result" }], count: 1 });
        }
        return Promise.resolve({ items: firstPage, count: firstPage.length });
      },
    );
    // The library holds a second page, which is still in flight when the query changes.
    const getLibraryCount = vi.fn(async () => ({ count: 101 }));
    const getLibraryBook = vi.fn(async (bookId: number) => ({ ...detail, id: bookId }));
    const apiClient = { getLibrary, getLibraryCount, getLibraryBook } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<LibraryProbe apiClient={apiClient} />);
      await Promise.resolve();
    });

    let loadMorePromise!: Promise<void>;
    await act(async () => {
      loadMorePromise = getLibraryProbe().loadMore();
    });
    await act(async () => {
      getLibraryProbe().setSearchQuery("new");
    });
    expect(staleSignal?.aborted).toBe(true);

    await act(async () => {
      stalePage.resolve({ items: [{ ...book, id: 101 }], count: 1 });
      await loadMorePromise;
      await vi.advanceTimersByTimeAsync(220);
    });

    expect(getLibraryProbe().items.map((item) => item.id)).toEqual([999]);
  });
});
