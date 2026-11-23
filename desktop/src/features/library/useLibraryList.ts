import { useMemo } from "react";

import type { LibrarySession } from "./librarySession";
import type { ApiClient } from "../../api/client";
import type { CleaningPreviewResponse, LibraryBookDetail, LibraryBookSummary } from "../../types/api";
import type { LibraryModeFilter } from "./libraryRepository";
import { useLibraryQueries } from "./useLibraryQueries";
import { useLibraryMutations } from "./useLibraryMutations";

interface UseLibraryListArgs {
  apiClient: ApiClient | null;
  backendDisconnected: boolean;
  session: LibrarySession;
}

interface UseLibraryListResult {
  captureNavigation: () => () => boolean;
  searchQuery: string;
  setSearchQuery: (value: string) => void;
  modeFilter: LibraryModeFilter;
  setModeFilter: (value: LibraryModeFilter) => void;
  items: LibraryBookSummary[];
  unfilteredItems: LibraryBookSummary[];
  indexedCount: number;
  hasMore: boolean;
  selectedBookId: number | null;
  selectedBookIndex: number;
  selectedBookDetail: LibraryBookDetail | null;
  selectedBookPlayable: boolean;
  loadingList: boolean;
  completedListLoads: number;
  loadingMore: boolean;
  loadingDetail: boolean;
  savingMetadata: boolean;
  errorMessage: string;
  statusMessage: string;
  setStatusMessage: (message: string) => void;
  pendingDeletionPath: string | null;
  dismissPendingDeletion: () => void;
  openPendingDeletionFolder: () => Promise<void>;
  bookDetailText: string;
  refreshLibrary: () => Promise<void>;
  reloadCurrentQuery: () => Promise<void>;
  reloadLibrarySession: () => Promise<void>;
  loadMore: () => Promise<void>;
  selectBook: (index: number) => Promise<LibraryBookDetail | null>;
  selectBookById: (bookId: number) => Promise<LibraryBookDetail | null>;
  selectRelative: (delta: number, fromBookId?: number | null) => Promise<LibraryBookDetail | null>;
  updateSelectedBookMetadata: (payload: { title: string; author: string; tags: string }) => Promise<void>;
  loadCleaningPreview: (chapterNumber: number) => Promise<CleaningPreviewResponse | null>;
  repairSelectedBook: (releaseBookAudio?: (bookId: number) => void) => Promise<void>;
  openSelectedBookFolder: () => Promise<void>;
  deleteSelectedBook: (releaseBookAudio?: (bookId: number) => void) => Promise<boolean>;
  setSelectedPlaybackPosition: (positionSec: number) => void;
  getReloadContextGeneration: () => number;
}

export function useLibraryList({ apiClient, backendDisconnected, session }: UseLibraryListArgs): UseLibraryListResult {
  const queries = useLibraryQueries({ apiClient, backendDisconnected, session });
  const {
    repository,
    searchQuery,
    setSearchQuery,
    modeFilter,
    setModeFilter,
    items,
    unfilteredItems,
    indexedCount,
    hasMore,
    selectedBookId,
    selectedBookIndex,
    selectedBookDetail,
    loadingList,
    completedListLoads,
    loadingMore,
    loadingDetail,
    errorMessage,
    statusMessage,
    pendingDeletionPath,
    dismissPendingDeletion,
    getReloadContextGeneration,
    selectBook,
    selectBookById,
    loadMore,
    selectRelative,
    refreshLibrary,
    reloadCurrentQuery,
    reloadLibrarySession,
    loadCleaningPreview,
  } = queries;
  const {
    metadataSaves,
    openPendingDeletionFolder,
    openSelectedBookFolder,
    deleteSelectedBook,
    updateSelectedBookMetadata,
    repairSelectedBook,
    setSelectedPlaybackPosition,
  } = useLibraryMutations(apiClient, queries);

  const bookDetailText = useMemo(
    () => (selectedBookDetail ? JSON.stringify(selectedBookDetail, null, 2) : "Select a book to inspect details."),
    [selectedBookDetail],
  );
  // A book whose audio is known to be missing is not offered for playback.
  const selectedBookPlayable = selectedBookDetail?.id === selectedBookId && Boolean(selectedBookDetail?.playback_url)
    && selectedBookDetail?.audio_available !== false;

  return {
    captureNavigation: queries.captureNavigation,
    searchQuery,
    setSearchQuery,
    modeFilter,
    setModeFilter,
    items,
    unfilteredItems,
    indexedCount,
    hasMore,
    selectedBookId,
    selectedBookIndex,
    selectedBookDetail,
    selectedBookPlayable,
    loadingList,
    completedListLoads,
    loadingMore,
    loadingDetail,
    savingMetadata: selectedBookId !== null && metadataSaves.get(selectedBookId)?.repository === repository,
    errorMessage,
    statusMessage,
    setStatusMessage: queries.setStatusMessage,
    pendingDeletionPath,
    dismissPendingDeletion,
    openPendingDeletionFolder,
    bookDetailText,
    refreshLibrary,
    reloadCurrentQuery,
    reloadLibrarySession,
    loadMore,
    selectBook,
    selectBookById,
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
