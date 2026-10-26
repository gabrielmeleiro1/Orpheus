type ApiErrorCode = "http" | "network" | "timeout" | "aborted" | "invalid-json";

export class ApiError extends Error {
  status: number | null;
  code: ApiErrorCode;
  detail: string;
  serverCode: string | null;
  retriable: boolean;

  constructor({
    message,
    status,
    code,
    detail,
    serverCode = null,
    retriable,
  }: {
    message: string;
    status: number | null;
    code: ApiErrorCode;
    detail: string;
    serverCode?: string | null;
    retriable: boolean;
  }) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.serverCode = serverCode;
    this.retriable = retriable;
  }
}

export interface ApiRequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

interface JsonRequestOptions extends Omit<RequestInit, "headers" | "signal">, ApiRequestOptions {
  headers?: HeadersInit;
}

/** Shared transport surface every resource group is built on. */
export interface RequestJson {
  <T>(path: string, init?: JsonRequestOptions): Promise<T>;
}

export interface RequestBlob {
  (path: string, init?: JsonRequestOptions): Promise<Blob>;
}

interface HttpTransport {
  requestJson: RequestJson;
  requestBlob: RequestBlob;
}

/**
 * Resolve an API path against the backend origin. Absolute URLs are rejected:
 * every request carries the session bearer token, which must never reach any
 * origin other than the local backend.
 */
export function resolveUrl(baseUrl: string, path: string): string {
  if (/^[a-z][a-z\d+.-]*:/i.test(path) || path.startsWith("//")) {
    throw new Error("API paths must be relative to the backend origin");
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
      const message = typeof source.msg === "string"
        ? source.msg.trim().replace(/^Value error,\s*/i, "")
        : "";
      if (!message) {
        return "";
      }
      const location = summarizeValidationLocation(source.loc);
      const field = location.split(".").at(-1)?.replace(/_/g, " ").toLowerCase();
      const selfExplanatory = field && message.toLowerCase().startsWith(`${field} `);
      return location && !selfExplanatory ? `${location}: ${message}` : message;
    })
    .filter((item) => item.length > 0)
    .join("; ");
}

// Structured setup errors carry a stable code; only these have user copy.
const STRUCTURED_ERROR_MESSAGES: ReadonlyMap<string, string> = new Map([
  ["invalid_request", "Check your runtime and model selection."],
  ["packages_not_ready", "Runtime dependencies are missing. Run runtime setup to repair them."],
  ["probe_unavailable", "The runtime check could not run. Check runtime and try again."],
  ["probe_failed", "The selected runtime is unavailable. Check its dependencies and device support."],
  ["assets_missing", "Required model files are missing. Install or repair the selected models."],
  ["download_in_progress", "A model download is already running. Wait for it to finish or cancel it."],
  ["setup_in_progress", "Runtime setup is already running. Wait for it to finish."],
  ["conversion_in_progress", "Wait for the current conversion to finish before changing runtime data."],
  ["cache_unavailable", "Model storage could not be read. Try again."],
  ["invalid_model", "Unknown managed model."],
  ["model_delete_failed", "The model could not be deleted. Close apps using it and try again."],
]);
const STRUCTURED_ERROR_FALLBACK = "The request could not be completed. Try again.";

type JsonObject = Record<string, unknown>;

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function trimmedString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function parseErrorBody(bodyText: string): JsonObject | null {
  try {
    const parsed: unknown = JSON.parse(bodyText);
    return isJsonObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** User-facing text for an HTTP error body; plain-text bodies pass through. */
function errorDetailFromBody(body: JsonObject | null, bodyText: string): string {
  if (body) {
    const detail = body.detail;
    if (trimmedString(detail)) {
      return trimmedString(detail);
    }
    if (isJsonObject(detail)) {
      // Unknown structured codes get generic copy rather than leaking fields.
      return trimmedString(detail.message)
        || STRUCTURED_ERROR_MESSAGES.get(trimmedString(detail.error))
        || STRUCTURED_ERROR_FALLBACK;
    }
    const validationSummary = summarizeValidationDetails(detail);
    if (validationSummary) {
      return validationSummary;
    }
    if (trimmedString(body.message)) {
      return trimmedString(body.message);
    }
  }
  return bodyText.trim();
}

function serverCodeFromBody(body: JsonObject | null): string | null {
  const detail = body?.detail;
  if (!isJsonObject(detail)) {
    return null;
  }
  if (typeof detail.code === "string") {
    return detail.code;
  }
  return typeof detail.error === "string" ? detail.error : null;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function createHttpError(status: number, bodyText: string): ApiError {
  const body = parseErrorBody(bodyText);
  const detail = errorDetailFromBody(body, bodyText);
  const serverCode = serverCodeFromBody(body);
  return new ApiError({
    // Users read the backend's sentence; status and code stay on the error.
    message: detail || `The backend could not complete the request (error ${status}).`,
    status,
    code: "http",
    detail,
    serverCode,
    retriable: status === 408 || status === 429 || status >= 500,
  });
}

export function createRequestTimeoutError(): ApiError {
  return new ApiError({
    message: "request timed out",
    status: null,
    code: "timeout",
    detail: "request timed out",
    retriable: true,
  });
}

function createRequestFailure(error: unknown, timedOut: boolean): ApiError {
  if (timedOut) {
    return createRequestTimeoutError();
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

async function fetchWithRequestOptions<T>(
  input: RequestInfo | URL,
  init: JsonRequestOptions,
  headers: Headers,
  consume: (response: Response) => Promise<T>,
): Promise<T> {
  const { timeoutMs, signal, ...fetchInit } = init;
  const timeout = Number(timeoutMs);

  const controller = new AbortController();
  let timedOut = false;
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const abortFromCaller = () => {
    if (controller.signal.aborted) return;
    // Operation budgets use the same reason as AbortSignal.timeout().
    timedOut = signal?.reason instanceof DOMException && signal.reason.name === "TimeoutError";
    controller.abort();
  };
  const shouldUseComposedSignal = Boolean(signal) || (Number.isFinite(timeout) && timeout > 0);

  if (shouldUseComposedSignal) {
    if (signal?.aborted) {
      abortFromCaller();
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
    let response: Response;
    try {
      response = await fetch(input, {
        ...fetchInit,
        headers,
        ...(shouldUseComposedSignal ? { signal: controller.signal } : {}),
      });
    } catch (error: unknown) {
      throw createRequestFailure(error, timedOut);
    }
    try {
      return await consume(response);
    } catch (error: unknown) {
      if (error instanceof ApiError) {
        throw error;
      }
      // Anything else failed while reading the body: the connection dropped
      // after the headers arrived, which callers may retry.
      throw createRequestFailure(error, timedOut);
    }
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

    return fetchWithRequestOptions(resolveUrl(resolvedBaseUrl, path), init, headers, async (response) => {
      if (!response.ok) {
        const bodyText = await response.text();
        throw createHttpError(response.status, bodyText);
      }

      try {
        return (await response.json()) as T;
      } catch (error: unknown) {
        // Only a parse failure means the body was malformed; a body that could
        // not be read is a transport failure, classified by the caller.
        if (!(error instanceof SyntaxError)) {
          throw error;
        }
        throw new ApiError({
          message: "invalid JSON response",
          status: null,
          code: "invalid-json",
          detail: "invalid JSON response",
          retriable: false,
        });
      }
    });
  }

  async function requestBlob(path: string, init: JsonRequestOptions = {}): Promise<Blob> {
    const headers = createHeaders(init, bearerToken, null);

    return fetchWithRequestOptions(resolveUrl(resolvedBaseUrl, path), init, headers, async (response) => {
      if (!response.ok) {
        const bodyText = await response.text();
        throw createHttpError(response.status, bodyText);
      }

      return response.blob();
    });
  }

  return { requestJson, requestBlob };
}
