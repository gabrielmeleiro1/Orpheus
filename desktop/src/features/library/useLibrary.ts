import type {
  CleaningPreviewResponse,
  ConversionMode,
  LibraryBookDetail,
  LibraryBookSummary,
  MaintenanceDiagnosticsResponse,
} from "../../types/api";
import type { ApiClient } from "../../api/client";
import { useLibraryList } from "./useLibraryList";
import { useLibraryMaintenance } from "./useLibraryMaintenance";

export { formatLibraryMeta } from "./useLibraryList";

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
  maintenanceReport: MaintenanceDiagnosticsResponse | null;
  maintenanceLoading: boolean;
  maintenanceMessage: string;
  canOpenAppData: boolean;
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
  openAppDataFolder: () => Promise<void>;
  deleteSelectedBook: () => Promise<boolean>;
  setSelectedPlaybackPosition: (positionSec: number) => void;
}

/**
 * Compatibility facade composing the decomposed library hooks. Its public
 * return shape matches the historical monolithic hook (plus additive
 * app-data-folder controls moved out of LibraryPanel), so existing consumers
 * keep working unchanged.
 */
export function useLibrary({ apiClient, backendDisconnected = false }: UseLibraryArgs): UseLibraryResult {
  const list = useLibraryList({ apiClient, backendDisconnected });
  const maintenance = useLibraryMaintenance({
    apiClient,
    onAfterCleanup: list.reloadCurrentQuery,
    getReloadContextGeneration: list.getReloadContextGeneration,
  });

  return {
    searchQuery: list.searchQuery,
    setSearchQuery: list.setSearchQuery,
    modeFilter: list.modeFilter,
    setModeFilter: list.setModeFilter,
    items: list.items,
    indexedCount: list.indexedCount,
    selectedBookId: list.selectedBookId,
    selectedBookIndex: list.selectedBookIndex,
    selectedBookDetail: list.selectedBookDetail,
    selectedBookPlayable: list.selectedBookPlayable,
    loadingList: list.loadingList,
    loadingDetail: list.loadingDetail,
    errorMessage: list.errorMessage,
    statusMessage: list.statusMessage,
    bookSummaryTitle: list.bookSummaryTitle,
    bookSummaryMeta: list.bookSummaryMeta,
    bookSummaryExtra: list.bookSummaryExtra,
    bookDetailText: list.bookDetailText,
    maintenanceReport: maintenance.maintenanceReport,
    maintenanceLoading: maintenance.maintenanceLoading,
    maintenanceMessage: maintenance.maintenanceMessage,
    canOpenAppData: maintenance.canOpenAppData,
    refreshLibrary: list.refreshLibrary,
    selectBook: list.selectBook,
    selectRelative: list.selectRelative,
    updateSelectedBookMetadata: list.updateSelectedBookMetadata,
    loadCleaningPreview: list.loadCleaningPreview,
    repairSelectedBook: list.repairSelectedBook,
    loadMaintenanceDiagnostics: maintenance.loadMaintenanceDiagnostics,
    backupDatabase: maintenance.backupDatabase,
    cleanupStorage: maintenance.cleanupStorage,
    openSelectedBookFolder: list.openSelectedBookFolder,
    openAppDataFolder: maintenance.openAppDataFolder,
    deleteSelectedBook: list.deleteSelectedBook,
    setSelectedPlaybackPosition: list.setSelectedPlaybackPosition,
  };
}
