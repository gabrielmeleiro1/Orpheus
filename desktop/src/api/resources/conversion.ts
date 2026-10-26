import type { RequestJson, ApiRequestOptions } from "../http";
import { normalizeBookId } from "../validation";
import type {
  ActiveConversionResponse,
  CancelConversionResponse,
  RepairConversionRequest,
  SourcePreflight,
  SourceTextUploadRequest,
  SourceUploadResponse,
  StartConversionRequest,
  StartConversionResponse,
  VoiceSuggestionResponse,
} from "../../types/api";

// The first start after launch runs a blocking voice-runtime check (load the
// model and synthesize a probe) before replying; that took ~26 s cold on a
// Windows CUDA machine. A shorter budget turns every first start into an
// ambiguous one that must be reconciled.
export const CONVERSION_START_TIMEOUT_MS = 90_000;
export const SOURCE_PREFLIGHT_TIMEOUT_MS = 30_000;

export function createConversionApi(requestJson: RequestJson) {
  async function preflightSource(sourcePath: string, options?: ApiRequestOptions): Promise<SourcePreflight> {
    return requestJson<SourcePreflight>("/api/sources/preflight", {
      method: "POST",
      body: JSON.stringify({ source_path: sourcePath }),
      timeoutMs: SOURCE_PREFLIGHT_TIMEOUT_MS,
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

  async function deleteUpload(handle: string, options?: ApiRequestOptions): Promise<{ deleted: boolean }> {
    return requestJson<{ deleted: boolean }>(`/api/uploads/${encodeURIComponent(handle)}`, {
      method: "DELETE",
      ...options,
    });
  }

  async function suggestVoice(
    sourcePath: string,
    engine?: string | null,
    options?: ApiRequestOptions,
    settings?: Pick<StartConversionRequest, "cleaner_backend" | "cleaner_api_model" | "cleaner_api_key">,
  ): Promise<VoiceSuggestionResponse> {
    return requestJson<VoiceSuggestionResponse>("/api/voices/suggest", {
      method: "POST",
      body: JSON.stringify({
        source_path: sourcePath,
        tts_engine: engine || null,
        ...settings,
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
      timeoutMs: CONVERSION_START_TIMEOUT_MS,
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

  return {
    preflightSource,
    uploadSourceFile,
    uploadSourceText,
    deleteUpload,
    suggestVoice,
    startConversion,
    repairConversion,
    getActiveConversion,
    cancelConversion,
  };
}
