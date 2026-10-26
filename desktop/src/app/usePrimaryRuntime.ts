import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api/client";
import type { ModelOptionsResponse, RuntimeSetupStatusResponse, SystemCapabilitiesResponse } from "../types/api";
import { runtimeTtsReady } from "./runtimeReadiness";
import {
  DESKTOP_SETTINGS_CHANGED_EVENT,
  readPersistedSettings,
} from "../features/conversion/conversionOptionsShared";
import { buildRuntimeIdentityFromSnapshot, type RuntimeIdentity } from "./runtimeIdentity";
import { getRuntimeBootstrap, updateRuntimeBootstrapCapabilities } from "./runtimeBootstrapCache";
import { startRuntimeCapabilityPolling } from "../features/conversion/runtimeCapabilityPolling";

// Capability polls repeat while the runtime warms up, and this hook's host is
// the whole dashboard: an unchanged identity must not re-render it.
function retainEqualIdentity(next: RuntimeIdentity) {
  return (current: RuntimeIdentity) => (
    current.ttsLabel === next.ttsLabel && current.cleanerLabel === next.cleanerLabel ? current : next
  );
}

export function useRuntimeIdentity(apiClient: ApiClient | null, setupStatus: RuntimeSetupStatusResponse | null = null): RuntimeIdentity & { ttsReady: boolean | null } {
  const [identity, setIdentity] = useState<RuntimeIdentity>({ ttsLabel: "—", cleanerLabel: "—" });
  const capabilitiesRef = useRef<SystemCapabilitiesResponse | null>(null);
  const modelOptionsRef = useRef<ModelOptionsResponse | null>(null);

  useEffect(() => {
    if (!apiClient) {
      capabilitiesRef.current = null;
      modelOptionsRef.current = null;
      setIdentity(retainEqualIdentity({ ttsLabel: "—", cleanerLabel: "—" }));
      return undefined;
    }
    let cancelled = false;
    capabilitiesRef.current = null;
    modelOptionsRef.current = null;
    setIdentity(retainEqualIdentity({ ttsLabel: "—", cleanerLabel: "—" }));
    const updateIdentity = () => {
      if (cancelled || !modelOptionsRef.current) return;
      setIdentity(retainEqualIdentity(buildRuntimeIdentityFromSnapshot({
        capabilities: capabilitiesRef.current,
        modelOptions: modelOptionsRef.current,
        settings: readPersistedSettings(),
      })));
    };
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => apiClient.getSystemCapabilities({ signal }),
      onCapabilities: (capabilities) => {
        if (cancelled) return;
        capabilitiesRef.current = capabilities;
        updateRuntimeBootstrapCapabilities(apiClient, capabilities);
        updateIdentity();
      },
    });
    let bootstrapPending = false;
    const loadBootstrap = async () => {
      if (bootstrapPending) return;
      bootstrapPending = true;
      try {
        const { capabilities, models: modelOptions } = await getRuntimeBootstrap(apiClient);
        if (cancelled) return;
        modelOptionsRef.current = modelOptions;
        if (!poller.pollApplied) {
          capabilitiesRef.current = capabilities;
          poller.noteCapabilities(capabilities);
        }
        updateIdentity();
      } catch {
        if (!cancelled) {
          setIdentity(retainEqualIdentity({ ttsLabel: "—", cleanerLabel: "—" }));
        }
      } finally {
        bootstrapPending = false;
      }
    };
    void loadBootstrap();
    const onSettingsChanged = () => {
      if (!capabilitiesRef.current || !modelOptionsRef.current) {
        void loadBootstrap();
      } else {
        updateIdentity();
      }
    };
    window.addEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    return () => {
      cancelled = true;
      poller.stop();
      window.removeEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    };
  }, [apiClient]);

  return { ...identity, ttsReady: apiClient ? runtimeTtsReady(setupStatus) : null };
}
