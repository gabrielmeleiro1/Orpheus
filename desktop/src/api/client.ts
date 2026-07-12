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

export type ApiErrorCode = "http" | "network" | "timeout" | "aborted" | "invalid-json";

export class ApiError extends Error {
  status: number | null;
  code: ApiErrorCode;
  detail: string;
  retriable: boolean;

  constructor({
    message,
    status,
    code,
    detail,
    retriable,
  }: {
    message: string;
    status: number | null;
    code: ApiErrorCode;
    detail: string;
    retriable: boolean;
  }) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.retriable = retriable;
  }
}

export interface DesktopBackendStatus {
  lifecycle: "stopped" | "starting" | "ready" | "failed";
  endpoint: DesktopBackendStatusEndpoint | null;
  error: string | null;
}

export interface ApiRequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

interface JsonRequestOptions extends Omit<RequestInit, "headers" | "signal">, ApiRequestOptions {
  headers?: HeadersInit;
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
  warmupParallelWorkers(payload: ParallelWarmupRequest, options?: ApiRequestOptions): Promise<ParallelWorkerRecommendation>;
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

export interface LibraryQueryOptions {
  conversionMode?: ConversionMode | "";
  favorite?: boolean;
  offset?: number;
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

function summarizeValidationLocation(loc: unknown): string {
  if (!Array.isArray(loc)) {
    return "";
  }

  const parts = loc
    .map((item) => String(item).trim())
    .filter((item) => item.length > 0 && item !== "body");
  return parts.join(".");
}

function summarizeValidationDetails(detail: unknown): string {
  if (!Array.isArray(detail)) {
    return "";
  }

  return detail
    .map((item) => {
      if (!item || typeof item !== "object") {
        return "";
      }
      const source = item as Record<string, unknown>;
      const message = typeof source.msg === "string" ? source.msg.trim() : "";
      if (!message) {
        return "";
      }
      const location = summarizeValidationLocation(source.loc);
      return location ? `${location}: ${message}` : message;
    })
    .filter((item) => item.length > 0)
    .join("; ");
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
    const detailSummary = summarizeValidationDetails(parsed.detail);
    if (detailSummary) {
      return detailSummary;
    }
    if (typeof parsed.message === "string" && parsed.message.trim()) {
      return parsed.message.trim();
    }
  } catch {
    // Ignore JSON parse failures and return raw text.
  }

  return trimmed;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function createHttpError(status: number, detail: string): ApiError {
  return new ApiError({
    message: `API request failed (${status})${detail ? `: ${detail}` : ""}`,
    status,
    code: "http",
    detail,
    retriable: status === 408 || status === 429 || status >= 500,
  });
}

function createRequestFailure(error: unknown, timedOut: boolean): ApiError {
  if (timedOut) {
    return new ApiError({
      message: "request timed out",
      status: null,
      code: "timeout",
      detail: "request timed out",
      retriable: true,
    });
  }

  if (isAbortError(error)) {
    return new ApiError({
      message: "request aborted",
      status: null,
      code: "aborted",
      detail: "request aborted",
      retriable: false,
    });
  }

  const detail = error instanceof Error ? error.message : String(error || "network request failed");
  return new ApiError({
    message: detail,
    status: null,
    code: "network",
    detail,
    retriable: true,
  });
}

async function fetchWithRequestOptions(
  input: RequestInfo | URL,
  init: JsonRequestOptions,
  headers: Headers,
): Promise<Response> {
  const { timeoutMs, signal, ...fetchInit } = init;
  const timeout = Number(timeoutMs);

  const controller = new AbortController();
  let timedOut = false;
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const abortFromCaller = () => controller.abort();
  const shouldUseComposedSignal = Boolean(signal) || (Number.isFinite(timeout) && timeout > 0);

  if (shouldUseComposedSignal) {
    if (signal?.aborted) {
      controller.abort();
    } else {
      signal?.addEventListener("abort", abortFromCaller, { once: true });
    }

    if (Number.isFinite(timeout) && timeout > 0) {
      timeoutId = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeout);
    }
  }

  try {
    return await fetch(input, {
      ...fetchInit,
      headers,
      ...(shouldUseComposedSignal ? { signal: controller.signal } : {}),
    });
  } catch (error: unknown) {
    throw createRequestFailure(error, timedOut);
  } finally {
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
    }
    signal?.removeEventListener("abort", abortFromCaller);
  }
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

    const response = await fetchWithRequestOptions(resolveUrl(resolvedBaseUrl, path), init, headers);

    if (!response.ok) {
      const bodyText = await response.text();
      const bodyHint = normalizeErrorText(bodyText);
      throw createHttpError(response.status, bodyHint);
    }

    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiError({
        message: "invalid JSON response",
        status: null,
        code: "invalid-json",
        detail: "invalid JSON response",
        retriable: false,
      });
    }
  }

  async function requestBlob(path: string, init: JsonRequestOptions = {}): Promise<Blob> {
    const headers = createHeaders(init, bearerToken, null);

    const response = await fetchWithRequestOptions(resolveUrl(resolvedBaseUrl, path), init, headers);

    if (!response.ok) {
      const bodyText = await response.text();
      const bodyHint = normalizeErrorText(bodyText);
      throw createHttpError(response.status, bodyHint);
    }

    return response.blob();
  }

  async function getHealth(options?: ApiRequestOptions): Promise<HealthResponse> {
    return requestJson<HealthResponse>("/health", options);
  }

  async function getModelOptions(options?: ApiRequestOptions): Promise<ModelOptionsResponse> {
    return requestJson<ModelOptionsResponse>("/api/options/models", options);
  }

  async function fetchOpenAiModelIds(apiKey: string, options?: ApiRequestOptions): Promise<string[]> {
    const normalizedKey = apiKey.trim();
    if (!normalizedKey) {
      throw new Error("OpenAI API key is required");
    }
    const response = await requestJson<OpenAIModelListResponse>("/api/options/openai/models", {
      method: "POST",
      body: JSON.stringify({
        api_key: normalizedKey,
      }),
      ...options,
    });
    return Array.isArray(response.models) ? response.models : [];
  }

  async function getVoiceOptions(engine: string, options?: ApiRequestOptions): Promise<VoiceOptionsResponse> {
    const params = new URLSearchParams();
    if (engine.trim()) {
      params.set("engine", engine.trim());
    }
    const query = params.toString();
    const path = query ? `/api/options/voices?${query}` : "/api/options/voices";
    return requestJson<VoiceOptionsResponse>(path, options);
  }

  async function getSystemCapabilities(options?: ApiRequestOptions): Promise<SystemCapabilitiesResponse> {
    return requestJson<SystemCapabilitiesResponse>("/api/system/capabilities", options);
  }

  async function warmupParallelWorkers(
    payload: ParallelWarmupRequest,
    options?: ApiRequestOptions,
  ): Promise<ParallelWorkerRecommendation> {
    return requestJson<ParallelWorkerRecommendation>("/api/options/parallel/warmup", {
      method: "POST",
      body: JSON.stringify(payload),
      ...options,
    });
  }

  async function preflightSource(sourcePath: string, options?: ApiRequestOptions): Promise<SourcePreflight> {
    return requestJson<SourcePreflight>("/api/sources/preflight", {
      method: "POST",
      body: JSON.stringify({ source_path: sourcePath }),
      ...options,
    });
  }

  async function uploadSourceText(
    payload: SourceTextUploadRequest,
    options?: ApiRequestOptions,
  ): Promise<SourceUploadResponse> {
    return requestJson<SourceUploadResponse>("/api/uploads/text", {
      method: "POST",
      body: JSON.stringify({
        text: payload.text,
        title: payload.title?.trim() || null,
      }),
      ...options,
    });
  }

  async function uploadSourceFile(file: File, options?: ApiRequestOptions): Promise<SourceUploadResponse> {
    const formData = new FormData();
    formData.set("file", file);
    return requestJson<SourceUploadResponse>("/api/uploads/source", {
      method: "POST",
      body: formData,
      ...options,
    });
  }

  async function suggestVoice(
    sourcePath: string,
    engine?: string | null,
    options?: ApiRequestOptions,
  ): Promise<VoiceSuggestionResponse> {
    return requestJson<VoiceSuggestionResponse>("/api/voices/suggest", {
      method: "POST",
      body: JSON.stringify({
        source_path: sourcePath,
        tts_engine: engine || null,
      }),
      ...options,
    });
  }

  async function startConversion(
    payload: StartConversionRequest,
    options?: ApiRequestOptions,
  ): Promise<StartConversionResponse> {
    return requestJson<StartConversionResponse>("/api/conversions", {
      method: "POST",
      body: JSON.stringify(payload),
      ...options,
    });
  }

  async function repairConversion(
    bookId: number,
    payload: RepairConversionRequest,
    options?: ApiRequestOptions,
  ): Promise<StartConversionResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<StartConversionResponse>(`/api/conversions/repair/${normalizedBookId}`, {
      method: "POST",
      body: JSON.stringify(payload),
      ...options,
    });
  }

  async function getActiveConversion(options?: ApiRequestOptions): Promise<ActiveConversionResponse> {
    return requestJson<ActiveConversionResponse>("/api/conversions/active", options);
  }

  async function cancelConversion(
    jobId: string,
    options?: ApiRequestOptions,
  ): Promise<CancelConversionResponse> {
    const normalizedJobId = jobId.trim();
    if (!normalizedJobId) {
      throw new Error("jobId is required");
    }

    return requestJson<CancelConversionResponse>(
      `/api/conversions/${encodeURIComponent(normalizedJobId)}/cancel`,
      {
        method: "POST",
        ...options,
      },
    );
  }

  async function getLibrary(
    search?: string,
    limit?: number,
    filters: LibraryQueryOptions = {},
    options?: ApiRequestOptions,
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

    if (filters.favorite !== undefined) {
      params.set("favorite", String(filters.favorite));
    }

    if (Number.isInteger(filters.offset) && (filters.offset ?? 0) > 0) {
      params.set("offset", String(filters.offset));
    }

    const query = params.toString();
    const path = query ? `/api/library?${query}` : "/api/library";
    return requestJson<LibraryListResponse>(path, options);
  }

  async function getLibraryBook(
    bookId: number,
    options?: ApiRequestOptions,
  ): Promise<LibraryBookDetail> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<LibraryBookDetail>(`/api/library/${normalizedBookId}`, options);
  }

  async function createDesktopQueryToken(
    payload: DesktopQueryTokenRequest,
    options?: ApiRequestOptions,
  ): Promise<DesktopQueryTokenResponse> {
    const requestPayload: DesktopQueryTokenRequest =
      payload.scope === "audio"
        ? { scope: "audio", book_id: normalizeBookId(Number(payload.book_id)) }
        : { scope: "events" };

    return requestJson<DesktopQueryTokenResponse>("/api/desktop/query-token", {
      method: "POST",
      body: JSON.stringify(requestPayload),
      ...options,
    });
  }

  async function updateLibraryBook(
    bookId: number,
    payload: LibraryBookUpdateRequest,
    options?: ApiRequestOptions,
  ): Promise<LibraryBookDetail> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<LibraryBookDetail>(`/api/library/${normalizedBookId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
      ...options,
    });
  }

  async function getCleaningPreview(
    bookId: number,
    chapterNumber: number,
    options?: ApiRequestOptions,
  ): Promise<CleaningPreviewResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    const normalizedChapter = Math.max(1, Math.floor(Number(chapterNumber) || 0));
    return requestJson<CleaningPreviewResponse>(
      `/api/library/${normalizedBookId}/chapters/${normalizedChapter}/cleaning-preview`,
      options,
    );
  }

  async function getLibraryAudioUrl(bookId: number, options?: ApiRequestOptions): Promise<string> {
    const normalizedBookId = normalizeBookId(bookId);
    const resolved = resolveUrl(resolvedBaseUrl, `/api/library/${normalizedBookId}/audio`);
    const url = new URL(resolved, window.location.origin);

    if (usesDesktopAuth) {
      const queryToken = await createDesktopQueryToken(
        { scope: "audio", book_id: normalizedBookId },
        options,
      );
      if (queryToken.token.trim()) {
        url.searchParams.set("token", queryToken.token);
      }
    }

    return url.toString();
  }

  async function updateBookPlaybackPosition(
    bookId: number,
    positionSec: number,
    options?: ApiRequestOptions,
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
        ...options,
      },
    );
  }

  async function openBookFolder(
    bookId: number,
    options?: ApiRequestOptions,
  ): Promise<OpenBookFolderResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<OpenBookFolderResponse>(`/api/library/${normalizedBookId}/open-folder`, {
      method: "POST",
      ...options,
    });
  }

  async function deleteBook(bookId: number, options?: ApiRequestOptions): Promise<DeleteBookResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<DeleteBookResponse>(`/api/library/${normalizedBookId}`, {
      method: "DELETE",
      ...options,
    });
  }

  async function getMaintenanceDiagnostics(
    options?: ApiRequestOptions,
  ): Promise<MaintenanceDiagnosticsResponse> {
    return requestJson<MaintenanceDiagnosticsResponse>("/api/maintenance/diagnostics", options);
  }

  async function backupDatabase(options?: ApiRequestOptions): Promise<MaintenanceBackupResponse> {
    return requestJson<MaintenanceBackupResponse>("/api/maintenance/backup", {
      method: "POST",
      ...options,
    });
  }

  async function cleanupStorage(options?: ApiRequestOptions): Promise<MaintenanceCleanupResponse> {
    return requestJson<MaintenanceCleanupResponse>("/api/maintenance/cleanup", {
      method: "POST",
      body: JSON.stringify({ confirm: "cleanup" }),
      ...options,
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
    uploadSourceFile,
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
