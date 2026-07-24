import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import { isAbortLikeError, useLatestRequest } from "../conversion/useLatestRequest";
import type {
  CleaningPreviewResponse,
  LibraryBookDetail,
  LibraryBookSummary,
} from "../../types/api";
import { buildBookSummary, formatSummaryMeta } from "./libraryDisplayModel";
import { createLibraryRepository, type LibraryModeFilter } from "./libraryRepository";

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
  selectedBookId: number | null;
  selectedBookIndex: number;
  selectedBookDetail: LibraryBookDetail | null;
  selectedBookPlayable: boolean;
  loadingList: boolean;
  loadingDetail: boolean;
  errorMessage: string;
  statusMessage: string;
  bookSummaryTitle: string;
  bookSummaryMeta: string;
  bookSummaryExtra: string;
  bookDetailText: string;
  refreshLibrary: () => Promise<void>;
  reloadCurrentQuery: () => Promise<void>;
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
  const [selectedBookId, setSelectedBookId] = useState<number | null>(null);
  const [selectedBookIndex, setSelectedBookIndex] = useState(-1);
  const [selectedBookDetail, setSelectedBookDetail] = useState<LibraryBookDetail | null>(null);

  const [loadingList, setLoadingList] = useState(false);
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
  const detailAbortRef = useRef<AbortController | null>(null);
  const selectedBookIdRef = useRef<number | null>(null);
  const selectionGenerationRef = useRef(0);
  const preservedStatusRef = useRef<string | null>(null);
  const currentListContextRef = useRef<{ query: string; modeFilter: LibraryModeFilter }>({
    query: "",
    modeFilter: "all",
  });
  currentListContextRef.current = { query: searchQuery.trim(), modeFilter };

  const resetLibraryState = useCallback(() => {
    listAbortRef.current?.abort();
    listAbortRef.current = null;
    detailAbortRef.current?.abort();
    detailAbortRef.current = null;
    beginListRequest();
    beginDetailRequest();
    beginMutationRequest();
    beginPreviewRequest();
    beginEntityActionRequest();
    preservedStatusRef.current = null;
    setItems([]);
    setIndexedCount(0);
    selectedBookIdRef.current = null;
    selectionGenerationRef.current += 1;
    setSelectedBookId(null);
    setSelectedBookIndex(-1);
    setSelectedBookDetail(null);
    setLoadingList(false);
    setLoadingDetail(false);
    setErrorMessage("");
    setStatusMessage(backendDisconnected ? "Backend disconnected" : "Waiting for backend");
  }, [backendDisconnected, beginDetailRequest, beginEntityActionRequest, beginListRequest, beginMutationRequest, beginPreviewRequest]);

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
      reloadContextGenerationRef.current += 1;
      const isCurrent = beginOwnedRequest(beginListRequest);
      listAbortRef.current?.abort();
      const controller = new AbortController();
      listAbortRef.current = controller;
      setLoadingList(true);
      if (!options.preserveMessages) {
        setErrorMessage("");
      }

      try {
        const response = await repository.listBooks(query, requestModeFilter, { signal: controller.signal });
        if (!isCurrent()) {
          return;
        }

        const nextItems = Array.isArray(response.items) ? response.items : [];
        const nextIndexedCount = query || requestModeFilter !== "all"
          ? await repository.countBooks({ signal: controller.signal })
          : response.count;
        if (!isCurrent()) {
          return;
        }

        setItems(nextItems);
        setIndexedCount(nextIndexedCount);

        if (nextItems.length === 0) {
          detailAbortRef.current?.abort();
          detailAbortRef.current = null;
          beginDetailRequest();
          selectedBookIdRef.current = null;
          selectionGenerationRef.current += 1;
          beginPreviewRequest();
          setSelectedBookId(null);
          setSelectedBookIndex(-1);
          setSelectedBookDetail(null);
          setLoadingDetail(false);
          if (!options.preserveMessages) {
            setStatusMessage("Library empty");
          }
          return;
        }

        let nextIndex = nextItems.findIndex((item) => item.id === selectedBookIdRef.current);
        if (nextIndex < 0) {
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

  const selectRelative = useCallback(
    async (delta: number): Promise<LibraryBookDetail | null> => {
      if (!items.length || delta === 0) {
        return null;
      }

      const baseIndex = selectedBookIndex >= 0 ? selectedBookIndex : 0;
      const nextIndex = baseIndex + delta;
      if (nextIndex < 0 || nextIndex >= items.length) {
        return null;
      }

      return selectBook(nextIndex);
    },
    [items, selectBook, selectedBookIndex],
  );

  const refreshLibrary = useCallback(async (): Promise<void> => {
    preservedStatusRef.current = null;
    await loadLibrary(searchQuery.trim());
  }, [loadLibrary, searchQuery]);

  const reloadCurrentQuery = useCallback(async (): Promise<void> => {
    await loadLibrary(searchQuery.trim());
  }, [loadLibrary, searchQuery]);

  const openSelectedBookFolder = useCallback(async (): Promise<void> => {
    if (!repository || !selectedBookId) {
      setStatusMessage("No book selected");
      return;
    }

    const targetBookId = selectedBookId;
    const targetSelectionGeneration = selectionGenerationRef.current;
    const isCurrentAction = beginOwnedRequest(beginEntityActionRequest);
    setErrorMessage("");

    try {
      const response = await repository.openBookFolder(targetBookId);
      if (
        !isCurrentAction() ||
        selectedBookIdRef.current !== targetBookId ||
        selectionGenerationRef.current !== targetSelectionGeneration
      ) {
        return;
      }
      const pathText = response.folder_path?.trim() ? `opened folder: ${response.folder_path}` : "opened folder";
      setStatusMessage(pathText);
    } catch (error: unknown) {
      if (
        !isCurrentAction() ||
        selectedBookIdRef.current !== targetBookId ||
        selectionGenerationRef.current !== targetSelectionGeneration
      ) {
        return;
      }
      const message = error instanceof Error ? error.message : "failed to open book folder";
      setErrorMessage(message);
      setStatusMessage(message);
    }
  }, [beginEntityActionRequest, beginOwnedRequest, repository, selectedBookId]);

  const deleteSelectedBook = useCallback(async (): Promise<boolean> => {
    if (!repository || !selectedBookId) {
      setStatusMessage("No book selected");
      return false;
    }

    const title = selectedBookDetail?.title || items.find((item) => item.id === selectedBookId)?.title || "selected book";
    const deletedStatus = `Deleted ${title}`;
    const selectionGenerationAtDeleteStart = selectionGenerationRef.current;
    const queryContextGenerationAtDeleteStart = queryContextGenerationRef.current;
    const isDeleteQueryCurrent = () =>
      queryContextGenerationRef.current === queryContextGenerationAtDeleteStart;
    const isCurrentMutation = beginApiOwnedRequest(beginMutationRequest);
    const isCurrentList = beginApiOwnedRequest(beginListRequest);
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
      await repository.deleteBook(selectedBookId);
      if (!isCurrentMutation()) {
        return true;
      }

      const selectionStillDeleteOwned = () =>
        selectedBookIdRef.current === optimisticSelectedBookId &&
        selectionGenerationRef.current === optimisticSelectionGeneration;
      const currentListContext = currentListContextRef.current;
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
        const currentListContext = currentListContextRef.current;
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
    }
  }, [beginApiOwnedRequest, beginDetailRequest, beginListRequest, beginMutationRequest, beginPreviewRequest, indexedCount, items, loadLibrary, repository, selectedBookDetail, selectedBookId, selectedBookIndex]);

  const updateSelectedBookMetadata = useCallback(
    async (payload: { title: string; author: string; tags: string }): Promise<void> => {
      if (!repository || !selectedBookId) {
        setStatusMessage("No book selected");
        return;
      }

      const targetBookId = selectedBookId;
      const targetSelectionGeneration = selectionGenerationRef.current;
      const isCurrentMutation = beginOwnedRequest(beginMutationRequest);
      setErrorMessage("");

      try {
        const updated = await repository.updateBook(targetBookId, {
          title: payload.title.trim() || null,
          author: payload.author.trim() || null,
          tags: payload.tags.trim(),
        });
        if (
          !isCurrentMutation() ||
          selectedBookIdRef.current !== targetBookId ||
          selectionGenerationRef.current !== targetSelectionGeneration
        ) {
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
        if (
          !isCurrentMutation() ||
          selectedBookIdRef.current !== targetBookId ||
          selectionGenerationRef.current !== targetSelectionGeneration
        ) {
          return;
        }
        const message = error instanceof Error ? error.message : "failed to update book";
        setErrorMessage(message);
        setStatusMessage(message);
      }
    },
    [beginMutationRequest, beginOwnedRequest, repository, selectedBookId],
  );

  const loadCleaningPreview = useCallback(
    async (chapterNumber: number): Promise<CleaningPreviewResponse | null> => {
      if (!repository || !selectedBookId) {
        setStatusMessage("No book selected");
        return null;
      }

      const targetBookId = selectedBookId;
      const targetSelectionGeneration = selectionGenerationRef.current;
      const isCurrentPreview = beginOwnedRequest(beginPreviewRequest);
      setErrorMessage("");
      try {
        const preview = await repository.getCleaningPreview(targetBookId, chapterNumber);
        if (
          !isCurrentPreview() ||
          selectedBookIdRef.current !== targetBookId ||
          selectionGenerationRef.current !== targetSelectionGeneration
        ) {
          return null;
        }
        setStatusMessage(preview.available ? `Loaded cleaning preview for chapter ${chapterNumber}` : "Cleaning preview unavailable");
        return preview;
      } catch (error: unknown) {
        if (
          !isCurrentPreview() ||
          selectedBookIdRef.current !== targetBookId ||
          selectionGenerationRef.current !== targetSelectionGeneration
        ) {
          return null;
        }
        const message = error instanceof Error ? error.message : "failed to load cleaning preview";
        setErrorMessage(message);
        setStatusMessage(message);
        return null;
      }
    },
    [beginOwnedRequest, beginPreviewRequest, repository, selectedBookId],
  );

  const repairSelectedBook = useCallback(async (): Promise<void> => {
    if (!repository || !selectedBookId) {
      setStatusMessage("No book selected");
      return;
    }

    const targetBookId = selectedBookId;
    const targetSelectionGeneration = selectionGenerationRef.current;
    const isCurrentAction = beginOwnedRequest(beginEntityActionRequest);
    setErrorMessage("");
    try {
      const response = await repository.repairBook(targetBookId);
      if (
        !isCurrentAction() ||
        selectedBookIdRef.current !== targetBookId ||
        selectionGenerationRef.current !== targetSelectionGeneration
      ) {
        return;
      }
      setStatusMessage(`Repair started: ${response.job_id}`);
    } catch (error: unknown) {
      if (
        !isCurrentAction() ||
        selectedBookIdRef.current !== targetBookId ||
        selectionGenerationRef.current !== targetSelectionGeneration
      ) {
        return;
      }
      const message = error instanceof Error ? error.message : "failed to start repair";
      setErrorMessage(message);
      setStatusMessage(message);
    }
  }, [beginEntityActionRequest, beginOwnedRequest, repository, selectedBookId]);

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
    if (!repository || backendDisconnected) {
      resetLibraryState();
      return;
    }

    const timer = window.setTimeout(() => {
      void loadLibrary(searchQuery.trim());
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
    };
  }, [backendDisconnected, loadLibrary, repository, resetLibraryState, searchQuery]);

  const summaryValues = useMemo(() => buildBookSummary(selectedBookDetail), [selectedBookDetail]);
  const selectedBookPlayable = Boolean(selectedBookDetail?.playback_url);

  return {
    searchQuery,
    setSearchQuery,
    modeFilter,
    setModeFilter,
    items,
    indexedCount,
    selectedBookId,
    selectedBookIndex,
    selectedBookDetail,
    selectedBookPlayable,
    loadingList,
    loadingDetail,
    errorMessage,
    statusMessage,
    bookSummaryTitle: summaryValues.title,
    bookSummaryMeta: summaryValues.meta,
    bookSummaryExtra: summaryValues.extra,
    bookDetailText: summaryValues.raw,
    refreshLibrary,
    reloadCurrentQuery,
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
