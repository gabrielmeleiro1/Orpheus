import type { ApiClient, ApiRequestOptions } from "../../api/client";
import type {
  CleaningPreviewResponse,
  ConversionMode,
  DeleteBookResponse,
  LibraryBookDetail,
  LibraryBookUpdateRequest,
  LibraryListResponse,
  MaintenanceBackupResponse,
  MaintenanceCleanupResponse,
  MaintenanceDiagnosticsResponse,
  OpenBookFolderResponse,
  StartConversionResponse,
} from "../../types/api";

export type LibraryModeFilter = "all" | ConversionMode;

// Page size for library listing. The backend caps a single request at 1000
// items; we page in chunks and stop on the first short page so libraries
// larger than one page are no longer silently truncated.
export const LIBRARY_PAGE_SIZE = 500;
// Hard stop so a misbehaving backend that keeps returning full pages cannot
// loop forever (50 pages = 25k books).
const LIBRARY_MAX_PAGES = 50;

/**
 * Thin, React-free wrappers over the API client for every library and
 * maintenance call. Centralising them here keeps the hooks focused on state
 * and gives every request a single place to opt into {signal, timeoutMs}.
 */
export interface LibraryRepository {
  listBooks(search: string, modeFilter: LibraryModeFilter, options?: ApiRequestOptions): Promise<LibraryListResponse>;
  countBooks(options?: ApiRequestOptions): Promise<number>;
  getBook(bookId: number, options?: ApiRequestOptions): Promise<LibraryBookDetail>;
  updateBook(
    bookId: number,
    payload: LibraryBookUpdateRequest,
    options?: ApiRequestOptions,
  ): Promise<LibraryBookDetail>;
  deleteBook(bookId: number, options?: ApiRequestOptions): Promise<DeleteBookResponse>;
  getCleaningPreview(
    bookId: number,
    chapterNumber: number,
    options?: ApiRequestOptions,
  ): Promise<CleaningPreviewResponse>;
  openBookFolder(bookId: number, options?: ApiRequestOptions): Promise<OpenBookFolderResponse>;
  repairBook(bookId: number, options?: ApiRequestOptions): Promise<StartConversionResponse>;
  getMaintenanceDiagnostics(options?: ApiRequestOptions): Promise<MaintenanceDiagnosticsResponse>;
  backupDatabase(options?: ApiRequestOptions): Promise<MaintenanceBackupResponse>;
  cleanupStorage(options?: ApiRequestOptions): Promise<MaintenanceCleanupResponse>;
}

export function createLibraryRepository(apiClient: ApiClient): LibraryRepository {
  async function fetchAllBooks(
    search: string,
    modeFilter: LibraryModeFilter,
    options?: ApiRequestOptions,
  ): Promise<LibraryListResponse["items"]> {
    const items: LibraryListResponse["items"] = [];
    for (let page = 0; page < LIBRARY_MAX_PAGES; page += 1) {
      const response = await apiClient.getLibrary(search || undefined, LIBRARY_PAGE_SIZE, {
        conversionMode: modeFilter === "all" ? "" : modeFilter,
        offset: page * LIBRARY_PAGE_SIZE,
      }, options);
      items.push(...response.items);
      if (response.items.length < LIBRARY_PAGE_SIZE) {
        break;
      }
    }
    return items;
  }

  return {
    async listBooks(search, modeFilter, options) {
      const items = await fetchAllBooks(search, modeFilter, options);
      return { items, count: items.length };
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
      return apiClient.repairConversion(bookId, {}, options);
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
