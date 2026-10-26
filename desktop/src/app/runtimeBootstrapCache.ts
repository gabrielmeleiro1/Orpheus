import type { ApiClient } from "../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../types/api";

interface RuntimeBootstrap {
  models: ModelOptionsResponse;
  capabilities: SystemCapabilitiesResponse;
}

// The catalog and cached capability snapshot do not wait for runtime probes.
// Evict stalled reads so the next bootstrap consumer can try again.
const RUNTIME_BOOTSTRAP_REQUEST_TIMEOUT_MS = 8_000;

const bootstrapByClient = new WeakMap<ApiClient, Promise<RuntimeBootstrap>>();

export function invalidateRuntimeBootstrap(apiClient: ApiClient): void {
  bootstrapByClient.delete(apiClient);
}

export function getRuntimeBootstrap(apiClient: ApiClient): Promise<RuntimeBootstrap> {
  const existing = bootstrapByClient.get(apiClient);
  if (existing) {
    return existing;
  }

  const request = Promise.all([
    apiClient.getModelOptions({ timeoutMs: RUNTIME_BOOTSTRAP_REQUEST_TIMEOUT_MS }),
    apiClient.getSystemCapabilities({ timeoutMs: RUNTIME_BOOTSTRAP_REQUEST_TIMEOUT_MS }),
  ])
    .then(([models, capabilities]) => ({ models, capabilities }))
    .catch((error: unknown) => {
      if (bootstrapByClient.get(apiClient) === request) bootstrapByClient.delete(apiClient);
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
  const updated = existing.then((bootstrap) => ({
    models: bootstrap.models,
    capabilities,
  })).catch((error: unknown) => {
    if (bootstrapByClient.get(apiClient) === updated) bootstrapByClient.delete(apiClient);
    throw error;
  });
  bootstrapByClient.set(apiClient, updated);
  // A live capability update may have no consumer awaiting its derived promise.
  // Keep rejection observable to later readers without an unhandled rejection.
  void updated.catch(() => {});
}
