import { useSyncExternalStore } from "react";

import type { ParallelWorkerRecommendation, SystemCapabilitiesResponse } from "../../types/api";

/** The newest capability snapshot, including the values that change on every poll. */
export interface LiveCapabilities {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => SystemCapabilitiesResponse | null;
  publish: (next: SystemCapabilitiesResponse | null) => void;
}

export function createLiveCapabilities(): LiveCapabilities {
  let snapshot: SystemCapabilitiesResponse | null = null;
  const listeners = new Set<() => void>();
  return {
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getSnapshot: () => snapshot,
    publish: (next) => {
      if (next === snapshot) return;
      snapshot = next;
      for (const listener of [...listeners]) listener();
    },
  };
}

const NO_LIVE_CAPABILITIES = createLiveCapabilities();

/** Re-renders only the calling component when a poll brings new memory readings. */
export function useLiveWorkerRecommendation(
  liveCapabilities: LiveCapabilities | undefined,
): ParallelWorkerRecommendation | null {
  const store = liveCapabilities ?? NO_LIVE_CAPABILITIES;
  return useSyncExternalStore(
    store.subscribe,
    () => store.getSnapshot()?.parallel_worker_recommendation ?? null,
  );
}
