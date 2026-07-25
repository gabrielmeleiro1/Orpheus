import type { ApiClient } from "../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../types/api";

export interface RuntimeBootstrap {
  models: ModelOptionsResponse;
  capabilities: SystemCapabilitiesResponse;
}

const bootstrapByClient = new WeakMap<ApiClient, Promise<RuntimeBootstrap>>();

export function getRuntimeBootstrap(apiClient: ApiClient): Promise<RuntimeBootstrap> {
  const existing = bootstrapByClient.get(apiClient);
  if (existing) {
    return existing;
  }

  const request = Promise.all([
    apiClient.getModelOptions(),
    apiClient.getSystemCapabilities(),
  ])
    .then(([models, capabilities]) => ({ models, capabilities }))
    .catch((error: unknown) => {
      bootstrapByClient.delete(apiClient);
      throw error;
    });
  bootstrapByClient.set(apiClient, request);
  return request;
}

/**
 * Replace the cached capabilities snapshot for this client while keeping the
 * already-fetched model catalog. Used when a live Settings refresh observes a
 * newer runtime probe than the dashboard-pinned bootstrap.
 */
export function updateRuntimeBootstrapCapabilities(
  apiClient: ApiClient,
  capabilities: SystemCapabilitiesResponse,
): void {
  const existing = bootstrapByClient.get(apiClient);
  if (!existing) {
    return;
  }
  bootstrapByClient.set(
    apiClient,
    existing.then((bootstrap) => ({
      models: bootstrap.models,
      capabilities,
    })),
  );
}
