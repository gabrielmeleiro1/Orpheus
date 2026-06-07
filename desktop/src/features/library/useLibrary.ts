import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type {
  CleaningPreviewResponse,
  ConversionMode,
  LibraryBookDetail,
  LibraryBookSummary,
  MaintenanceDiagnosticsResponse,
} from "../../types/api";

const SEARCH_DEBOUNCE_MS = 220;
const DEFAULT_REPAIR_PARALLEL_WORKER_LIMIT = 4;

interface UseLibraryArgs {
  apiClient: ApiClient | null;
  backendDisconnected?: boolean;
}

export interface UseLibraryResult {
  searchQuery: string;
  setSearchQuery: (value: string) => void;
  modeFilter: "all" | ConversionMode;
  setModeFilter: (value: "all" | ConversionMode) => void;
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
  maintenanceReport: MaintenanceDiagnosticsResponse | null;
  maintenanceLoading: boolean;
  maintenanceMessage: string;
  refreshLibrary: () => Promise<void>;
  selectBook: (index: number) => Promise<LibraryBookDetail | null>;
  selectRelative: (delta: number) => Promise<LibraryBookDetail | null>;
  updateSelectedBookMetadata: (payload: { title: string; author: string; tags: string }) => Promise<void>;
  loadCleaningPreview: (chapterNumber: number) => Promise<CleaningPreviewResponse | null>;
  repairSelectedBook: () => Promise<void>;
  loadMaintenanceDiagnostics: () => Promise<void>;
  backupDatabase: () => Promise<void>;
  cleanupStorage: () => Promise<void>;
  openSelectedBookFolder: () => Promise<void>;
  deleteSelectedBook: () => Promise<boolean>;
  setSelectedPlaybackPosition: (positionSec: number) => void;
}

function formatTimeValue(seconds: number): string {
  const whole = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(whole / 60);
  const remainder = whole % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function normalizeSummaryMeta(item: LibraryBookSummary): string {
  const mode = item.conversion_mode === "document_audio" ? "document" : "audiobook";
  const source = item.source_available ? "" : " · source missing";
  return `${item.author || "unknown author"} · ${mode} · ${item.completed_chapters || 0}/${item.total_chapters || 0}${source}`;
}

export function useLibrary({ apiClient, backendDisconnected = false }: UseLibraryArgs): UseLibraryResult {
  const [searchQuery, setSearchQuery] = useState("");
  const [modeFilter, setModeFilter] = useState<"all" | ConversionMode>("all");
  const [items, setItems] = useState<LibraryBookSummary[]>([]);
  const [selectedBookId, setSelectedBookId] = useState<number | null>(null);
  const [selectedBookIndex, setSelectedBookIndex] = useState(-1);
  const [selectedBookDetail, setSelectedBookDetail] = useState<LibraryBookDetail | null>(null);

  const [loadingList, setLoadingList] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [statusMessage, setStatusMessage] = useState("library idle");
  const [maintenanceReport, setMaintenanceReport] = useState<MaintenanceDiagnosticsResponse | null>(null);
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);
  const [maintenanceMessage, setMaintenanceMessage] = useState("maintenance idle");

  const listRequestSeq = useRef(0);
  const detailRequestSeq = useRef(0);
  const selectedBookIdRef = useRef<number | null>(null);
  const preservedStatusRef = useRef<string | null>(null);

  const loadBookDetail = useCallback(
    async (bookId: number, options: { silentStatus?: boolean } = {}): Promise<LibraryBookDetail | null> => {
      if (!apiClient) {
        setSelectedBookDetail(null);
        return null;
      }

      const requestSeq = ++detailRequestSeq.current;
      setLoadingDetail(true);

      try {
        const detail = await apiClient.getLibraryBook(bookId);
        if (detailRequestSeq.current !== requestSeq) {
          return null;
        }

        setSelectedBookDetail(detail);
        if (!options.silentStatus && preservedStatusRef.current === null) {
          setStatusMessage(`selected: ${detail.title || "untitled"}`);
        }
        return detail;
      } catch (error: unknown) {
        if (detailRequestSeq.current !== requestSeq) {
          return null;
        }

        const message = error instanceof Error ? error.message : "failed to load book detail";
        setSelectedBookDetail(null);
        setErrorMessage(message);
        setStatusMessage(message);
        return null;
      } finally {
        if (detailRequestSeq.current === requestSeq) {
          setLoadingDetail(false);
        }
      }
    },
    [apiClient],
  );

  const loadLibrary = useCallback(
    async (query: string, options: { silentDetailStatus?: boolean } = {}): Promise<void> => {
      if (!apiClient) {
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

      const requestSeq = ++listRequestSeq.current;
      setLoadingList(true);
      setErrorMessage("");

      try {
        const response = await apiClient.getLibrary(query || undefined, undefined, {
          conversionMode: modeFilter === "all" ? "" : modeFilter,
        });
        if (listRequestSeq.current !== requestSeq) {
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
        if (listRequestSeq.current !== requestSeq) {
          return;
        }

        const message = error instanceof Error ? error.message : "failed to load library";
        setErrorMessage(message);
        setStatusMessage(message);
      } finally {
        if (listRequestSeq.current === requestSeq) {
          setLoadingList(false);
        }
      }
    },
    [apiClient, backendDisconnected, loadBookDetail, modeFilter],
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

  const openSelectedBookFolder = useCallback(async (): Promise<void> => {
    if (!apiClient || !selectedBookId) {
      setStatusMessage("no selected book");
      return;
    }

    setErrorMessage("");

    try {
      const response = await apiClient.openBookFolder(selectedBookId);
      const pathText = response.folder_path?.trim() ? `opened folder: ${response.folder_path}` : "opened folder";
      setStatusMessage(pathText);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "failed to open book folder";
      setErrorMessage(message);
      setStatusMessage(message);
    }
  }, [apiClient, selectedBookId]);

  const deleteSelectedBook = useCallback(async (): Promise<boolean> => {
    if (!apiClient || !selectedBookId) {
      setStatusMessage("no selected book");
      return false;
    }

    const title = selectedBookDetail?.title || items.find((item) => item.id === selectedBookId)?.title || "selected book";
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
      await apiClient.deleteBook(selectedBookId);
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
  }, [apiClient, items, loadLibrary, searchQuery, selectedBookDetail, selectedBookId, selectedBookIndex]);

  const updateSelectedBookMetadata = useCallback(
    async (payload: { title: string; author: string; tags: string }): Promise<void> => {
      if (!apiClient || !selectedBookId) {
        setStatusMessage("no selected book");
        return;
      }

      setErrorMessage("");

      try {
        const updated = await apiClient.updateLibraryBook(selectedBookId, {
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
    [apiClient, selectedBookId],
  );

  const loadCleaningPreview = useCallback(
    async (chapterNumber: number): Promise<CleaningPreviewResponse | null> => {
      if (!apiClient || !selectedBookId) {
        setStatusMessage("no selected book");
        return null;
      }

      setErrorMessage("");
      try {
        const preview = await apiClient.getCleaningPreview(selectedBookId, chapterNumber);
        setStatusMessage(preview.available ? `loaded cleaning preview for chapter ${chapterNumber}` : "cleaning preview unavailable");
        return preview;
      } catch (error: unknown) {
        const message = error instanceof Error ? error.message : "failed to load cleaning preview";
        setErrorMessage(message);
        setStatusMessage(message);
        return null;
      }
    },
    [apiClient, selectedBookId],
  );

  const repairSelectedBook = useCallback(async (): Promise<void> => {
    if (!apiClient || !selectedBookId) {
      setStatusMessage("no selected book");
      return;
    }

    setErrorMessage("");
    try {
      const response = await apiClient.repairConversion(selectedBookId, {
        runtime_options: {
          voice: "am_adam",
          speed: 1,
          tts_engine: null,
          tts_quantization: null,
          cleaner_model: null,
          cleaner_backend: null,
          cleaner_api_model: null,
          tts_options: null,
          parallel_worker_mode: "manual",
          max_parallel_chapters: DEFAULT_REPAIR_PARALLEL_WORKER_LIMIT,
          enable_parallel: DEFAULT_REPAIR_PARALLEL_WORKER_LIMIT > 1,
        },
      });
      setStatusMessage(`repair started: ${response.job_id}`);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "failed to start repair";
      setErrorMessage(message);
      setStatusMessage(message);
    }
  }, [apiClient, selectedBookId]);

  const loadMaintenanceDiagnostics = useCallback(async (): Promise<void> => {
    if (!apiClient) {
      setMaintenanceMessage("backend disconnected");
      return;
    }

    setMaintenanceLoading(true);
    try {
      const report = await apiClient.getMaintenanceDiagnostics();
      setMaintenanceReport(report);
      setMaintenanceMessage("diagnostics loaded");
    } catch (error: unknown) {
      setMaintenanceMessage(error instanceof Error ? error.message : "failed to load diagnostics");
    } finally {
      setMaintenanceLoading(false);
    }
  }, [apiClient]);

  const backupDatabase = useCallback(async (): Promise<void> => {
    if (!apiClient) {
      setMaintenanceMessage("backend disconnected");
      return;
    }

    setMaintenanceLoading(true);
    try {
      const response = await apiClient.backupDatabase();
      setMaintenanceMessage(`backup created: ${response.backup_path}`);
      await loadMaintenanceDiagnostics();
    } catch (error: unknown) {
      setMaintenanceMessage(error instanceof Error ? error.message : "backup failed");
    } finally {
      setMaintenanceLoading(false);
    }
  }, [apiClient, loadMaintenanceDiagnostics]);

  const cleanupStorage = useCallback(async (): Promise<void> => {
    if (!apiClient) {
      setMaintenanceMessage("backend disconnected");
      return;
    }
    if (typeof window.confirm === "function" && !window.confirm("Clean stale storage records and orphan files? A database backup will be created first.")) {
      return;
    }

    setMaintenanceLoading(true);
    try {
      await apiClient.cleanupStorage();
      setMaintenanceMessage("cleanup complete");
      await loadMaintenanceDiagnostics();
      await loadLibrary(searchQuery.trim());
    } catch (error: unknown) {
      setMaintenanceMessage(error instanceof Error ? error.message : "cleanup failed");
    } finally {
      setMaintenanceLoading(false);
    }
  }, [apiClient, loadLibrary, loadMaintenanceDiagnostics, searchQuery]);

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
    if (!apiClient) {
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
  }, [apiClient, loadLibrary, searchQuery]);

  const summaryValues = useMemo(() => {
    if (!selectedBookDetail) {
      return {
        title: "No book selected",
        meta: "Select a book to inspect details.",
        extra: "—",
        raw: "Select a book to inspect details.",
      };
    }

    const chapterCount = Array.isArray(selectedBookDetail.chapters) ? selectedBookDetail.chapters.length : 0;
    const resumeTime = formatTimeValue(selectedBookDetail.playback_position_sec || 0);

    return {
      title: selectedBookDetail.title || "unknown",
      meta: `${selectedBookDetail.author || "unknown"} · ${chapterCount} ${chapterCount === 1 ? "chapter" : "chapters"}`,
      extra:
        selectedBookDetail.playback_path && selectedBookDetail.playback_url
          ? selectedBookDetail.playback_position_sec > 0
            ? `Playback ready · resume ${resumeTime}`
            : "Playback ready"
          : "Playback output pending",
      raw: JSON.stringify(selectedBookDetail, null, 2),
    };
  }, [selectedBookDetail]);

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
    maintenanceReport,
    maintenanceLoading,
    maintenanceMessage,
    refreshLibrary,
    selectBook,
    selectRelative,
    updateSelectedBookMetadata,
    loadCleaningPreview,
    repairSelectedBook,
    loadMaintenanceDiagnostics,
    backupDatabase,
    cleanupStorage,
    openSelectedBookFolder,
    deleteSelectedBook,
    setSelectedPlaybackPosition,
  };
}

export function formatLibraryMeta(item: LibraryBookSummary): string {
  return normalizeSummaryMeta(item);
}
