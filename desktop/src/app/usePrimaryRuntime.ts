import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api/client";
import type { ModelOptionsResponse, RuntimeSetupStatusResponse, SystemCapabilitiesResponse } from "../types/api";
import { runtimeTtsReady } from "./runtimeReadiness";
import {
  DESKTOP_SETTINGS_CHANGED_EVENT,
  readPersistedSettings,
} from "../features/conversion/conversionOptionsShared";
import { buildRuntimeIdentityFromSnapshot, formatRuntimeIdentity, type RuntimeIdentity } from "./runtimeIdentity";
import { getRuntimeBootstrap, updateRuntimeBootstrapCapabilities } from "./runtimeBootstrapCache";
import { startRuntimeCapabilityPolling } from "../features/conversion/runtimeCapabilityPolling";

export function useRuntimeIdentity(apiClient: ApiClient | null, setupStatus: RuntimeSetupStatusResponse | null = null): RuntimeIdentity & { ttsReady: boolean | null } {
  const [identity, setIdentity] = useState<RuntimeIdentity>({ ttsLabel: "—", cleanerLabel: "—" });
  const capabilitiesRef = useRef<SystemCapabilitiesResponse | null>(null);
  const modelOptionsRef = useRef<ModelOptionsResponse | null>(null);

  useEffect(() => {
    if (!apiClient) {
      capabilitiesRef.current = null;
      modelOptionsRef.current = null;
      setIdentity({ ttsLabel: "—", cleanerLabel: "—" });
      return undefined;
    }
    let cancelled = false;
    capabilitiesRef.current = null;
    modelOptionsRef.current = null;
    setIdentity({ ttsLabel: "—", cleanerLabel: "—" });
    const updateIdentity = () => {
      if (cancelled || !modelOptionsRef.current) return;
      setIdentity({
        ...buildRuntimeIdentityFromSnapshot({
          capabilities: capabilitiesRef.current,
          modelOptions: modelOptionsRef.current,
          settings: readPersistedSettings(),
        }),
      });
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
    void getRuntimeBootstrap(apiClient)
      .then(({ capabilities, models: modelOptions }) => {
        if (cancelled) {
          return;
        }
        modelOptionsRef.current = modelOptions;
        if (!poller.pollApplied) {
          capabilitiesRef.current = capabilities;
          poller.noteCapabilities(capabilities);
        }
        updateIdentity();
      })
      .catch(() => {
        if (!cancelled) {
          setIdentity({ ttsLabel: "—", cleanerLabel: "—" });
        }
      });
    return () => {
      cancelled = true;
      poller.stop();
    };
  }, [apiClient]);

  useEffect(() => {
    if (!apiClient) {
      return undefined;
    }
    const onSettingsChanged = () => {
      if (!capabilitiesRef.current || !modelOptionsRef.current) {
        return;
      }
      setIdentity({
        ...buildRuntimeIdentityFromSnapshot({
          capabilities: capabilitiesRef.current,
          modelOptions: modelOptionsRef.current,
          settings: readPersistedSettings(),
        }),
      });
    };
    window.addEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    return () => {
      window.removeEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    };
  }, [apiClient]);

  return { ...identity, ttsReady: apiClient ? runtimeTtsReady(setupStatus) : null };
}

export function useRuntimeIdentityLabel(apiClient: ApiClient | null): string {
  return formatRuntimeIdentity(useRuntimeIdentity(apiClient));
}
