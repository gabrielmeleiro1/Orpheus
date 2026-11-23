import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { LibrarySession } from "./librarySession";
import { ApiError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import { useGenerationGuard } from "../../state/generationGuard";
import { useLatestRequest } from "../conversion/useLatestRequest";
import type { CleaningPreviewResponse, LibraryBookDetail, LibraryBookSummary, LibraryIdentity } from "../../types/api";
import { readLastPlayedBookId } from "./lastPlayedBook";
import { createLibraryRepository, type LibraryModeFilter } from "./libraryRepository";

/** Merge a freshly fetched page onto the accumulated list, dropping any ids we
 * already hold so overlapping/duplicate pages never render the same book twice. */
function appendUniqueBooks(
  previous: LibraryBookSummary[],
  incoming: LibraryBookSummary[],
): LibraryBookSummary[] {
  if (incoming.length === 0) {
    return previous;
  }
  const seen = new Set(previous.map((item) => item.id));
  const merged = previous.slice();
  for (const item of incoming) {
    if (!seen.has(item.id)) {
      seen.add(item.id);
      merged.push(item);
    }
  }
  return merged.length === previous.length ? previous : merged;
}

// Preview reads load chapter text and cleaning traces from disk; a stalled
// read must return to the panel so its spinner clears and selection can retry.
const CLEANING_PREVIEW_REQUEST_TIMEOUT_MS = 30_000;

// Library reads must release their loading state even while the backend stays connected.
const LIBRARY_READ_TIMEOUT_MS = 15_000;

const SEARCH_DEBOUNCE_MS = 220;

interface UseLibraryQueriesArgs {
  apiClient: ApiClient | null;
  backendDisconnected: boolean;
  session: LibrarySession;
}

export function useLibraryQueries({ apiClient, backendDisconnected, session }: UseLibraryQueriesArgs) {
  const repository = useMemo(() => (apiClient ? createLibraryRepository(apiClient) : null), [apiClient]);

  const libraryIdentityRef = useRef<string | null>(null);
  const reloadSessionRef = useRef<() => Promise<void>>(async () => {});
  const acceptIdentity = useCallback((identity: LibraryIdentity): boolean => {
    if (!identity.library_id || !identity.library_epoch) return true;
    const next = JSON.stringify([identity.library_id, identity.library_epoch]);
    if (libraryIdentityRef.current !== null && libraryIdentityRef.current !== next) {
      void reloadSessionRef.current();
      return false;
    }
    libraryIdentityRef.current = next;
    return true;
  }, []);

  const [searchQuery, setSearchQuery] = useState("");
  const [modeFilter, setModeFilter] = useState<LibraryModeFilter>("all");
  const [items, setItems] = useState<LibraryBookSummary[]>([]);
  // The unfiltered first page shown while a search or mode filter narrows
  // `items`; null means `items` itself is unfiltered.
  const [unfilteredPage, setUnfilteredPage] = useState<LibraryBookSummary[] | null>(null);
  // The last unfiltered first page read, and whether the library may have
  // changed since. A filtered load re-reads it only when it is stale.
  const shelfRef = useRef<{ items: LibraryBookSummary[]; stale: boolean } | null>(null);
  const [indexedCount, setIndexedCount] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [selectedBookId, setSelectedBookId] = useState<number | null>(null);
  const [selectedBookIndex, setSelectedBookIndex] = useState(-1);
  const [selectedBookDetail, setSelectedBookDetail] = useState<LibraryBookDetail | null>(null);

  const [loadingList, setLoadingList] = useState(false);
  // Counts list loads that ran to completion, successful or not.
  const [completedListLoads, setCompletedListLoads] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [statusMessage, setStatusMessage] = useState("Library idle");
  const [pendingDeletionPath, setPendingDeletionPath] = useState<string | null>(null);
  useEffect(() => setPendingDeletionPath(null), [apiClient]);
  const dismissPendingDeletion = useCallback(() => {
    setPendingDeletionPath(null);
    setStatusMessage((current) => current === "Removed from library, but some files couldn't be deleted" ? "" : current);
  }, []);

  const requestContextGenerationRef = useGenerationGuard();
  const queryContextGenerationRef = useGenerationGuard();
  const reloadContextGenerationRef = useGenerationGuard();
  const requestContextIdentityRef = useRef({ apiClient, backendDisconnected });
  const reloadContextIdentityRef = useRef({ query: searchQuery.trim(), modeFilter });
  if (
    requestContextIdentityRef.current.apiClient !== apiClient ||
    requestContextIdentityRef.current.backendDisconnected !== backendDisconnected
  ) {
    requestContextIdentityRef.current = { apiClient, backendDisconnected };
    requestContextGenerationRef.invalidate();
    reloadContextGenerationRef.invalidate();
  }
  if (
    reloadContextIdentityRef.current.query !== searchQuery.trim() ||
    reloadContextIdentityRef.current.modeFilter !== modeFilter
  ) {
    reloadContextIdentityRef.current = { query: searchQuery.trim(), modeFilter };
    queryContextGenerationRef.invalidate();
    reloadContextGenerationRef.invalidate();
  }

  const beginListRequest = useLatestRequest();
  const beginDetailRequest = useLatestRequest();
  const beginMutationRequest = useLatestRequest();
  const beginPreviewRequest = useLatestRequest();
  const beginEntityActionRequest = useLatestRequest();
  const beginApiOwnedRequest = useCallback((beginRequest: () => () => boolean) => {
    const libraryGeneration = session.generation;
    const ownsRequestContext = requestContextGenerationRef.capture();
    const isLatest = beginRequest();
    return () =>
      isLatest() &&
      ownsRequestContext() &&
      session.generation === libraryGeneration;
  }, [session]);
  const beginOwnedRequest = useCallback((beginRequest: () => () => boolean) => {
    const ownsQueryContext = queryContextGenerationRef.capture();
    const isApiCurrent = beginApiOwnedRequest(beginRequest);
    return () => isApiCurrent() && ownsQueryContext();
  }, [beginApiOwnedRequest]);
  const getReloadContextGeneration = useCallback(
    () => reloadContextGenerationRef.current,
    [],
  );
  const listAbortRef = useRef<AbortController | null>(null);
  // Load-more shares the list generation fence (so a new search invalidates an
  // in-flight page) but keeps its own abort controller and a re-entrancy guard
  // so rapid clicks never issue duplicate page requests.
  const loadMoreAbortRef = useRef<AbortController | null>(null);
  const loadMoreInFlightRef = useRef(false);
  const loadMoreRequestRef = useRef<Promise<LibraryBookSummary[]> | null>(null);
  const listFenceRef = useRef<() => boolean>(() => false);
  const nextOffsetRef = useRef(0);
  const hasMoreRef = useRef(false);
  // Total library size from /count for the loaded unfiltered list, else null.
  const unfilteredTotalRef = useRef<number | null>(null);
  const loadedRepositoryRef = useRef<typeof repository>(null);
  const detailAbortRef = useRef<AbortController | null>(null);
  const selectedBookIdRef = useRef<number | null>(null);
  const selectedBookDetailRef = useRef<LibraryBookDetail | null>(null);
  useLayoutEffect(() => {
    selectedBookDetailRef.current = selectedBookDetail;
  });
  const inFlightDeleteTokensRef = useRef(
    new Map<number, { token: symbol; controller: AbortController }>(),
  );
  const selectionGenerationRef = useGenerationGuard();
  const navigationGenerationRef = useGenerationGuard();
  const captureNavigation = useCallback(() => {
    const ownsNavigation = navigationGenerationRef.capture();
    const ownsContext = beginOwnedRequest(() => () => true);
    return () => ownsNavigation() && ownsContext();
  }, [beginOwnedRequest]);
  const preservedStatusRef = useRef<string | null>(null);
  // The query/filter a page was actually loaded for. Load-more must continue
  // the loaded list, not whatever the user has since typed into the box.
  const loadedListContextRef = useRef<{ query: string; modeFilter: LibraryModeFilter }>({
    query: "",
    modeFilter: "all",
  });

  // Use the caller's selection: the ref can be one render ahead of state.
  // Capture the generation here so every caller fences repeated selections
  // of the same book as well as navigation to a different book.
  const beginSelectionOwnedRequest = useCallback(
    (
      beginRequest: () => () => boolean,
      targetBookId: number,
    ) => {
      const ownsSelection = selectionGenerationRef.capture();
      const isOwned = beginOwnedRequest(beginRequest);
      return () =>
        isOwned() &&
        selectedBookIdRef.current === targetBookId &&
        ownsSelection();
    },
    [beginOwnedRequest],
  );

  const clearInFlightDeleteRequests = useCallback(() => {
    for (const { controller } of inFlightDeleteTokensRef.current.values()) {
      controller.abort();
    }
    inFlightDeleteTokensRef.current.clear();
  }, []);

  const resetLibraryState = useCallback(() => {
    libraryIdentityRef.current = null;
    listAbortRef.current?.abort();
    listAbortRef.current = null;
    loadMoreAbortRef.current?.abort();
    loadMoreAbortRef.current = null;
    loadMoreInFlightRef.current = false;
    detailAbortRef.current?.abort();
    detailAbortRef.current = null;
    beginListRequest();
    beginDetailRequest();
    beginMutationRequest();
    beginPreviewRequest();
    beginEntityActionRequest();
    clearInFlightDeleteRequests();
    preservedStatusRef.current = null;
    nextOffsetRef.current = 0;
    hasMoreRef.current = false;
    unfilteredTotalRef.current = null;
    shelfRef.current = null;
    setItems([]);
    setUnfilteredPage(null);
    setIndexedCount(0);
    setHasMore(false);
    selectedBookIdRef.current = null;
    selectionGenerationRef.invalidate();
    setSelectedBookId(null);
    setSelectedBookIndex(-1);
    selectedBookDetailRef.current = null;
    setSelectedBookDetail(null);
    setLoadingList(false);
    setLoadingMore(false);
    setLoadingDetail(false);
    setErrorMessage("");
    setStatusMessage(backendDisconnected ? "Backend disconnected" : "Waiting for backend");
  }, [backendDisconnected, beginDetailRequest, beginEntityActionRequest, beginListRequest, beginMutationRequest, beginPreviewRequest, clearInFlightDeleteRequests]);

  useEffect(() => session.subscribe(resetLibraryState), [session, resetLibraryState]);

  const loadBookDetail = useCallback(
    async (
      bookId: number,
      options: { silentStatus?: boolean; preserveMessages?: boolean } = {},
    ): Promise<LibraryBookDetail | null> => {
      if (!repository) {
        setSelectedBookDetail(null);
        return null;
      }

      const isCurrent = beginOwnedRequest(beginDetailRequest);
      detailAbortRef.current?.abort();
      const controller = new AbortController();
      detailAbortRef.current = controller;
      setLoadingDetail(true);

      try {
        const detail = await repository.getBook(bookId, {
          signal: controller.signal,
          timeoutMs: LIBRARY_READ_TIMEOUT_MS,
        });
        if (!isCurrent()) {
          return null;
        }

        if (!acceptIdentity(detail)) return null;
        setSelectedBookDetail(detail);
        if (!options.silentStatus && preservedStatusRef.current === null) {
          setStatusMessage(`Selected: ${detail.title || "Untitled"}`);
        }
        return detail;
      } catch (error: unknown) {
        if (!isCurrent() || (error instanceof ApiError && error.code === "aborted")) {
          return null;
        }

        const message = error instanceof Error ? error.message : "failed to load book detail";
        setSelectedBookDetail(null);
        if (!options.preserveMessages) {
          setErrorMessage(message);
          setStatusMessage(message);
        }
        return null;
      } finally {
        if (isCurrent()) {
          setLoadingDetail(false);
        }
      }
    },
    [acceptIdentity, beginDetailRequest, beginOwnedRequest, repository],
  );

  const loadLibrary = useCallback(
    async (
      query: string,
      options: {
        silentDetailStatus?: boolean;
        preserveMessages?: boolean;
        modeFilter?: LibraryModeFilter;
        /** Only the query or filter changed, so the known shelf is still current. */
        sameLibrary?: boolean;
      } = {},
    ): Promise<void> => {
      if (!repository || backendDisconnected) {
        resetLibraryState();
        return;
      }

      const requestModeFilter = options.modeFilter ?? modeFilter;
      loadedListContextRef.current = { query, modeFilter: requestModeFilter };
      reloadContextGenerationRef.invalidate();
      const isCurrent = beginOwnedRequest(beginListRequest);
      // Load-more inherits the full ownership fence, so a new search, filter,
      // or api-client swap discards any page still in flight.
      listFenceRef.current = isCurrent;
      listAbortRef.current?.abort();
      const controller = new AbortController();
      listAbortRef.current = controller;
      // A fresh list load supersedes any in-flight page append.
      loadMoreAbortRef.current?.abort();
      loadMoreAbortRef.current = null;
      loadMoreInFlightRef.current = false;
      setLoadingMore(false);
      setLoadingList(true);
      if (!options.preserveMessages) {
        setErrorMessage("");
      }
      const filtered = Boolean(query) || requestModeFilter !== "all";
      if (shelfRef.current && !options.sameLibrary) shelfRef.current.stale = true;
      if (filtered) setUnfilteredPage((page) => page ?? shelfRef.current?.items ?? []);

      try {
        const page = await repository.listPage(query, requestModeFilter, 0, {
          signal: controller.signal,
          timeoutMs: LIBRARY_READ_TIMEOUT_MS,
        });
        if (!isCurrent()) {
          return;
        }

        if (!acceptIdentity(page)) return;
        const nextItems = page.items;
        setItems(nextItems);
        if (!filtered) {
          shelfRef.current = { items: nextItems, stale: false };
          setUnfilteredPage(null);
        } else if (!shelfRef.current || shelfRef.current.stale) {
          // A failed read keeps the last shelf; the next filtered load retries.
          void repository.listPage("", "all", 0, { signal: controller.signal, timeoutMs: LIBRARY_READ_TIMEOUT_MS })
            .then((shelf) => {
              if (!isCurrent()) return;
              shelfRef.current = { items: shelf.items, stale: false };
              setUnfilteredPage(shelf.items);
            }, () => undefined);
        }
        setHasMore(page.hasMore);
        hasMoreRef.current = page.hasMore;
        nextOffsetRef.current = nextItems.length;
        unfilteredTotalRef.current = null;

        // indexedCount is the total library size, which is only equal to the
        // first page's length for an unfiltered, single-page library; otherwise
        // ask the backend for the authoritative count.
        let countFailed = false;
        if (query || requestModeFilter !== "all" || page.hasMore) {
          try {
            const count = await repository.countBooks({
              signal: controller.signal,
              timeoutMs: LIBRARY_READ_TIMEOUT_MS,
            });
            if (!isCurrent()) return;
            setIndexedCount(count);
            // The count is the whole library, so it bounds only unfiltered paging.
            if (!query && requestModeFilter === "all") {
              unfilteredTotalRef.current = count;
              if (nextItems.length >= count) {
                hasMoreRef.current = false;
                setHasMore(false);
              }
            }
          } catch (error: unknown) {
            if (!isCurrent() || (error instanceof ApiError && error.code === "aborted")) return;
            // Keep the fetched page and last known total until refresh can retry the count.
            countFailed = true;
            if (!options.preserveMessages) {
              const message = error instanceof Error ? error.message : "failed to count library books";
              setErrorMessage(message);
              setStatusMessage(message);
            }
          }
        } else {
          setIndexedCount(nextItems.length);
        }

        let nextIndex = nextItems.findIndex((item) => item.id === selectedBookIdRef.current);
        const selectedId = selectedBookIdRef.current;
        if (nextIndex < 0 && selectedId !== null && selectedBookDetailRef.current?.id === selectedId) {
          const isSelectionCurrent = beginSelectionOwnedRequest(
            beginDetailRequest, selectedId,
          );
          detailAbortRef.current?.abort();
          detailAbortRef.current = null;
          setSelectedBookIndex(-1);
          setLoadingDetail(true);
          try {
            const detail = await repository.getBook(selectedId, {
              signal: controller.signal,
              timeoutMs: LIBRARY_READ_TIMEOUT_MS,
            });
            if (!isCurrent() || !isSelectionCurrent()) return;
            if (!acceptIdentity(detail)) return;
            setSelectedBookDetail(detail);
          } catch (error: unknown) {
            if (!isCurrent() || !isSelectionCurrent() || (error instanceof ApiError && error.code === "aborted")) return;
            if (!(error instanceof ApiError) || error.status !== 404) throw error;
            setLoadingDetail(false);
            selectedBookIdRef.current = null;
            selectionGenerationRef.invalidate();
            beginPreviewRequest();
            setSelectedBookId(null);
            setSelectedBookDetail(null);
          } finally {
            if (isCurrent() && isSelectionCurrent()) setLoadingDetail(false);
          }
          return;
        }

        if (nextItems.length === 0) {
          detailAbortRef.current?.abort();
          detailAbortRef.current = null;
          beginDetailRequest();
          selectionGenerationRef.invalidate();
          beginPreviewRequest();
          setSelectedBookIndex(-1);
          selectedBookIdRef.current = null;
          setSelectedBookId(null);
          setSelectedBookDetail(null);
          setLoadingDetail(false);
          if (!options.preserveMessages && !countFailed) {
            setStatusMessage(query || requestModeFilter !== "all" ? "No books match" : "Library empty");
          }
          return;
        }
        if (nextIndex < 0 && selectedBookIdRef.current === null) {
          // First load: reopen the book the listener last played, when it is on this page.
          const lastPlayedId = readLastPlayedBookId();
          nextIndex = lastPlayedId === null ? -1 : nextItems.findIndex((item) => item.id === lastPlayedId);
        }
        if (nextIndex < 0) nextIndex = 0;

        const nextBook = nextItems[nextIndex];
        if (nextBook.id !== selectedBookIdRef.current) {
          selectionGenerationRef.invalidate();
          beginPreviewRequest();
        }
        setSelectedBookIndex(nextIndex);
        selectedBookIdRef.current = nextBook.id;
        setSelectedBookId(nextBook.id);
        await loadBookDetail(nextBook.id, {
          silentStatus: options.silentDetailStatus || countFailed,
          preserveMessages: options.preserveMessages,
        });
      } catch (error: unknown) {
        if (!isCurrent() || (error instanceof ApiError && error.code === "aborted")) {
          return;
        }

        if (!options.preserveMessages) {
          const message = error instanceof Error ? error.message : "failed to load library";
          setErrorMessage(message);
          setStatusMessage(message);
        }
      } finally {
        if (isCurrent()) {
          setLoadingList(false);
          setCompletedListLoads((count) => count + 1);
        }
      }
    },
    [acceptIdentity, backendDisconnected, beginDetailRequest, beginListRequest, beginOwnedRequest, beginPreviewRequest, beginSelectionOwnedRequest, loadBookDetail, modeFilter, repository, resetLibraryState],
  );

  /** Apply a local edit to every list that shows the book, the cached shelf included. */
  const patchBook = useCallback((bookId: number, patch: Partial<LibraryBookSummary>) => {
    const apply = (list: LibraryBookSummary[]) =>
      list.map((item) => (item.id === bookId ? { ...item, ...patch } : item));
    setItems(apply);
    setUnfilteredPage((page) => page && apply(page));
    if (shelfRef.current) shelfRef.current = { ...shelfRef.current, items: apply(shelfRef.current.items) };
  }, []);

  const selectBookAtIndex = useCallback((bookId: number, index: number): Promise<LibraryBookDetail | null> => {
    navigationGenerationRef.invalidate();
    setPendingDeletionPath(null);
    selectionGenerationRef.invalidate();
    beginPreviewRequest();
    preservedStatusRef.current = null;
    setSelectedBookIndex(index);
    selectedBookIdRef.current = bookId;
    setSelectedBookId(bookId);
    setErrorMessage("");
    return loadBookDetail(bookId);
  }, [beginPreviewRequest, loadBookDetail]);

  // A book off the loaded pages is selected at index -1, like a selection a reload kept.
  const selectBookById = useCallback(
    (bookId: number) => selectBookAtIndex(bookId, items.findIndex((item) => item.id === bookId)),
    [items, selectBookAtIndex],
  );

  const selectBook = useCallback(
    async (index: number): Promise<LibraryBookDetail | null> => {
      const nextBook = items[index];
      return nextBook ? selectBookAtIndex(nextBook.id, index) : null;
    },
    [items, selectBookAtIndex],
  );

  const fetchMorePage = useCallback(async (): Promise<LibraryBookSummary[]> => {
    if (!repository || backendDisconnected || !hasMoreRef.current || loadMoreInFlightRef.current) {
      return [];
    }

    // Fence to the list generation captured by the most recent load. A search,
    // mode change, or api-client swap replaces this fence, so any page that
    // lands afterwards is discarded instead of being appended onto a different
    // query's results.
    const isCurrent = listFenceRef.current;
    if (!isCurrent()) {
      return [];
    }

    loadMoreInFlightRef.current = true;
    const controller = new AbortController();
    loadMoreAbortRef.current = controller;
    const requestOffset = nextOffsetRef.current;
    const context = loadedListContextRef.current;
    setLoadingMore(true);
    setErrorMessage("");

    try {
      const page = await repository.listPage(context.query, context.modeFilter, requestOffset, {
        signal: controller.signal,
        timeoutMs: LIBRARY_READ_TIMEOUT_MS,
      });
      if (!isCurrent()) {
        return [];
      }

      if (!acceptIdentity(page)) return [];
      setItems((previous) => appendUniqueBooks(previous, page.items));
      // Advance by what the backend returned so the next request continues past
      // this page even if some ids were de-duplicated on the client.
      nextOffsetRef.current = requestOffset + page.items.length;
      // A full page only suggests more rows; a known total says when the last page arrived.
      const total = unfilteredTotalRef.current;
      const more = page.hasMore && (total === null || nextOffsetRef.current < total);
      hasMoreRef.current = more;
      setHasMore(more);
      return page.items;
    } catch (error: unknown) {
      if (!isCurrent() || (error instanceof ApiError && error.code === "aborted")) {
        return [];
      }
      const message = error instanceof Error ? error.message : "failed to load more books";
      setErrorMessage(message);
      setStatusMessage(message);
      return [];
    } finally {
      if (loadMoreAbortRef.current === controller) {
        loadMoreInFlightRef.current = false;
        loadMoreAbortRef.current = null;
        if (isCurrent()) {
          setLoadingMore(false);
        }
      }
    }
  }, [acceptIdentity, backendDisconnected, repository]);

  const loadMorePage = useCallback((): Promise<LibraryBookSummary[]> => {
    // Playback reaching the page boundary joins an existing shelf request.
    // It must not mistake an in-flight page for the end of the library.
    if (loadMoreInFlightRef.current && loadMoreRequestRef.current) return loadMoreRequestRef.current;
    const request = fetchMorePage().finally(() => {
      if (loadMoreRequestRef.current === request) loadMoreRequestRef.current = null;
    });
    loadMoreRequestRef.current = request;
    return request;
  }, [fetchMorePage]);

  const loadMore = useCallback(async (): Promise<void> => {
    await loadMorePage();
  }, [loadMorePage]);

  const selectRelative = useCallback(
    async (delta: number, fromBookId: number | null = null): Promise<LibraryBookDetail | null> => {
      if (!items.length || delta === 0) {
        return null;
      }

      const fromIndex = fromBookId === null ? selectedBookIndex : items.findIndex((item) => item.id === fromBookId);
      if (fromBookId !== null && fromIndex < 0) return null;
      const baseIndex = fromIndex >= 0 ? fromIndex : 0;
      const nextIndex = baseIndex + delta;
      if (nextIndex >= 0 && nextIndex < items.length) {
        return selectBook(nextIndex);
      }
      if (delta !== 1 || nextIndex !== items.length || !hasMoreRef.current) {
        return null;
      }

      // Stepping past the last loaded row pulls the next page so keyboard
      // navigation is not capped at the current page boundary.
      const selectionOwner = selectedBookIdRef.current;
      if (selectionOwner === null) return null;
      const isCurrentSelection = beginSelectionOwnedRequest(
        () => () => true, selectionOwner,
      );
      const knownIds = new Set(items.map((item) => item.id));
      const incoming = await loadMorePage();
      if (!isCurrentSelection()) return null;
      const nextBook = incoming.find((item) => !knownIds.has(item.id));
      if (!nextBook) {
        return null;
      }

      return selectBookAtIndex(nextBook.id, items.length);
    },
    [beginSelectionOwnedRequest, items, loadMorePage, selectBook, selectBookAtIndex, selectedBookIndex],
  );

  const refreshLibrary = useCallback(async (): Promise<void> => {
    dismissPendingDeletion();
    preservedStatusRef.current = null;
    await loadLibrary(searchQuery.trim());
  }, [dismissPendingDeletion, loadLibrary, searchQuery]);

  const reloadCurrentQuery = useCallback(async (): Promise<void> => {
    await loadLibrary(searchQuery.trim());
  }, [loadLibrary, searchQuery]);

  const reloadLibrarySession = useCallback(async (): Promise<void> => {
    session.invalidate();
    const context = reloadContextIdentityRef.current;
    await loadLibrary(context.query, { modeFilter: context.modeFilter });
  }, [session, loadLibrary]);
  useLayoutEffect(() => {
    reloadSessionRef.current = reloadLibrarySession;
  });

  const loadCleaningPreview = useCallback(
    async (chapterNumber: number): Promise<CleaningPreviewResponse | null> => {
      if (!repository || !selectedBookId) {
        setStatusMessage("No book selected");
        return null;
      }

      const targetBookId = selectedBookId;
      const isCurrentPreview = beginSelectionOwnedRequest(
        beginPreviewRequest,
        targetBookId,
      );
      setErrorMessage("");
      try {
        const preview = await repository.getCleaningPreview(targetBookId, chapterNumber, { timeoutMs: CLEANING_PREVIEW_REQUEST_TIMEOUT_MS });
        if (!isCurrentPreview()) {
          return null;
        }
        setStatusMessage(preview.available ? "" : "Cleaning preview unavailable");
        return preview;
      } catch (error: unknown) {
        if (!isCurrentPreview()) {
          return null;
        }
        const message = error instanceof Error ? error.message : "failed to load cleaning preview";
        setErrorMessage(message);
        setStatusMessage(message);
        return null;
      }
    },
    [beginPreviewRequest, beginSelectionOwnedRequest, repository, selectedBookId],
  );

  useEffect(() => {
    return () => {
      listAbortRef.current?.abort();
      loadMoreAbortRef.current?.abort();
      loadMoreAbortRef.current = null;
      loadMoreInFlightRef.current = false;
      detailAbortRef.current?.abort();
      clearInFlightDeleteRequests();
      beginListRequest();
      beginDetailRequest();
      beginMutationRequest();
      beginPreviewRequest();
      beginEntityActionRequest();
    };
  }, [beginDetailRequest, beginEntityActionRequest, beginListRequest, beginMutationRequest, beginPreviewRequest, clearInFlightDeleteRequests]);

  useEffect(() => {
    // A new API client/backend owns a distinct mutation context. Retire any
    // delete owners from the previous context so a hung request cannot block
    // retries against the replacement client.
    clearInFlightDeleteRequests();
  }, [apiClient, backendDisconnected, clearInFlightDeleteRequests]);

  useEffect(() => {
    if (!repository || backendDisconnected) {
      loadedRepositoryRef.current = null;
      resetLibraryState();
      return;
    }

    const loadImmediately = loadedRepositoryRef.current !== repository;
    loadedRepositoryRef.current = repository;

    // A changed query/filter invalidates list pages immediately, not after the
    // debounce. Otherwise an in-flight load-more response can land during the
    // debounce window and mix records from two different queries.
    listAbortRef.current?.abort();
    listAbortRef.current = null;
    loadMoreAbortRef.current?.abort();
    loadMoreAbortRef.current = null;
    loadMoreInFlightRef.current = false;
    listFenceRef.current = beginOwnedRequest(beginListRequest);
    nextOffsetRef.current = 0;
    hasMoreRef.current = false;
    setHasMore(false);
    setLoadingMore(false);

    if (loadImmediately) {
      void loadLibrary(searchQuery.trim());
      return;
    }

    const timer = window.setTimeout(() => {
      void loadLibrary(searchQuery.trim(), { sameLibrary: true });
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
    };
  }, [backendDisconnected, beginListRequest, beginOwnedRequest, loadLibrary, repository, resetLibraryState, searchQuery]);

  return {
    captureNavigation,
    repository,
    searchQuery,
    setSearchQuery,
    modeFilter,
    setModeFilter,
    items,
    setItems,
    patchBook,
    unfilteredItems: unfilteredPage ?? items,
    indexedCount,
    setIndexedCount,
    hasMore,
    selectedBookId,
    setSelectedBookId,
    selectedBookIndex,
    setSelectedBookIndex,
    selectedBookDetail,
    setSelectedBookDetail,
    loadingList,
    setLoadingList,
    completedListLoads,
    loadingMore,
    setLoadingMore,
    loadingDetail,
    setLoadingDetail,
    errorMessage,
    setErrorMessage,
    statusMessage,
    setStatusMessage,
    pendingDeletionPath,
    setPendingDeletionPath,
    dismissPendingDeletion,
    queryContextGenerationRef,
    reloadContextIdentityRef,
    beginListRequest,
    beginDetailRequest,
    beginMutationRequest,
    beginPreviewRequest,
    beginEntityActionRequest,
    beginApiOwnedRequest,
    getReloadContextGeneration,
    listAbortRef,
    loadMoreAbortRef,
    loadMoreInFlightRef,
    listFenceRef,
    detailAbortRef,
    selectedBookIdRef,
    inFlightDeleteTokensRef,
    selectionGenerationRef,
    preservedStatusRef,
    beginSelectionOwnedRequest,
    loadLibrary,
    selectBook,
    selectBookById,
    loadMore,
    selectRelative,
    refreshLibrary,
    reloadCurrentQuery,
    reloadLibrarySession,
    loadCleaningPreview,
  };
}
