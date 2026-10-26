import type { LibraryRequestOptions } from "../../api/resources/library";
import type { CleanupStorageOptions } from "../../api/resources/maintenance";
import type { ApiClient, ApiRequestOptions } from "../../api/client";
import type {
  CleaningPreviewResponse,
  ConversionMode,
  DeleteBookResponse,
  LibraryBookDetail,
  LibraryIdentity,
  LibraryBookSummary,
  LibraryBookUpdateRequest,
  MaintenanceBackupResponse,
  MaintenanceCleanupResponse,
  MaintenanceDiagnosticsResponse,
  OpenBookFolderResponse,
  StartConversionResponse,
} from "../../types/api";

export type LibraryModeFilter = "all" | ConversionMode;

// Fixed page size for library listing. The list is fetched one page at a time
// and the caller pulls more on demand, so we no longer eagerly materialise the
// whole library (previously up to 25k records) just to render the first screen.
// The backend caps a single request at 1000 items; this stays well under that
// while keeping each page cheap to fetch and render.
export const LIBRARY_PAGE_SIZE = 100;

/** One page of library results plus the metadata needed to page further. */
interface LibraryPage extends LibraryIdentity {
  items: LibraryBookSummary[];
  /** The offset this page was requested at. */
  offset: number;
  /** True when the page came back full, so another page may exist. */
  hasMore: boolean;
}

/**
 * Thin, React-free wrappers over the API client for every library and
 * maintenance call. Centralising them here keeps the hooks focused on state
 * and gives every request a single place to opt into {signal, timeoutMs}.
 */
export interface LibraryRepository {
  listPage(
    search: string,
    modeFilter: LibraryModeFilter,
    offset: number,
    options?: ApiRequestOptions,
  ): Promise<LibraryPage>;
  countBooks(options?: ApiRequestOptions): Promise<number>;
  getBook(bookId: number, options?: ApiRequestOptions): Promise<LibraryBookDetail>;
  updateBook(
    bookId: number,
    payload: LibraryBookUpdateRequest,
    options?: ApiRequestOptions,
  ): Promise<LibraryBookDetail>;
  deleteBook(bookId: number, options?: LibraryRequestOptions): Promise<DeleteBookResponse>;
  getCleaningPreview(
    bookId: number,
    chapterNumber: number,
    options?: ApiRequestOptions,
  ): Promise<CleaningPreviewResponse>;
  openBookFolder(bookId: number, options?: ApiRequestOptions): Promise<OpenBookFolderResponse>;
  repairBook(bookId: number, options?: LibraryRequestOptions): Promise<StartConversionResponse>;
  getMaintenanceDiagnostics(options?: ApiRequestOptions): Promise<MaintenanceDiagnosticsResponse>;
  backupDatabase(options?: ApiRequestOptions): Promise<MaintenanceBackupResponse>;
  cleanupStorage(options?: CleanupStorageOptions): Promise<MaintenanceCleanupResponse>;
}

export function createLibraryRepository(apiClient: ApiClient): LibraryRepository {
  return {
    async listPage(search, modeFilter, offset, options) {
      const response = await apiClient.getLibrary(search || undefined, LIBRARY_PAGE_SIZE, {
        conversionMode: modeFilter === "all" ? "" : modeFilter,
        offset,
      }, options);
      const items = Array.isArray(response.items) ? response.items : [];
      return {
        items: items.map((item) => ({
          ...item,
          library_id: item.library_id ?? response.library_id,
          library_epoch: item.library_epoch ?? response.library_epoch,
        })),
        library_id: response.library_id,
        library_epoch: response.library_epoch,
        offset,
        // A full page implies the backend may have more rows past this offset.
        hasMore: items.length >= LIBRARY_PAGE_SIZE,
      };
    },
    async countBooks(options) {
      const response = await apiClient.getLibraryCount(options);
      return Math.max(0, Number(response.count) || 0);
    },
    getBook(bookId, options) {
      return apiClient.getLibraryBook(bookId, options);
    },
    updateBook(bookId, payload, options) {
      return apiClient.updateLibraryBook(bookId, payload, options);
    },
    deleteBook(bookId, options) {
      return apiClient.deleteBook(bookId, options);
    },
    getCleaningPreview(bookId, chapterNumber, options) {
      return apiClient.getCleaningPreview(bookId, chapterNumber, options);
    },
    openBookFolder(bookId, options) {
      return apiClient.openBookFolder(bookId, options);
    },
    repairBook(bookId, options) {
      // Omit narration/worker fields so the backend reuses persisted conversion
      // settings and forces safe automatic worker planning.
      return apiClient.repairConversion(bookId, { expected_epoch: options?.expectedEpoch }, options);
    },
    getMaintenanceDiagnostics(options) {
      return apiClient.getMaintenanceDiagnostics(options);
    },
    backupDatabase(options) {
      return apiClient.backupDatabase(options);
    },
    cleanupStorage(options) {
      return apiClient.cleanupStorage(options);
    },
  };
}
