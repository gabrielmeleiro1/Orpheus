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
}

export function useLibraryList({ apiClient, backendDisconnected }: UseLibraryListArgs): UseLibraryListResult {
  const repository = useMemo(() => (apiClient ? createLibraryRepository(apiClient) : null), [apiClient]);

  const [searchQuery, setSearchQuery] = useState("");
  const [modeFilter, setModeFilter] = useState<LibraryModeFilter>("all");
  const [items, setItems] = useState<LibraryBookSummary[]>([]);
  const [selectedBookId, setSelectedBookId] = useState<number | null>(null);
  const [selectedBookIndex, setSelectedBookIndex] = useState(-1);
  const [selectedBookDetail, setSelectedBookDetail] = useState<LibraryBookDetail | null>(null);

  const [loadingList, setLoadingList] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [statusMessage, setStatusMessage] = useState("library idle");

  const beginListRequest = useLatestRequest();
  const beginDetailRequest = useLatestRequest();
  const listAbortRef = useRef<AbortController | null>(null);
  const detailAbortRef = useRef<AbortController | null>(null);
  const selectedBookIdRef = useRef<number | null>(null);
  const preservedStatusRef = useRef<string | null>(null);

  const loadBookDetail = useCallback(
    async (bookId: number, options: { silentStatus?: boolean } = {}): Promise<LibraryBookDetail | null> => {
      if (!repository) {
        setSelectedBookDetail(null);
        return null;
      }

      const isCurrent = beginDetailRequest();
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
          setStatusMessage(`selected: ${detail.title || "untitled"}`);
        }
        return detail;
      } catch (error: unknown) {
        if (!isCurrent() || isAbortLikeError(error)) {
          return null;
        }

        const message = error instanceof Error ? error.message : "failed to load book detail";
        setSelectedBookDetail(null);
        setErrorMessage(message);
        setStatusMessage(message);
        return null;
      } finally {
        if (isCurrent()) {
          setLoadingDetail(false);
        }
      }
    },
    [beginDetailRequest, repository],
  );

  const loadLibrary = useCallback(
    async (query: string, options: { silentDetailStatus?: boolean } = {}): Promise<void> => {
      if (!repository) {
        setItems([]);
        selectedBookIdRef.current = null;
        setSelectedBookId(null);
        setSelectedBookIndex(-1);
        setSelectedBookDetail(null);
        setLoadingList(false);
        setLoadingDetail(false);
        setErrorMessage("");
        setStatusMessage(backendDisconnected ? "backend disconnected" : "waiting for backend");
        return;
      }

      const isCurrent = beginListRequest();
      listAbortRef.current?.abort();
      const controller = new AbortController();
      listAbortRef.current = controller;
      setLoadingList(true);
      setErrorMessage("");

      try {
        const response = await repository.listBooks(query, modeFilter, { signal: controller.signal });
        if (!isCurrent()) {
          return;
        }

        const nextItems = Array.isArray(response.items) ? response.items : [];
        setItems(nextItems);

        if (nextItems.length === 0) {
          selectedBookIdRef.current = null;
          setSelectedBookId(null);
          setSelectedBookIndex(-1);
          setSelectedBookDetail(null);
          setStatusMessage("library empty");
          return;
        }

        let nextIndex = nextItems.findIndex((item) => item.id === selectedBookIdRef.current);
        if (nextIndex < 0) {
          nextIndex = 0;
        }

        const nextBook = nextItems[nextIndex];
        setSelectedBookIndex(nextIndex);
        selectedBookIdRef.current = nextBook.id;
        setSelectedBookId(nextBook.id);
        await loadBookDetail(nextBook.id, { silentStatus: options.silentDetailStatus });
      } catch (error: unknown) {
        if (!isCurrent() || isAbortLikeError(error)) {
          return;
        }

        const message = error instanceof Error ? error.message : "failed to load library";
        setErrorMessage(message);
        setStatusMessage(message);
      } finally {
        if (isCurrent()) {
          setLoadingList(false);
        }
      }
    },
    [backendDisconnected, beginListRequest, loadBookDetail, modeFilter, repository],
  );

  const selectBook = useCallback(
    async (index: number): Promise<LibraryBookDetail | null> => {
      if (index < 0 || index >= items.length) {
        return null;
      }

      const nextBook = items[index];
      preservedStatusRef.current = null;
      setSelectedBookIndex(index);
      selectedBookIdRef.current = nextBook.id;
      setSelectedBookId(nextBook.id);
      setSelectedBookDetail(null);
      setErrorMessage("");
      return loadBookDetail(nextBook.id);
    },
    [items, loadBookDetail],
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
      setStatusMessage("no selected book");
      return;
    }

    setErrorMessage("");

    try {
      const response = await repository.openBookFolder(selectedBookId);
      const pathText = response.folder_path?.trim() ? `opened folder: ${response.folder_path}` : "opened folder";
      setStatusMessage(pathText);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "failed to open book folder";
      setErrorMessage(message);
      setStatusMessage(message);
    }
  }, [repository, selectedBookId]);

  const deleteSelectedBook = useCallback(async (): Promise<boolean> => {
    if (!repository || !selectedBookId) {
      setStatusMessage("no selected book");
      return false;
    }

    const title = selectedBookDetail?.title || items.find((item) => item.id === selectedBookId)?.title || "selected book";
    if (
      typeof window !== "undefined" &&
      typeof window.confirm === "function" &&
      !window.confirm(`Delete ${title}? This removes its generated audio and cannot be undone.`)
    ) {
      return false;
    }
    const deletedStatus = `deleted ${title}`;
    const previousItems = items;
    const previousSelectedBookId = selectedBookId;
    const previousSelectedBookIndex = selectedBookIndex;
    const previousSelectedBookDetail = selectedBookDetail;
    const deleteIndex = items.findIndex((item) => item.id === selectedBookId);
    const optimisticItems = items.filter((item) => item.id !== selectedBookId);
    const nextIndex = optimisticItems.length > 0 ? Math.min(Math.max(deleteIndex, 0), optimisticItems.length - 1) : -1;
    const nextBook = nextIndex >= 0 ? optimisticItems[nextIndex] : null;

    setErrorMessage("");
    preservedStatusRef.current = null;
    setStatusMessage(`deleting ${title}`);
    setItems(optimisticItems);
    setSelectedBookIndex(nextIndex);
    selectedBookIdRef.current = nextBook?.id ?? null;
    setSelectedBookId(nextBook?.id ?? null);
    setSelectedBookDetail(null);

    try {
      await repository.deleteBook(selectedBookId);
      preservedStatusRef.current = deletedStatus;
      setStatusMessage(deletedStatus);
      await loadLibrary(searchQuery.trim(), { silentDetailStatus: true });
      setStatusMessage(deletedStatus);
      return true;
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "failed to delete book";
      setItems(previousItems);
      setSelectedBookIndex(previousSelectedBookIndex);
      selectedBookIdRef.current = previousSelectedBookId;
      setSelectedBookId(previousSelectedBookId);
      setSelectedBookDetail(previousSelectedBookDetail);
      setErrorMessage(message);
      setStatusMessage(message);
      return false;
    }
  }, [items, loadLibrary, repository, searchQuery, selectedBookDetail, selectedBookId, selectedBookIndex]);

  const updateSelectedBookMetadata = useCallback(
    async (payload: { title: string; author: string; tags: string }): Promise<void> => {
      if (!repository || !selectedBookId) {
        setStatusMessage("no selected book");
        return;
      }

      setErrorMessage("");

      try {
        const updated = await repository.updateBook(selectedBookId, {
          title: payload.title.trim() || null,
          author: payload.author.trim() || null,
          tags: payload.tags.trim(),
        });
        setSelectedBookDetail((current) => ({
          ...(current ?? updated),
          ...updated,
          chapters: current?.chapters ?? updated.chapters ?? [],
          playback_url: current?.playback_url ?? updated.playback_url ?? null,
        }));
        setItems((current) =>
          current.map((item) =>
            item.id === selectedBookId
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
        setStatusMessage("metadata saved");
      } catch (error: unknown) {
        const message = error instanceof Error ? error.message : "failed to update book";
        setErrorMessage(message);
        setStatusMessage(message);
      }
    },
    [repository, selectedBookId],
  );

  const loadCleaningPreview = useCallback(
    async (chapterNumber: number): Promise<CleaningPreviewResponse | null> => {
      if (!repository || !selectedBookId) {
        setStatusMessage("no selected book");
        return null;
      }

      setErrorMessage("");
      try {
        const preview = await repository.getCleaningPreview(selectedBookId, chapterNumber);
        setStatusMessage(preview.available ? `loaded cleaning preview for chapter ${chapterNumber}` : "cleaning preview unavailable");
        return preview;
      } catch (error: unknown) {
        const message = error instanceof Error ? error.message : "failed to load cleaning preview";
        setErrorMessage(message);
        setStatusMessage(message);
        return null;
      }
    },
    [repository, selectedBookId],
  );

  const repairSelectedBook = useCallback(async (): Promise<void> => {
    if (!repository || !selectedBookId) {
      setStatusMessage("no selected book");
      return;
    }

    setErrorMessage("");
    try {
      const response = await repository.repairBook(selectedBookId);
      setStatusMessage(`repair started: ${response.job_id}`);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "failed to start repair";
      setErrorMessage(message);
      setStatusMessage(message);
    }
  }, [repository, selectedBookId]);

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
    if (!repository) {
      setItems([]);
      selectedBookIdRef.current = null;
      setSelectedBookId(null);
      setSelectedBookIndex(-1);
      setSelectedBookDetail(null);
      setLoadingList(false);
      setLoadingDetail(false);
      setErrorMessage("");
      setStatusMessage(backendDisconnected ? "backend disconnected" : "waiting for backend");
      return;
    }

    const timer = window.setTimeout(() => {
      void loadLibrary(searchQuery.trim());
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
    };
  }, [backendDisconnected, loadLibrary, repository, searchQuery]);

  const summaryValues = useMemo(() => buildBookSummary(selectedBookDetail), [selectedBookDetail]);
  const selectedBookPlayable = Boolean(selectedBookDetail?.playback_url);

  return {
    searchQuery,
    setSearchQuery,
    modeFilter,
    setModeFilter,
    items,
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
  };
}

// Re-exported for the library list rows; historically lived on useLibrary.
export function formatLibraryMeta(item: LibraryBookSummary): string {
  return formatSummaryMeta(item);
}
