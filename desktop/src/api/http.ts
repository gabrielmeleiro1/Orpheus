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

export interface ApiRequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

export interface JsonRequestOptions extends Omit<RequestInit, "headers" | "signal">, ApiRequestOptions {
  headers?: HeadersInit;
}

/** Shared transport surface every resource group is built on. */
export interface RequestJson {
  <T>(path: string, init?: JsonRequestOptions): Promise<T>;
}

export interface RequestBlob {
  (path: string, init?: JsonRequestOptions): Promise<Blob>;
}

export interface HttpTransport {
  requestJson: RequestJson;
  requestBlob: RequestBlob;
}

export function resolveUrl(baseUrl: string, path: string): string {
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
    if (parsed.detail && typeof parsed.detail === "object" && !Array.isArray(parsed.detail)) {
      const detail = parsed.detail as { message?: unknown; error?: unknown };
      if (typeof detail.message === "string" && detail.message.trim()) return detail.message.trim();
      const messages: Record<string, string> = {
        invalid_request: "Check your runtime and model selection.",
        packages_not_ready: "Runtime dependencies are missing. Run runtime setup to repair them.",
        probe_unavailable: "The runtime check could not run. Check runtime and try again.",
        probe_failed: "The selected runtime is unavailable. Check its dependencies and device support.",
        assets_missing: "Required model files are missing. Install or repair the selected models.",
        download_in_progress: "A model download is already running. Wait for it to finish or cancel it.",
        setup_in_progress: "Runtime setup is already running. Wait for it to finish.",
        conversion_in_progress: "Wait for the current conversion to finish before changing runtime data.",
        cache_unavailable: "Model storage could not be read. Try again.",
        invalid_model: "Unknown managed model.",
        model_delete_failed: "The model could not be deleted. Close apps using it and try again.",
      };
      return typeof detail.error === "string" && messages[detail.error]
        ? messages[detail.error] : "The request could not be completed. Try again.";
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

export function createHttpTransport(resolvedBaseUrl: string, bearerToken: string): HttpTransport {
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

  return { requestJson, requestBlob };
}
