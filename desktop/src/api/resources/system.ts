import type { RequestJson, ApiRequestOptions } from "../http";
import type {
  HealthResponse,
  ModelOptionsResponse,
  OpenAIModelListResponse,
  SystemCapabilitiesResponse,
  VoiceOptionsResponse,
} from "../../types/api";

export function createSystemApi(requestJson: RequestJson) {
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

  return {
    getHealth,
    getModelOptions,
    fetchOpenAiModelIds,
    getVoiceOptions,
    getSystemCapabilities,
  };
}
