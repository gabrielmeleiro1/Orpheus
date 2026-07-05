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

// Default worker limit used when repairing missing chapters from the library.
const DEFAULT_REPAIR_PARALLEL_WORKER_LIMIT = 4;

export type LibraryModeFilter = "all" | ConversionMode;

/**
 * Thin, React-free wrappers over the API client for every library and
 * maintenance call. Centralising them here keeps the hooks focused on state
 * and gives every request a single place to opt into {signal, timeoutMs}.
 */
export interface LibraryRepository {
  listBooks(search: string, modeFilter: LibraryModeFilter, options?: ApiRequestOptions): Promise<LibraryListResponse>;
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
  return {
    listBooks(search, modeFilter, options) {
      return apiClient.getLibrary(search || undefined, undefined, {
        conversionMode: modeFilter === "all" ? "" : modeFilter,
      }, options);
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
      return apiClient.repairConversion(
        bookId,
        {
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
        },
        options,
      );
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
