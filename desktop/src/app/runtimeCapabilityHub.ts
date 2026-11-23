import type { ApiClient } from "../api/client";
import type { ApiError } from "../api/http";
import { startRuntimeCapabilityPolling } from "../features/conversion/runtimeCapabilityPolling";
import type { SystemCapabilitiesResponse } from "../types/api";

interface RuntimeCapabilitySubscriber {
  onCapabilities: (capabilities: SystemCapabilitiesResponse) => void;
  onError?: (error: ApiError) => void;
}

export interface RuntimeCapabilitySubscription {
  /** Record a snapshot this consumer obtained elsewhere (for example the bootstrap). */
  noteCapabilities: (capabilities: SystemCapabilitiesResponse | null | undefined) => void;
  /** True once a live poll response reached this subscription. */
  readonly pollApplied: boolean;
  unsubscribe: () => void;
}

interface SharedCapabilityPoller {
  poller: ReturnType<typeof startRuntimeCapabilityPolling>;
  latest: SystemCapabilitiesResponse | null;
  subscribers: Map<RuntimeCapabilitySubscriber, { applied: boolean }>;
}

const capabilityPollerByClient = new WeakMap<ApiClient, SharedCapabilityPoller>();

/**
 * One capability poller per client, shared by every consumer. Each new
 * subscription restarts it with a full budget, as a dedicated poller had, seeded
 * with the newest snapshot so a settled runtime is not polled again. Polling
 * stops when the probe settles, the budget runs out, or the last consumer leaves.
 */
export function subscribeRuntimeCapabilities(
  apiClient: ApiClient,
  subscriber: RuntimeCapabilitySubscriber,
): RuntimeCapabilitySubscription {
  const previous = capabilityPollerByClient.get(apiClient);
  previous?.poller.stop();
  const shared: SharedCapabilityPoller = {
    latest: previous?.latest ?? null,
    subscribers: previous?.subscribers ?? new Map(),
    poller: startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => apiClient.getSystemCapabilities({ signal }),
      onCapabilities: (capabilities) => {
        shared.latest = capabilities;
        for (const [listener, state] of [...shared.subscribers]) {
          state.applied = true;
          listener.onCapabilities(capabilities);
        }
      },
      onError: (error) => {
        for (const listener of [...shared.subscribers.keys()]) listener.onError?.(error);
      },
    }),
  };
  const state = { applied: false };
  shared.subscribers.set(subscriber, state);
  capabilityPollerByClient.set(apiClient, shared);
  if (shared.latest) shared.poller.noteCapabilities(shared.latest);

  let active = true;
  return {
    noteCapabilities: (capabilities) => {
      const current = capabilityPollerByClient.get(apiClient);
      if (!active || !current) return;
      if (capabilities) current.latest = capabilities;
      current.poller.noteCapabilities(capabilities);
    },
    get pollApplied() {
      return state.applied;
    },
    unsubscribe: () => {
      if (!active) return;
      active = false;
      const current = capabilityPollerByClient.get(apiClient);
      if (!current) return;
      current.subscribers.delete(subscriber);
      if (current.subscribers.size === 0) {
        current.poller.stop();
        capabilityPollerByClient.delete(apiClient);
      }
    },
  };
}
