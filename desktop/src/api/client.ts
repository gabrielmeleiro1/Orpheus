import type {
  ActiveConversionResponse,
  CancelConversionResponse,
  CleaningPreviewResponse,
  ConversionMode,
  DeleteBookResponse,
  DesktopQueryTokenRequest,
  DesktopQueryTokenResponse,
  DesktopBackendEndpoint,
  DesktopBackendStatusEndpoint,
  HealthResponse,
  LibraryBookDetail,
  LibraryBookUpdateRequest,
  LibraryListResponse,
  MaintenanceBackupResponse,
  MaintenanceCleanupResponse,
  MaintenanceDiagnosticsResponse,
  ModelOptionsResponse,
  OpenBookFolderResponse,
  OpenAIModelListResponse,
  ParallelWarmupRequest,
  ParallelWorkerRecommendation,
  PlaybackPositionUpdateResponse,
  RepairConversionRequest,
  SourcePreflight,
  SourceTextUploadRequest,
  SourceUploadResponse,
  StartConversionRequest,
  StartConversionResponse,
  SystemCapabilitiesResponse,
  VoiceSuggestionResponse,
  VoiceOptionsResponse,
} from "../types/api";

interface ApiClientOptions {
  baseUrl: string;
  token?: string;
}

export interface DesktopBackendStatus {
  lifecycle: "stopped" | "starting" | "ready" | "failed";
  endpoint: DesktopBackendStatusEndpoint | null;
  error: string | null;
}

interface JsonRequestOptions extends Omit<RequestInit, "headers"> {
  headers?: HeadersInit;
}

export interface ApiClient {
  usesDesktopAuth: boolean;
  requestJson<T>(path: string, init?: JsonRequestOptions): Promise<T>;
  requestBlob(path: string, init?: JsonRequestOptions): Promise<Blob>;
  getHealth(): Promise<HealthResponse>;
  getModelOptions(): Promise<ModelOptionsResponse>;
  fetchOpenAiModelIds(apiKey: string): Promise<string[]>;
  getVoiceOptions(engine: string): Promise<VoiceOptionsResponse>;
  getSystemCapabilities(): Promise<SystemCapabilitiesResponse>;
  warmupParallelWorkers(payload: ParallelWarmupRequest): Promise<ParallelWorkerRecommendation>;
  preflightSource(sourcePath: string): Promise<SourcePreflight>;
  uploadSourceText(payload: SourceTextUploadRequest): Promise<SourceUploadResponse>;
  suggestVoice(sourcePath: string, engine?: string | null): Promise<VoiceSuggestionResponse>;
  startConversion(payload: StartConversionRequest): Promise<StartConversionResponse>;
  repairConversion(bookId: number, payload: RepairConversionRequest): Promise<StartConversionResponse>;
  getActiveConversion(): Promise<ActiveConversionResponse>;
  cancelConversion(jobId: string): Promise<CancelConversionResponse>;
  getLibrary(search?: string, limit?: number, filters?: LibraryQueryOptions): Promise<LibraryListResponse>;
  getLibraryBook(bookId: number): Promise<LibraryBookDetail>;
  getLibraryAudioUrl(bookId: number): Promise<string>;
  createDesktopQueryToken(payload: DesktopQueryTokenRequest): Promise<DesktopQueryTokenResponse>;
  updateLibraryBook(bookId: number, payload: LibraryBookUpdateRequest): Promise<LibraryBookDetail>;
  getCleaningPreview(bookId: number, chapterNumber: number): Promise<CleaningPreviewResponse>;
  updateBookPlaybackPosition(bookId: number, positionSec: number): Promise<PlaybackPositionUpdateResponse>;
  openBookFolder(bookId: number): Promise<OpenBookFolderResponse>;
  deleteBook(bookId: number): Promise<DeleteBookResponse>;
  getMaintenanceDiagnostics(): Promise<MaintenanceDiagnosticsResponse>;
  backupDatabase(): Promise<MaintenanceBackupResponse>;
  cleanupStorage(): Promise<MaintenanceCleanupResponse>;
}

const OPENAI_MODELS_URL = "https://api.openai.com/v1/models";

export interface LibraryQueryOptions {
  conversionMode?: ConversionMode | "";
}

function resolveUrl(baseUrl: string, path: string): string {
  if (/^https?:\/\//.test(path)) {
    return path;
  }

  const normalizedBase = baseUrl.replace(/\/+$/, "");
  if (!normalizedBase) {
    return path;
  }

  if (path.startsWith("/")) {
    return `${normalizedBase}${path}`;
  }

  return `${normalizedBase}/${path}`;
}

function createHeaders(
  init: JsonRequestOptions,
  bearerToken: string,
  defaultAccept: string | null = "application/json",
): Headers {
  const headers = new Headers(init.headers);
  if (defaultAccept && !headers.has("Accept")) {
    headers.set("Accept", defaultAccept);
  }
  if (bearerToken) {
    headers.set("Authorization", `Bearer ${bearerToken}`);
  }
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return headers;
}

function normalizeErrorText(bodyText: string): string {
  const trimmed = bodyText.trim();
  if (!trimmed) {
    return "";
  }

  try {
    const parsed = JSON.parse(trimmed) as { detail?: unknown; message?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      return parsed.detail.trim();
    }
    if (typeof parsed.message === "string" && parsed.message.trim()) {
      return parsed.message.trim();
    }
  } catch {
    // Ignore JSON parse failures and return raw text.
  }

  return trimmed;
}

export function createApiClient({ baseUrl, token }: ApiClientOptions): ApiClient {
  const resolvedBaseUrl = baseUrl.trim();
  const bearerToken = token?.trim() ?? "";
  const usesDesktopAuth = Boolean(bearerToken);

  const normalizeBookId = (bookId: number): number => {
    if (!Number.isInteger(bookId) || bookId <= 0) {
      throw new Error("bookId must be a positive integer");
    }
    return bookId;
  };

  async function requestJson<T>(path: string, init: JsonRequestOptions = {}): Promise<T> {
    const headers = createHeaders(init, bearerToken, "application/json");

    const response = await fetch(resolveUrl(resolvedBaseUrl, path), {
      ...init,
      headers,
    });

    if (!response.ok) {
      const bodyText = await response.text();
      const bodyHint = normalizeErrorText(bodyText);
      throw new Error(`API request failed (${response.status})${bodyHint ? `: ${bodyHint}` : ""}`);
    }

    return (await response.json()) as T;
  }

  async function requestBlob(path: string, init: JsonRequestOptions = {}): Promise<Blob> {
    const headers = createHeaders(init, bearerToken, null);

    const response = await fetch(resolveUrl(resolvedBaseUrl, path), {
      ...init,
      headers,
    });

    if (!response.ok) {
      const bodyText = await response.text();
      const bodyHint = normalizeErrorText(bodyText);
      throw new Error(`API request failed (${response.status})${bodyHint ? `: ${bodyHint}` : ""}`);
    }

    return response.blob();
  }

  async function getHealth(): Promise<HealthResponse> {
    return requestJson<HealthResponse>("/health");
  }

  async function getModelOptions(): Promise<ModelOptionsResponse> {
    return requestJson<ModelOptionsResponse>("/api/options/models");
  }

  async function fetchOpenAiModelIds(apiKey: string): Promise<string[]> {
    const normalizedKey = apiKey.trim();
    if (!normalizedKey) {
      throw new Error("OpenAI API key is required");
    }
    const response = await requestJson<OpenAIModelListResponse>("/api/options/openai/models", {
      method: "POST",
      body: JSON.stringify({
        api_key: normalizedKey,
        models_url: OPENAI_MODELS_URL,
      }),
    });
    return Array.isArray(response.models) ? response.models : [];
  }

  async function getVoiceOptions(engine: string): Promise<VoiceOptionsResponse> {
    const params = new URLSearchParams();
    if (engine.trim()) {
      params.set("engine", engine.trim());
    }
    const query = params.toString();
    const path = query ? `/api/options/voices?${query}` : "/api/options/voices";
    return requestJson<VoiceOptionsResponse>(path);
  }

  async function getSystemCapabilities(): Promise<SystemCapabilitiesResponse> {
    return requestJson<SystemCapabilitiesResponse>("/api/system/capabilities");
  }

  async function warmupParallelWorkers(payload: ParallelWarmupRequest): Promise<ParallelWorkerRecommendation> {
    return requestJson<ParallelWorkerRecommendation>("/api/options/parallel/warmup", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async function preflightSource(sourcePath: string): Promise<SourcePreflight> {
    return requestJson<SourcePreflight>("/api/sources/preflight", {
      method: "POST",
      body: JSON.stringify({ source_path: sourcePath }),
    });
  }

  async function uploadSourceText(payload: SourceTextUploadRequest): Promise<SourceUploadResponse> {
    return requestJson<SourceUploadResponse>("/api/uploads/text", {
      method: "POST",
      body: JSON.stringify({
        text: payload.text,
        title: payload.title?.trim() || null,
      }),
    });
  }

  async function suggestVoice(
    sourcePath: string,
    engine?: string | null,
  ): Promise<VoiceSuggestionResponse> {
    return requestJson<VoiceSuggestionResponse>("/api/voices/suggest", {
      method: "POST",
      body: JSON.stringify({
        source_path: sourcePath,
        tts_engine: engine || null,
      }),
    });
  }

  async function startConversion(payload: StartConversionRequest): Promise<StartConversionResponse> {
    return requestJson<StartConversionResponse>("/api/conversions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async function repairConversion(
    bookId: number,
    payload: RepairConversionRequest,
  ): Promise<StartConversionResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<StartConversionResponse>(`/api/conversions/repair/${normalizedBookId}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async function getActiveConversion(): Promise<ActiveConversionResponse> {
    return requestJson<ActiveConversionResponse>("/api/conversions/active");
  }

  async function cancelConversion(jobId: string): Promise<CancelConversionResponse> {
    const normalizedJobId = jobId.trim();
    if (!normalizedJobId) {
      throw new Error("jobId is required");
    }

    return requestJson<CancelConversionResponse>(
      `/api/conversions/${encodeURIComponent(normalizedJobId)}/cancel`,
      {
        method: "POST",
      },
    );
  }

  async function getLibrary(
    search?: string,
    limit?: number,
    filters: LibraryQueryOptions = {},
  ): Promise<LibraryListResponse> {
    const params = new URLSearchParams();

    const normalizedSearch = String(search ?? "").trim();
    if (normalizedSearch) {
      params.set("search", normalizedSearch);
    }

    if (Number.isInteger(limit) && (limit ?? 0) > 0) {
      params.set("limit", String(limit));
    }

    if (filters.conversionMode) {
      params.set("conversion_mode", filters.conversionMode);
    }

    const query = params.toString();
    const path = query ? `/api/library?${query}` : "/api/library";
    return requestJson<LibraryListResponse>(path);
  }

  async function getLibraryBook(bookId: number): Promise<LibraryBookDetail> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<LibraryBookDetail>(`/api/library/${normalizedBookId}`);
  }

  async function createDesktopQueryToken(
    payload: DesktopQueryTokenRequest,
  ): Promise<DesktopQueryTokenResponse> {
    const requestPayload: DesktopQueryTokenRequest =
      payload.scope === "audio"
        ? { scope: "audio", book_id: normalizeBookId(Number(payload.book_id)) }
        : { scope: "events" };

    return requestJson<DesktopQueryTokenResponse>("/api/desktop/query-token", {
      method: "POST",
      body: JSON.stringify(requestPayload),
    });
  }

  async function updateLibraryBook(
    bookId: number,
    payload: LibraryBookUpdateRequest,
  ): Promise<LibraryBookDetail> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<LibraryBookDetail>(`/api/library/${normalizedBookId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  }

  async function getCleaningPreview(
    bookId: number,
    chapterNumber: number,
  ): Promise<CleaningPreviewResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    const normalizedChapter = Math.max(1, Math.floor(Number(chapterNumber) || 0));
    return requestJson<CleaningPreviewResponse>(
      `/api/library/${normalizedBookId}/chapters/${normalizedChapter}/cleaning-preview`,
    );
  }

  async function getLibraryAudioUrl(bookId: number): Promise<string> {
    const normalizedBookId = normalizeBookId(bookId);
    const resolved = resolveUrl(resolvedBaseUrl, `/api/library/${normalizedBookId}/audio`);
    const url = new URL(resolved, window.location.origin);

    if (usesDesktopAuth) {
      const queryToken = await createDesktopQueryToken({ scope: "audio", book_id: normalizedBookId });
      if (queryToken.token.trim()) {
        url.searchParams.set("token", queryToken.token);
      }
    }

    return url.toString();
  }

  async function updateBookPlaybackPosition(
    bookId: number,
    positionSec: number,
  ): Promise<PlaybackPositionUpdateResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    const normalizedPosition = Number(positionSec);
    if (!Number.isFinite(normalizedPosition) || normalizedPosition < 0) {
      throw new Error("positionSec must be a non-negative number");
    }

    return requestJson<PlaybackPositionUpdateResponse>(
      `/api/library/${normalizedBookId}/playback-position`,
      {
        method: "PUT",
        body: JSON.stringify({ position_sec: normalizedPosition }),
      },
    );
  }

  async function openBookFolder(bookId: number): Promise<OpenBookFolderResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<OpenBookFolderResponse>(`/api/library/${normalizedBookId}/open-folder`, {
      method: "POST",
    });
  }

  async function deleteBook(bookId: number): Promise<DeleteBookResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<DeleteBookResponse>(`/api/library/${normalizedBookId}`, {
      method: "DELETE",
    });
  }

  async function getMaintenanceDiagnostics(): Promise<MaintenanceDiagnosticsResponse> {
    return requestJson<MaintenanceDiagnosticsResponse>("/api/maintenance/diagnostics");
  }

  async function backupDatabase(): Promise<MaintenanceBackupResponse> {
    return requestJson<MaintenanceBackupResponse>("/api/maintenance/backup", {
      method: "POST",
    });
  }

  async function cleanupStorage(): Promise<MaintenanceCleanupResponse> {
    return requestJson<MaintenanceCleanupResponse>("/api/maintenance/cleanup", {
      method: "POST",
      body: JSON.stringify({ confirm: "cleanup" }),
    });
  }

  return {
    usesDesktopAuth,
    requestJson,
    requestBlob,
    getHealth,
    getModelOptions,
    fetchOpenAiModelIds,
    getVoiceOptions,
    getSystemCapabilities,
    warmupParallelWorkers,
    preflightSource,
    uploadSourceText,
    suggestVoice,
    startConversion,
    repairConversion,
    getActiveConversion,
    cancelConversion,
    getLibrary,
    getLibraryBook,
    getLibraryAudioUrl,
    createDesktopQueryToken,
    updateLibraryBook,
    getCleaningPreview,
    updateBookPlaybackPosition,
    openBookFolder,
    deleteBook,
    getMaintenanceDiagnostics,
    backupDatabase,
    cleanupStorage,
  };
}

export function createApiClientFromDesktopEndpoint(endpoint: DesktopBackendEndpoint): ApiClient {
  return createApiClient({ baseUrl: endpoint.base_url, token: endpoint.token });
}
