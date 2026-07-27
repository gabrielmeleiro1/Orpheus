import {
  createHttpTransport,
  type ApiRequestOptions,
  type JsonRequestOptions,
} from "./http";
import { createConversionApi } from "./resources/conversion";
import { createLibraryApi, type LibraryQueryOptions } from "./resources/library";
import { createMaintenanceApi } from "./resources/maintenance";
import { createSetupApi } from "./resources/setup";
import { createSystemApi } from "./resources/system";
import type {
  ActiveConversionResponse,
  CancelConversionResponse,
  CleaningPreviewResponse,
  DeleteBookResponse,
  DesktopQueryTokenRequest,
  DesktopQueryTokenResponse,
  DesktopBackendEndpoint,
  DesktopBackendStatusEndpoint,
  HealthResponse,
  LibraryBookDetail,
  LibraryBookUpdateRequest,
  LibraryCountResponse,
  LibraryListResponse,
  MaintenanceBackupResponse,
  MaintenanceCleanupResponse,
  MaintenanceDiagnosticsResponse,
  ModelOptionsResponse,
  OpenBookFolderResponse,
  PlaybackPositionUpdateResponse,
  RepairConversionRequest,
  SourcePreflight,
  SourceTextUploadRequest,
  SourceUploadResponse,
  StartConversionRequest,
  StartConversionResponse,
  SystemCapabilitiesResponse,
  RuntimeModelDownloadRequest,
  RuntimeModelDownloadStatus,
  RuntimeSetupAdoptRequest,
  RuntimeSetupRuntimeDeleteResponse,
  RuntimeSetupStatusResponse,
  VoiceSuggestionResponse,
  VoiceOptionsResponse,
} from "../types/api";

export { ApiError } from "./http";
export type { ApiErrorCode, ApiRequestOptions } from "./http";

interface ApiClientOptions {
  baseUrl: string;
  token?: string;
}

export interface DesktopBackendStatus {
  lifecycle: "stopped" | "starting" | "ready" | "failed";
  endpoint: DesktopBackendStatusEndpoint | null;
  error: string | null;
}

export interface ApiClient {
  usesDesktopAuth: boolean;
  requestJson<T>(path: string, init?: JsonRequestOptions): Promise<T>;
  requestBlob(path: string, init?: JsonRequestOptions): Promise<Blob>;
  getHealth(options?: ApiRequestOptions): Promise<HealthResponse>;
  getModelOptions(options?: ApiRequestOptions): Promise<ModelOptionsResponse>;
  fetchOpenAiModelIds(apiKey: string, options?: ApiRequestOptions): Promise<string[]>;
  getVoiceOptions(engine: string, options?: ApiRequestOptions): Promise<VoiceOptionsResponse>;
  getSystemCapabilities(options?: ApiRequestOptions): Promise<SystemCapabilitiesResponse>;
  getRuntimeSetupStatus(options?: ApiRequestOptions): Promise<RuntimeSetupStatusResponse>;
  adoptRuntimeSetup(
    request: RuntimeSetupAdoptRequest,
    options?: ApiRequestOptions,
  ): Promise<RuntimeSetupStatusResponse>;
  deleteRuntimeSetupData(options?: ApiRequestOptions): Promise<RuntimeSetupRuntimeDeleteResponse>;
  getRuntimeModelDownloadStatus(options?: ApiRequestOptions): Promise<RuntimeModelDownloadStatus>;
  startRuntimeModelDownload(
    request: RuntimeModelDownloadRequest,
    options?: ApiRequestOptions,
  ): Promise<RuntimeModelDownloadStatus>;
  cancelRuntimeModelDownload(options?: ApiRequestOptions): Promise<RuntimeModelDownloadStatus>;
  preflightSource(sourcePath: string, options?: ApiRequestOptions): Promise<SourcePreflight>;
  uploadSourceFile(file: File, options?: ApiRequestOptions): Promise<SourceUploadResponse>;
  uploadSourceText(payload: SourceTextUploadRequest, options?: ApiRequestOptions): Promise<SourceUploadResponse>;
  suggestVoice(sourcePath: string, engine?: string | null, options?: ApiRequestOptions): Promise<VoiceSuggestionResponse>;
  startConversion(payload: StartConversionRequest, options?: ApiRequestOptions): Promise<StartConversionResponse>;
  repairConversion(
    bookId: number,
    payload: RepairConversionRequest,
    options?: ApiRequestOptions,
  ): Promise<StartConversionResponse>;
  getActiveConversion(options?: ApiRequestOptions): Promise<ActiveConversionResponse>;
  cancelConversion(jobId: string, options?: ApiRequestOptions): Promise<CancelConversionResponse>;
  getLibrary(
    search?: string,
    limit?: number,
    filters?: LibraryQueryOptions,
    options?: ApiRequestOptions,
  ): Promise<LibraryListResponse>;
  getLibraryCount(options?: ApiRequestOptions): Promise<LibraryCountResponse>;
  getLibraryBook(bookId: number, options?: ApiRequestOptions): Promise<LibraryBookDetail>;
  getLibraryAudioUrl(bookId: number, options?: ApiRequestOptions): Promise<string>;
  createDesktopQueryToken(
    payload: DesktopQueryTokenRequest,
    options?: ApiRequestOptions,
  ): Promise<DesktopQueryTokenResponse>;
  updateLibraryBook(
    bookId: number,
    payload: LibraryBookUpdateRequest,
    options?: ApiRequestOptions,
  ): Promise<LibraryBookDetail>;
  getCleaningPreview(
    bookId: number,
    chapterNumber: number,
    options?: ApiRequestOptions,
  ): Promise<CleaningPreviewResponse>;
  updateBookPlaybackPosition(
    bookId: number,
    positionSec: number,
    options?: ApiRequestOptions,
  ): Promise<PlaybackPositionUpdateResponse>;
  openBookFolder(bookId: number, options?: ApiRequestOptions): Promise<OpenBookFolderResponse>;
  deleteBook(bookId: number, options?: ApiRequestOptions): Promise<DeleteBookResponse>;
  getMaintenanceDiagnostics(options?: ApiRequestOptions): Promise<MaintenanceDiagnosticsResponse>;
  backupDatabase(options?: ApiRequestOptions): Promise<MaintenanceBackupResponse>;
  cleanupStorage(options?: ApiRequestOptions): Promise<MaintenanceCleanupResponse>;
}

export function createApiClient({ baseUrl, token }: ApiClientOptions): ApiClient {
  const resolvedBaseUrl = baseUrl.trim();
  const bearerToken = token?.trim() ?? "";
  const usesDesktopAuth = Boolean(bearerToken);

  const { requestJson, requestBlob } = createHttpTransport(resolvedBaseUrl, bearerToken);

  return {
    usesDesktopAuth,
    requestJson,
    requestBlob,
    ...createSystemApi(requestJson),
    ...createSetupApi(requestJson),
    ...createConversionApi(requestJson),
    ...createLibraryApi({ requestJson, resolvedBaseUrl, usesDesktopAuth }),
    ...createMaintenanceApi(requestJson),
  };
}

export function createApiClientFromDesktopEndpoint(endpoint: DesktopBackendEndpoint): ApiClient {
  return createApiClient({ baseUrl: endpoint.base_url, token: endpoint.token });
}
