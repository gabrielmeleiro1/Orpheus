import { useCallback, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import type {
  CleaningPreviewResponse,
  ConversionMode,
  LibraryBookDetail,
  LibraryBookSummary,
  MaintenanceBackup,
  MaintenanceDiagnosticsResponse,
} from "../../types/api";
import type { ApiClient } from "../../api/client";
import { createLibrarySession, type LibrarySession } from "./librarySession";
import { useLibraryList } from "./useLibraryList";
import { useLibraryMaintenance } from "./useLibraryMaintenance";
import { WRITES_PAUSED_MESSAGE } from "./maintenanceModel";

interface UseLibraryArgs {
  apiClient: ApiClient | null;
  backendDisconnected?: boolean;
}

export interface UseLibraryResult {
  captureNavigation?: () => () => boolean;
  libraryGeneration?: number;
  session?: LibrarySession;
  searchQuery: string;
  setSearchQuery: (value: string) => void;
  modeFilter: "all" | ConversionMode;
  setModeFilter: (value: "all" | ConversionMode) => void;
  items: LibraryBookSummary[];
  /** The library's first page without the search or mode filter, for the sidebar. */
  unfilteredItems: LibraryBookSummary[];
  indexedCount: number;
  hasMore: boolean;
  selectedBookId: number | null;
  selectedBookIndex: number;
  selectedBookDetail: LibraryBookDetail | null;
  selectedBookPlayable: boolean;
  loadingList: boolean;
  loadingMore: boolean;
  loadingDetail: boolean;
  savingMetadata?: boolean;
  errorMessage: string;
  statusMessage: string;
  pendingDeletionPath?: string | null;
  dismissPendingDeletion?: () => void;
  openPendingDeletionFolder?: () => Promise<void>;
  bookDetailText: string;
  maintenanceReport: MaintenanceDiagnosticsResponse | null;
  maintenanceBackups?: MaintenanceBackup[];
  maintenanceLoading: boolean;
  maintenanceMessage: string;
  canOpenAppData: boolean;
  refreshLibrary: () => Promise<void>;
  reloadLibrarySession?: () => Promise<void>;
  loadMore: () => Promise<void>;
  selectBook: (index: number) => Promise<LibraryBookDetail | null>;
  /** Selects a book whether or not it is on a loaded page. */
  selectBookById: (bookId: number) => Promise<LibraryBookDetail | null>;
  /** Steps from `fromBookId` when given, else from the selected book. */
  selectRelative: (delta: number, fromBookId?: number | null) => Promise<LibraryBookDetail | null>;
  updateSelectedBookMetadata: (payload: { title: string; author: string; tags: string }) => Promise<void>;
  loadCleaningPreview: (chapterNumber: number) => Promise<CleaningPreviewResponse | null>;
  repairSelectedBook: (releaseBookAudio?: (bookId: number) => void) => Promise<void>;
  loadMaintenanceDiagnostics: () => Promise<void>;
  backupDatabase: () => Promise<void>;
  restoreBackup?: (id: string) => Promise<void>;
  /** Names folders no book owns to delete as well; without them cleanup keeps every such folder. */
  cleanupStorage: (removeUnownedFolders?: string[]) => Promise<void>;
  openSelectedBookFolder: () => Promise<void>;
  openAppDataFolder: () => Promise<void>;
  deleteSelectedBook: (releaseBookAudio?: (bookId: number) => void) => Promise<boolean>;
  setSelectedPlaybackPosition: (positionSec: number) => void;
  /** The backend is unreachable: items are the last shelf seen while connected. */
  offline?: boolean;
}

const OFFLINE_MESSAGE = "The backend is disconnected. Showing your library as it was until it reconnects.";

/**
 * Compatibility facade composing the decomposed library hooks. Its public
 * return shape matches the historical monolithic hook (plus additive
 * app-data-folder controls moved out of LibraryPanel), so existing consumers
 * keep working unchanged.
 */
export function useLibrary({ apiClient, backendDisconnected = false }: UseLibraryArgs): UseLibraryResult {
  const [session] = useState(createLibrarySession);
  const sessionClientRef = useRef(apiClient);
  // Keep restore suspension across reconnects and retire the previous client's callbacks.
  useLayoutEffect(() => {
    if (sessionClientRef.current === apiClient) return;
    sessionClientRef.current = apiClient;
    session.invalidate();
  }, [apiClient, session]);
  const getGeneration = useCallback(() => session.generation, [session]);
  const libraryGeneration = useSyncExternalStore(session.subscribe, getGeneration, getGeneration);
  const list = useLibraryList({ apiClient, backendDisconnected, session });
  const maintenance = useLibraryMaintenance({
    apiClient,
    backendDisconnected,
    session,
    onAfterCleanup: list.reloadCurrentQuery,
    getReloadContextGeneration: list.getReloadContextGeneration,
  });

  // The session holds writes back (returning null) while a restore is running or unverified.
  const runMutation = useCallback(async <T,>(operation: () => Promise<T>): Promise<T | null> => {
    const result = await session.runMutation(libraryGeneration, operation);
    if (result === null) list.setStatusMessage(WRITES_PAUSED_MESSAGE);
    return result;
  }, [session, libraryGeneration, list.setStatusMessage]);
  const updateSelectedBookMetadata = useCallback(async (payload: { title: string; author: string; tags: string }) => {
    await runMutation(() => list.updateSelectedBookMetadata(payload));
  }, [runMutation, list.updateSelectedBookMetadata]);
  const repairSelectedBook = useCallback(async (releaseBookAudio?: (bookId: number) => void) => {
    await runMutation(() => list.repairSelectedBook(releaseBookAudio));
  }, [runMutation, list.repairSelectedBook]);
  const deleteSelectedBook = useCallback(async (releaseBookAudio?: (bookId: number) => void) =>
    await runMutation(() => list.deleteSelectedBook(releaseBookAudio)) ?? false,
  [runMutation, list.deleteSelectedBook]);
  const setSelectedPlaybackPosition = useCallback((positionSec: number) => {
    if (session.isCurrent(libraryGeneration)) list.setSelectedPlaybackPosition(positionSec);
  }, [session, libraryGeneration, list.setSelectedPlaybackPosition]);

  // Losing the backend clears the live list; keep the last shelf on screen,
  // marked offline, until a reconnected backend has loaded the real one.
  const disconnected = !apiClient || backendDisconnected;
  const [offlineShelf, setOfflineShelf] = useState<{
    items: LibraryBookSummary[];
    unfilteredItems: LibraryBookSummary[];
    indexedCount: number;
  } | null>(null);
  const wasDisconnectedRef = useRef(disconnected);
  useLayoutEffect(() => {
    const dropped = disconnected && !wasDisconnectedRef.current;
    wasDisconnectedRef.current = disconnected;
    // These are this render's values, from before the list resets.
    if (dropped && list.unfilteredItems.length > 0) {
      setOfflineShelf({ items: list.items, unfilteredItems: list.unfilteredItems, indexedCount: list.indexedCount });
    }
  }, [disconnected, list.items, list.unfilteredItems, list.indexedCount]);
  const reconnectedListLoaded = !disconnected && !list.loadingList;
  useLayoutEffect(() => {
    if (reconnectedListLoaded) setOfflineShelf(null);
  }, [reconnectedListLoaded]);
  const shelf = offlineShelf && !reconnectedListLoaded ? offlineShelf : null;

  return useMemo(() => ({
    captureNavigation: list.captureNavigation,
    session,
    libraryGeneration,
    searchQuery: list.searchQuery,
    setSearchQuery: list.setSearchQuery,
    modeFilter: list.modeFilter,
    setModeFilter: list.setModeFilter,
    items: shelf ? shelf.items : list.items,
    unfilteredItems: shelf ? shelf.unfilteredItems : list.unfilteredItems,
    indexedCount: shelf ? shelf.indexedCount : list.indexedCount,
    hasMore: shelf ? false : list.hasMore,
    offline: Boolean(shelf) || disconnected,
    selectedBookId: list.selectedBookId,
    selectedBookIndex: list.selectedBookIndex,
    selectedBookDetail: list.selectedBookDetail,
    selectedBookPlayable: list.selectedBookPlayable,
    loadingList: list.loadingList,
    loadingMore: list.loadingMore,
    loadingDetail: list.loadingDetail,
    savingMetadata: list.savingMetadata,
    errorMessage: disconnected && (shelf || list.items.length === 0) ? OFFLINE_MESSAGE : list.errorMessage,
    statusMessage: list.statusMessage,
    pendingDeletionPath: list.pendingDeletionPath,
    dismissPendingDeletion: list.dismissPendingDeletion,
    openPendingDeletionFolder: list.openPendingDeletionFolder,
    bookDetailText: list.bookDetailText,
    maintenanceReport: maintenance.maintenanceReport,
    maintenanceBackups: maintenance.maintenanceBackups,
    maintenanceLoading: maintenance.maintenanceLoading,
    maintenanceMessage: maintenance.maintenanceMessage,
    canOpenAppData: maintenance.canOpenAppData,
    refreshLibrary: list.refreshLibrary,
    reloadLibrarySession: list.reloadLibrarySession,
    loadMore: list.loadMore,
    selectBook: list.selectBook,
    selectBookById: list.selectBookById,
    selectRelative: list.selectRelative,
    updateSelectedBookMetadata,
    loadCleaningPreview: list.loadCleaningPreview,
    repairSelectedBook,
    loadMaintenanceDiagnostics: maintenance.loadMaintenanceDiagnostics,
    backupDatabase: maintenance.backupDatabase,
    restoreBackup: maintenance.restoreBackup,
    cleanupStorage: maintenance.cleanupStorage,
    openSelectedBookFolder: list.openSelectedBookFolder,
    openAppDataFolder: maintenance.openAppDataFolder,
    deleteSelectedBook,
    setSelectedPlaybackPosition,
  }), [
    session,
    libraryGeneration,
    list.searchQuery,
    list.captureNavigation,
    list.setSearchQuery,
    list.modeFilter,
    list.setModeFilter,
    list.items,
    list.unfilteredItems,
    list.indexedCount,
    list.hasMore,
    shelf,
    disconnected,
    list.selectedBookId,
    list.selectedBookIndex,
    list.selectedBookDetail,
    list.selectedBookPlayable,
    list.loadingList,
    list.loadingMore,
    list.loadingDetail,
    list.savingMetadata,
    list.errorMessage,
    list.statusMessage,
    list.pendingDeletionPath,
    list.openPendingDeletionFolder,
    list.bookDetailText,
    list.refreshLibrary,
    list.reloadLibrarySession,
    list.loadMore,
    list.selectBook,
    list.selectBookById,
    list.selectRelative,
    updateSelectedBookMetadata,
    list.loadCleaningPreview,
    repairSelectedBook,
    list.openSelectedBookFolder,
    deleteSelectedBook,
    setSelectedPlaybackPosition,
    maintenance.maintenanceReport,
    maintenance.maintenanceBackups,
    maintenance.maintenanceLoading,
    maintenance.maintenanceMessage,
    maintenance.canOpenAppData,
    maintenance.loadMaintenanceDiagnostics,
    maintenance.backupDatabase,
    maintenance.restoreBackup,
    maintenance.cleanupStorage,
    maintenance.openAppDataFolder,
  ]);
}
