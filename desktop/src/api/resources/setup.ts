import type { RequestJson, ApiRequestOptions } from "../http";
import type {
  RuntimeModelDownloadRequest,
  RuntimeModelDownloadStatus,
  RuntimeSetupAdoptRequest,
  RuntimeSetupRuntimeDeleteResponse,
  RuntimeSetupStatusResponse,
} from "../../types/api";

export function createSetupApi(requestJson: RequestJson) {
  async function getRuntimeSetupStatus(options?: ApiRequestOptions): Promise<RuntimeSetupStatusResponse> {
    return requestJson<RuntimeSetupStatusResponse>("/api/setup/status", options);
  }

  async function adoptRuntimeSetup(
    request: RuntimeSetupAdoptRequest,
    options?: ApiRequestOptions,
  ): Promise<RuntimeSetupStatusResponse> {
    return requestJson<RuntimeSetupStatusResponse>("/api/setup/adopt", {
      method: "POST",
      body: JSON.stringify(request),
      ...options,
    });
  }

  async function getRuntimeModelDownloadStatus(
    options?: ApiRequestOptions,
  ): Promise<RuntimeModelDownloadStatus> {
    return requestJson<RuntimeModelDownloadStatus>("/api/setup/models/download", options);
  }

  async function startRuntimeModelDownload(
    request: RuntimeModelDownloadRequest,
    options?: ApiRequestOptions,
  ): Promise<RuntimeModelDownloadStatus> {
    return requestJson<RuntimeModelDownloadStatus>("/api/setup/models/download", {
      method: "POST",
      body: JSON.stringify(request),
      ...options,
    });
  }

  async function cancelRuntimeModelDownload(
    options?: ApiRequestOptions,
  ): Promise<RuntimeModelDownloadStatus> {
    return requestJson<RuntimeModelDownloadStatus>("/api/setup/models/download/cancel", {
      method: "POST",
      ...options,
    });
  }

  async function deleteRuntimeSetupData(
    options?: ApiRequestOptions,
  ): Promise<RuntimeSetupRuntimeDeleteResponse> {
    return requestJson<RuntimeSetupRuntimeDeleteResponse>("/api/setup/runtime", {
      method: "DELETE",
      ...options,
    });
  }

  return {
    getRuntimeSetupStatus,
    adoptRuntimeSetup,
    getRuntimeModelDownloadStatus,
    startRuntimeModelDownload,
    cancelRuntimeModelDownload,
    deleteRuntimeSetupData,
  };
}
