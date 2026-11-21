import { useEffect } from "react";

import type { ApiClient } from "../api/client";
import { getRuntimeBootstrap, updateRuntimeBootstrapCapabilities } from "./runtimeBootstrapCache";
import { startRuntimeCapabilityPolling } from "../features/conversion/runtimeCapabilityPolling";

/**
 * Pins the runtime bootstrap cache for this client and, while the runtime
 * probe warms up, keeps its capabilities fresh so Create opens on live ones.
 */
export function useRuntimeBootstrapRefresh(apiClient: ApiClient | null): void {
  useEffect(() => {
    if (!apiClient) return undefined;
    let cancelled = false;
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => apiClient.getSystemCapabilities({ signal }),
      onCapabilities: (capabilities) => {
        if (!cancelled) updateRuntimeBootstrapCapabilities(apiClient, capabilities);
      },
    });
    getRuntimeBootstrap(apiClient).then(({ capabilities }) => {
      if (!cancelled && !poller.pollApplied) poller.noteCapabilities(capabilities);
    }, () => undefined);
    return () => {
      cancelled = true;
      poller.stop();
    };
  }, [apiClient]);
}
