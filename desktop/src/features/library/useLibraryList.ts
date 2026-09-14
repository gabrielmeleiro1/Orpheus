import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import { isAbortLikeError, useLatestRequest } from "../conversion/useLatestRequest";
import type {
  CleaningPreviewResponse,
  LibraryBookDetail,
  LibraryBookSummary,
} from "../../types/api";
import { clearPlaybackCheckpoint } from "../player/playbackCheckpoint";
import { buildBookSummary, formatSummaryMeta } from "./libraryDisplayModel";
import { createLibraryRepository, type LibraryModeFilter, type LibraryRepository } from "./libraryRepository";

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

const SEARCH_DEBOUNCE_MS = 220;

interface UseLibraryListArgs {
  apiClient: ApiClient | null;
  backendDisconnected: boolean;
}

export interface UseLibraryListResult {
  searchQuery: string;
  setSearchQuery: (value: string) => void;
  modeFilter: LibraryModeFilter;
  setModeFilter: (value: LibraryModeFilter) => void;
  items: LibraryBookSummary[];
  indexedCount: number;
  hasMore: boolean;
  selectedBookId: number | null;
  selectedBookIndex: number;
  selectedBookDetail: LibraryBookDetail | null;
  selectedBookPlayable: boolean;
  loadingList: boolean;
  loadingMore: boolean;
  loadingDetail: boolean;
  errorMessage: string;
  statusMessage: string;
  bookSummaryTitle: string;
  bookSummaryMeta: string;
  bookDetailText: string;
  refreshLibrary: () => Promise<void>;
  reloadCurrentQuery: () => Promise<void>;
  loadMore: () => Promise<void>;
  selectBook: (index: number) => Promise<LibraryBookDetail | null>;
  selectRelative: (delta: number) => Promise<LibraryBookDetail | null>;
  updateSelectedBookMetadata: (payload: { title: string; author: string; tags: string }) => Promise<void>;
  loadCleaningPreview: (chapterNumber: number) => Promise<CleaningPreviewResponse | null>;
  repairSelectedBook: () => Promise<void>;
  openSelectedBookFolder: () => Promise<void>;
  deleteSelectedBook: () => Promise<boolean>;
  setSelectedPlaybackPosition: (positionSec: number) => void;
  getReloadContextGeneration: () => number;
}

export function useLibraryList({ apiClient, backendDisconnected }: UseLibraryListArgs): UseLibraryListResult {
  const repository = useMemo(() => (apiClient ? createLibraryRepository(apiClient) : null), [apiClient]);

  const [searchQuery, setSearchQuery] = useState("");
  const [modeFilter, setModeFilter] = useState<LibraryModeFilter>("all");
  const [items, setItems] = useState<LibraryBookSummary[]>([]);
  const [indexedCount, setIndexedCount] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [selectedBookId, setSelectedBookId] = useState<number | null>(null);
  const [selectedBookIndex, setSelectedBookIndex] = useState(-1);
  const [selectedBookDetail, setSelectedBookDetail] = useState<LibraryBookDetail | null>(null);

  const [loadingList, setLoadingList] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [statusMessage, setStatusMessage] = useState("Library idle");

  const requestContextGenerationRef = useRef(0);
  const queryContextGenerationRef = useRef(0);
  const reloadContextGenerationRef = useRef(0);
  const requestContextIdentityRef = useRef({ apiClient, backendDisconnected });
  const reloadContextIdentityRef = useRef({ query: searchQuery.trim(), modeFilter });
  if (
    requestContextIdentityRef.current.apiClient !== apiClient ||
    requestContextIdentityRef.current.backendDisconnected !== backendDisconnected
  ) {
    requestContextIdentityRef.current = { apiClient, backendDisconnected };
    requestContextGenerationRef.current += 1;
    reloadContextGenerationRef.current += 1;
  }
  if (
    reloadContextIdentityRef.current.query !== searchQuery.trim() ||
    reloadContextIdentityRef.current.modeFilter !== modeFilter
  ) {
    reloadContextIdentityRef.current = { query: searchQuery.trim(), modeFilter };
    queryContextGenerationRef.current += 1;
    reloadContextGenerationRef.current += 1;
  }

  const beginListRequest = useLatestRequest();
  const beginDetailRequest = useLatestRequest();
  const beginMutationRequest = useLatestRequest();
  const beginPreviewRequest = useLatestRequest();
  const beginEntityActionRequest = useLatestRequest();
  const beginApiOwnedRequest = useCallback((beginRequest: () => () => boolean) => {
    const requestContextGeneration = requestContextGenerationRef.current;
    const isLatest = beginRequest();
    return () =>
      isLatest() &&
      requestContextGenerationRef.current === requestContextGeneration;
  }, []);
  const beginOwnedRequest = useCallback((beginRequest: () => () => boolean) => {
    const queryContextGeneration = queryContextGenerationRef.current;
    const isApiCurrent = beginApiOwnedRequest(beginRequest);
    return () =>
      isApiCurrent() &&
      queryContextGenerationRef.current === queryContextGeneration;
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
  const listFenceRef = useRef<() => boolean>(() => false);
  const nextOffsetRef = useRef(0);
  const hasMoreRef = useRef(false);
  const loadedRepositoryRef = useRef<typeof repository>(null);
  const detailAbortRef = useRef<AbortController | null>(null);
  const selectedBookIdRef = useRef<number | null>(null);
  const selectedBookDetailRef = useRef<LibraryBookDetail | null>(null);
  selectedBookDetailRef.current = selectedBookDetail;
  const inFlightDeleteTokensRef = useRef(
    new Map<number, { token: symbol; controller: AbortController }>(),
  );
  const selectionGenerationRef = useRef(0);
  const preservedStatusRef = useRef<string | null>(null);
  // The query/filter a page was actually loaded for. Load-more must continue
  // the loaded list, not whatever the user has since typed into the box.
  const loadedListContextRef = useRef<{ query: string; modeFilter: LibraryModeFilter }>({
    query: "",
    modeFilter: "all",
  });

  // Sibling of beginOwnedRequest for selection-scoped work. Targets are passed
  // in, never read off selectedBookIdRef here: callbacks capture the
  // selectedBookId state value and the ref can lead it by one render. Both
  // clauses are load-bearing — selectRelative advances the id ref without
  // bumping the generation, and re-selecting the same book does the reverse.
  const beginSelectionOwnedRequest = useCallback(
    (
      beginRequest: () => () => boolean,
      targetBookId: number,
      targetSelectionGeneration: number,
    ) => {
      const isOwned = beginOwnedRequest(beginRequest);
      return () =>
        isOwned() &&
        selectedBookIdRef.current === targetBookId &&
        selectionGenerationRef.current === targetSelectionGeneration;
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
    setItems([]);
    setIndexedCount(0);
    setHasMore(false);
    selectedBookIdRef.current = null;
    selectionGenerationRef.current += 1;
    setSelectedBookId(null);
    setSelectedBookIndex(-1);
    setSelectedBookDetail(null);
    setLoadingList(false);
    setLoadingMore(false);
    setLoadingDetail(false);
    setErrorMessage("");
    setStatusMessage(backendDisconnected ? "Backend disconnected" : "Waiting for backend");
  }, [backendDisconnected, beginDetailRequest, beginEntityActionRequest, beginListRequest, beginMutationRequest, beginPreviewRequest, clearInFlightDeleteRequests]);

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
        const detail = await repository.getBook(bookId, { signal: controller.signal });
        if (!isCurrent()) {
          return null;
        }

        setSelectedBookDetail(detail);
        if (!options.silentStatus && preservedStatusRef.current === null) {
          setStatusMessage(`Selected: ${detail.title || "Untitled"}`);
        }
        return detail;
      } catch (error: unknown) {
        if (!isCurrent() || isAbortLikeError(error)) {
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
    [beginDetailRequest, beginOwnedRequest, repository],
  );

  const loadLibrary = useCallback(
    async (
      query: string,
      options: {
        silentDetailStatus?: boolean;
        preserveMessages?: boolean;
        modeFilter?: LibraryModeFilter;
      } = {},
    ): Promise<void> => {
      if (!repository || backendDisconnected) {
        resetLibraryState();
        return;
      }

      const requestModeFilter = options.modeFilter ?? modeFilter;
      loadedListContextRef.current = { query, modeFilter: requestModeFilter };
      reloadContextGenerationRef.current += 1;
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

      try {
        const page = await repository.listPage(query, requestModeFilter, 0, { signal: controller.signal });
        if (!isCurrent()) {
          return;
        }

        const nextItems = page.items;
        // indexedCount is the total library size, which is only equal to the
        // first page's length for an unfiltered, single-page library; otherwise
        // ask the backend for the authoritative count.
        const nextIndexedCount = query || requestModeFilter !== "all" || page.hasMore
          ? await repository.countBooks({ signal: controller.signal })
          : nextItems.length;
        if (!isCurrent()) {
          return;
        }

        setItems(nextItems);
        setIndexedCount(nextIndexedCount);
        setHasMore(page.hasMore);
        hasMoreRef.current = page.hasMore;
        nextOffsetRef.current = nextItems.length;

        if (nextItems.length === 0) {
          const preserveSelectionForPlayback =
            (query.trim() !== "" || requestModeFilter !== "all") &&
            selectedBookIdRef.current !== null &&
            selectedBookDetailRef.current !== null;
          detailAbortRef.current?.abort();
          detailAbortRef.current = null;
          beginDetailRequest();
          selectionGenerationRef.current += 1;
          beginPreviewRequest();
          setSelectedBookIndex(-1);
          if (!preserveSelectionForPlayback) {
            selectedBookIdRef.current = null;
            setSelectedBookId(null);
            setSelectedBookDetail(null);
          }
          setLoadingDetail(false);
          if (!options.preserveMessages) {
            setStatusMessage(query || requestModeFilter !== "all" ? "No books match" : "Library empty");
          }
          return;
        }

        let nextIndex = nextItems.findIndex((item) => item.id === selectedBookIdRef.current);
        if (nextIndex < 0) {
          if (
            (query.trim() !== "" || requestModeFilter !== "all") &&
            selectedBookIdRef.current !== null &&
            selectedBookDetailRef.current !== null
          ) {
            // Keep the current selection alive for the player while a search or
            // filter temporarily hides it. The detail pane hides selections
            // with index -1, while playback remains independent of the result
            // set and is restored when the book becomes visible again.
            setSelectedBookIndex(-1);
            return;
          }
          nextIndex = 0;
        }

        const nextBook = nextItems[nextIndex];
        if (nextBook.id !== selectedBookIdRef.current) {
          selectionGenerationRef.current += 1;
          beginPreviewRequest();
        }
        setSelectedBookIndex(nextIndex);
        selectedBookIdRef.current = nextBook.id;
        setSelectedBookId(nextBook.id);
        await loadBookDetail(nextBook.id, {
          silentStatus: options.silentDetailStatus,
          preserveMessages: options.preserveMessages,
        });
      } catch (error: unknown) {
        if (!isCurrent() || isAbortLikeError(error)) {
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
        }
      }
    },
    [backendDisconnected, beginDetailRequest, beginListRequest, beginOwnedRequest, beginPreviewRequest, loadBookDetail, modeFilter, repository, resetLibraryState],
  );

  const selectBook = useCallback(
    async (index: number): Promise<LibraryBookDetail | null> => {
      if (index < 0 || index >= items.length) {
        return null;
      }

      const nextBook = items[index];
      selectionGenerationRef.current += 1;
      beginPreviewRequest();
      preservedStatusRef.current = null;
      setSelectedBookIndex(index);
      selectedBookIdRef.current = nextBook.id;
      setSelectedBookId(nextBook.id);
      setErrorMessage("");
      return loadBookDetail(nextBook.id);
    },
    [beginPreviewRequest, items, loadBookDetail],
  );

  const loadMorePage = useCallback(async (): Promise<LibraryBookSummary[]> => {
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

    try {
      const page = await repository.listPage(context.query, context.modeFilter, requestOffset, {
        signal: controller.signal,
      });
      if (!isCurrent()) {
        return [];
      }

      setItems((previous) => appendUniqueBooks(previous, page.items));
      // Advance by what the backend returned so the next request continues past
      // this page even if some ids were de-duplicated on the client.
      nextOffsetRef.current = requestOffset + page.items.length;
      hasMoreRef.current = page.hasMore;
      setHasMore(page.hasMore);
      return page.items;
    } catch (error: unknown) {
      if (!isCurrent() || isAbortLikeError(error)) {
        return [];
      }
      const message = error instanceof Error ? error.message : "failed to load more books";
      setErrorMessage(message);
      setStatusMessage(message);
      return [];
    } finally {
      loadMoreInFlightRef.current = false;
      if (loadMoreAbortRef.current === controller) {
        loadMoreAbortRef.current = null;
      }
      if (isCurrent()) {
        setLoadingMore(false);
      }
    }
  }, [backendDisconnected, repository]);

  const loadMore = useCallback(async (): Promise<void> => {
    await loadMorePage();
  }, [loadMorePage]);

  const selectRelative = useCallback(
    async (delta: number): Promise<LibraryBookDetail | null> => {
      if (!items.length || delta === 0) {
        return null;
      }

      const baseIndex = selectedBookIndex >= 0 ? selectedBookIndex : 0;
      const nextIndex = baseIndex + delta;
      if (nextIndex >= 0 && nextIndex < items.length) {
        return selectBook(nextIndex);
      }
      if (delta !== 1 || nextIndex !== items.length || !hasMoreRef.current) {
        return null;
      }

      // Stepping past the last loaded row pulls the next page so keyboard
      // navigation is not capped at the current page boundary.
      const knownIds = new Set(items.map((item) => item.id));
      const incoming = await loadMorePage();
      const nextBook = incoming.find((item) => !knownIds.has(item.id));
      if (!nextBook) {
        return null;
      }

      const appendedIndex = items.length;
      preservedStatusRef.current = null;
      setSelectedBookIndex(appendedIndex);
      selectedBookIdRef.current = nextBook.id;
      setSelectedBookId(nextBook.id);
      setErrorMessage("");
      return loadBookDetail(nextBook.id);
    },
    [items, loadBookDetail, loadMorePage, selectBook, selectedBookIndex],
  );

  const refreshLibrary = useCallback(async (): Promise<void> => {
    preservedStatusRef.current = null;
    await loadLibrary(searchQuery.trim());
  }, [loadLibrary, searchQuery]);

  const reloadCurrentQuery = useCallback(async (): Promise<void> => {
    await loadLibrary(searchQuery.trim());
  }, [loadLibrary, searchQuery]);

  /** Fire-and-report action against the selected book: identical selection
   * ownership, status, and error handling for every entity endpoint. */
  const runEntityAction = useCallback(
    async <T,>(
      call: (repo: LibraryRepository, bookId: number) => Promise<T>,
      describeSuccess: (result: T) => string,
      fallbackMessage: string,
    ): Promise<void> => {
      if (!repository || !selectedBookId) {
        setStatusMessage("No book selected");
        return;
      }

      const targetBookId = selectedBookId;
      const isCurrentAction = beginSelectionOwnedRequest(
        beginEntityActionRequest,
        targetBookId,
        selectionGenerationRef.current,
      );
      setErrorMessage("");

      try {
        const result = await call(repository, targetBookId);
        if (!isCurrentAction()) {
          return;
        }
        setStatusMessage(describeSuccess(result));
      } catch (error: unknown) {
        if (!isCurrentAction()) {
          return;
        }
        const message = error instanceof Error ? error.message : fallbackMessage;
        setErrorMessage(message);
        setStatusMessage(message);
      }
    },
    [beginEntityActionRequest, beginSelectionOwnedRequest, repository, selectedBookId],
  );

  const openSelectedBookFolder = useCallback(
    (): Promise<void> =>
      runEntityAction(
        (repo, bookId) => repo.openBookFolder(bookId),
        (response) =>
          response.folder_path?.trim() ? `opened folder: ${response.folder_path}` : "opened folder",
        "failed to open book folder",
      ),
    [runEntityAction],
  );

  const deleteSelectedBook = useCallback(async (): Promise<boolean> => {
    if (!repository || !selectedBookId) {
      setStatusMessage("No book selected");
      return false;
    }

    const deleteOwnerToken = Symbol();
    if (inFlightDeleteTokensRef.current.has(selectedBookId)) {
      return false;
    }
    const deleteController = new AbortController();
    inFlightDeleteTokensRef.current.set(selectedBookId, {
      token: deleteOwnerToken,
      controller: deleteController,
    });

    const title = selectedBookDetail?.title || items.find((item) => item.id === selectedBookId)?.title || "selected book";
    const deletedStatus = `Deleted ${title}`;
    const selectionGenerationAtDeleteStart = selectionGenerationRef.current;
    const queryContextGenerationAtDeleteStart = queryContextGenerationRef.current;
    const isDeleteQueryCurrent = () =>
      queryContextGenerationRef.current === queryContextGenerationAtDeleteStart;
    const isCurrentMutation = beginApiOwnedRequest(beginMutationRequest);
    const isCurrentList = beginApiOwnedRequest(beginListRequest);
    listFenceRef.current = isCurrentList;
    loadMoreAbortRef.current?.abort();
    loadMoreAbortRef.current = null;
    loadMoreInFlightRef.current = false;
    setLoadingMore(false);
    listAbortRef.current?.abort();
    listAbortRef.current = null;
    detailAbortRef.current?.abort();
    detailAbortRef.current = null;
    beginDetailRequest();
    setLoadingList(false);
    setLoadingDetail(false);
    const previousItems = items;
    const previousSelectedBookId = selectedBookId;
    const previousSelectedBookIndex = selectedBookIndex;
    const previousSelectedBookDetail = selectedBookDetail;
    const previousIndexedCount = indexedCount;
    const deleteIndex = items.findIndex((item) => item.id === selectedBookId);
    const optimisticItems = items.filter((item) => item.id !== selectedBookId);
    const nextIndex = optimisticItems.length > 0 ? Math.min(Math.max(deleteIndex, 0), optimisticItems.length - 1) : -1;
    const nextBook = nextIndex >= 0 ? optimisticItems[nextIndex] : null;
    const optimisticSelectedBookId = nextBook?.id ?? null;
    if (optimisticSelectedBookId !== selectedBookIdRef.current) {
      selectionGenerationRef.current += 1;
      beginPreviewRequest();
    }
    const optimisticSelectionGeneration =
      selectionGenerationAtDeleteStart + (optimisticSelectedBookId === selectedBookId ? 0 : 1);

    setErrorMessage("");
    preservedStatusRef.current = null;
    setStatusMessage(`Deleting ${title}…`);
    setItems(optimisticItems);
    setIndexedCount(Math.max(0, indexedCount - 1));
    setSelectedBookIndex(nextIndex);
    selectedBookIdRef.current = optimisticSelectedBookId;
    setSelectedBookId(optimisticSelectedBookId);
    setSelectedBookDetail(null);

    try {
      await repository.deleteBook(selectedBookId, { signal: deleteController.signal });
      if (!isCurrentMutation()) {
        return true;
      }
      clearPlaybackCheckpoint(selectedBookId);

      const selectionStillDeleteOwned = () =>
        selectedBookIdRef.current === optimisticSelectedBookId &&
        selectionGenerationRef.current === optimisticSelectionGeneration;
      const currentListContext = reloadContextIdentityRef.current;
      const selectionOwnedBeforeReconcile = selectionStillDeleteOwned();
      const preserveNewerSelectionMessages = !selectionOwnedBeforeReconcile;
      const publishDeletedStatus =
        selectionOwnedBeforeReconcile && isDeleteQueryCurrent();
      if (publishDeletedStatus) {
        preservedStatusRef.current = deletedStatus;
        setStatusMessage(deletedStatus);
      }
      await loadLibrary(currentListContext.query, {
        modeFilter: currentListContext.modeFilter,
        silentDetailStatus:
          publishDeletedStatus || preserveNewerSelectionMessages,
        preserveMessages: preserveNewerSelectionMessages,
      });
      if (!isCurrentMutation()) {
        return true;
      }

      if (selectionStillDeleteOwned() && isDeleteQueryCurrent()) {
        setStatusMessage(deletedStatus);
        // Exactly one preserved status ("deleted X") survives the delete's own
        // silent reload; release it here so the next search-triggered detail
        // load resumes the normal "selected: X" status flow.
        preservedStatusRef.current = null;
      }
      return true;
    } catch (error: unknown) {
      if (!isCurrentMutation() || !isCurrentList()) {
        return false;
      }

      const selectionStillDeleteOwned =
        selectedBookIdRef.current === optimisticSelectedBookId &&
        selectionGenerationRef.current === optimisticSelectionGeneration;
      if (!selectionStillDeleteOwned || !isDeleteQueryCurrent()) {
        const currentListContext = reloadContextIdentityRef.current;
        const preserveNewerSelectionMessages = !selectionStillDeleteOwned;
        await loadLibrary(currentListContext.query, {
          modeFilter: currentListContext.modeFilter,
          silentDetailStatus: preserveNewerSelectionMessages,
          preserveMessages: preserveNewerSelectionMessages,
        });
        return false;
      }

      const message = error instanceof Error ? error.message : "failed to delete book";
      preservedStatusRef.current = null;
      if (previousSelectedBookId !== selectedBookIdRef.current) {
        selectionGenerationRef.current += 1;
        beginPreviewRequest();
      }
      setItems(previousItems);
      setIndexedCount(previousIndexedCount);
      setSelectedBookIndex(previousSelectedBookIndex);
      selectedBookIdRef.current = previousSelectedBookId;
      setSelectedBookId(previousSelectedBookId);
      setSelectedBookDetail(previousSelectedBookDetail);
      setErrorMessage(message);
      setStatusMessage(message);
      return false;
    } finally {
      if (inFlightDeleteTokensRef.current.get(selectedBookId)?.token === deleteOwnerToken) {
        inFlightDeleteTokensRef.current.delete(selectedBookId);
      }
    }
  }, [beginApiOwnedRequest, beginDetailRequest, beginListRequest, beginMutationRequest, beginPreviewRequest, indexedCount, items, loadLibrary, repository, selectedBookDetail, selectedBookId, selectedBookIndex]);

  const updateSelectedBookMetadata = useCallback(
    async (payload: { title: string; author: string; tags: string }): Promise<void> => {
      if (!repository || !selectedBookId) {
        setStatusMessage("No book selected");
        return;
      }

      const targetBookId = selectedBookId;
      const isCurrentMutation = beginSelectionOwnedRequest(
        beginMutationRequest,
        targetBookId,
        selectionGenerationRef.current,
      );
      setErrorMessage("");

      try {
        const updated = await repository.updateBook(targetBookId, {
          title: payload.title.trim() || null,
          author: payload.author.trim() || null,
          tags: payload.tags.trim(),
        });
        if (!isCurrentMutation()) {
          return;
        }
        setSelectedBookDetail((current) => ({
          ...(current ?? updated),
          ...updated,
          chapters: current?.chapters ?? updated.chapters ?? [],
          playback_url: current?.playback_url ?? updated.playback_url ?? null,
        }));
        setItems((current) =>
          current.map((item) =>
            item.id === targetBookId
              ? {
                  ...item,
                  title: updated.title,
                  author: updated.author,
                  tags: updated.tags,
                  source_available: updated.source_available,
                }
              : item,
          ),
        );
        setStatusMessage("Metadata saved");
      } catch (error: unknown) {
        if (!isCurrentMutation()) {
          return;
        }
        const message = error instanceof Error ? error.message : "failed to update book";
        setErrorMessage(message);
        setStatusMessage(message);
      }
    },
    [beginMutationRequest, beginSelectionOwnedRequest, repository, selectedBookId],
  );

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
        selectionGenerationRef.current,
      );
      setErrorMessage("");
      try {
        const preview = await repository.getCleaningPreview(targetBookId, chapterNumber);
        if (!isCurrentPreview()) {
          return null;
        }
        setStatusMessage(preview.available ? `Loaded cleaning preview for chapter ${chapterNumber}` : "Cleaning preview unavailable");
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

  const repairSelectedBook = useCallback(
    (): Promise<void> =>
      runEntityAction(
        (repo, bookId) => repo.repairBook(bookId),
        (response) => `Repair started: ${response.job_id}`,
        "failed to start repair",
      ),
    [runEntityAction],
  );

  const setSelectedPlaybackPosition = useCallback((positionSec: number) => {
    const normalizedPosition = Number(positionSec);
    if (!Number.isFinite(normalizedPosition) || normalizedPosition < 0) {
      return;
    }

    setSelectedBookDetail((current) => {
      if (!current) {
        return current;
      }
      return {
        ...current,
        playback_position_sec: normalizedPosition,
      };
    });
  }, []);

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
      void loadLibrary(searchQuery.trim());
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
    };
  }, [backendDisconnected, beginListRequest, beginOwnedRequest, loadLibrary, repository, resetLibraryState, searchQuery]);

  const summaryValues = useMemo(() => buildBookSummary(selectedBookDetail), [selectedBookDetail]);
  const selectedBookPlayable = Boolean(selectedBookDetail?.playback_url);

  return {
    searchQuery,
    setSearchQuery,
    modeFilter,
    setModeFilter,
    items,
    indexedCount,
    hasMore,
    selectedBookId,
    selectedBookIndex,
    selectedBookDetail,
    selectedBookPlayable,
    loadingList,
    loadingMore,
    loadingDetail,
    errorMessage,
    statusMessage,
    bookSummaryTitle: summaryValues.title,
    bookSummaryMeta: summaryValues.meta,
    bookDetailText: summaryValues.raw,
    refreshLibrary,
    reloadCurrentQuery,
    loadMore,
    selectBook,
    selectRelative,
    updateSelectedBookMetadata,
    loadCleaningPreview,
    repairSelectedBook,
    openSelectedBookFolder,
    deleteSelectedBook,
    setSelectedPlaybackPosition,
    getReloadContextGeneration,
  };
}

// Re-exported for the library list rows; historically lived on useLibrary.
export function formatLibraryMeta(item: LibraryBookSummary): string {
  return formatSummaryMeta(item);
}
