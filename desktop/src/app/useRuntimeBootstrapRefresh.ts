import { useEffect } from "react";

import type { ApiClient } from "../api/client";
import { getRuntimeBootstrap, updateRuntimeBootstrapCapabilities } from "./runtimeBootstrapCache";
import { subscribeRuntimeCapabilities } from "./runtimeCapabilityHub";

/**
 * Pins the runtime bootstrap cache for this client and, while the runtime
 * probe warms up, keeps its capabilities fresh so Create opens on live ones.
 * The poller is shared with any other consumer of the same client.
 */
export function useRuntimeBootstrapRefresh(apiClient: ApiClient | null): void {
  useEffect(() => {
    if (!apiClient) return undefined;
    let cancelled = false;
    const subscription = subscribeRuntimeCapabilities(apiClient, {
      onCapabilities: (capabilities) => {
        if (!cancelled) updateRuntimeBootstrapCapabilities(apiClient, capabilities);
      },
    });
    getRuntimeBootstrap(apiClient).then(({ capabilities }) => {
      if (!cancelled && !subscription.pollApplied) subscription.noteCapabilities(capabilities);
    }, () => undefined);
    return () => {
      cancelled = true;
      subscription.unsubscribe();
    };
  }, [apiClient]);
}
