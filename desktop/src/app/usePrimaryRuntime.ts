import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../types/api";
import {
  DESKTOP_SETTINGS_CHANGED_EVENT,
  readPersistedSettings,
} from "../features/conversion/conversionOptionsShared";
import { buildRuntimeIdentityFromSnapshot, formatRuntimeIdentity, type RuntimeIdentity } from "./runtimeIdentity";
import { getRuntimeBootstrap, updateRuntimeBootstrapCapabilities } from "./runtimeBootstrapCache";
import { startRuntimeCapabilityPolling } from "../features/conversion/runtimeCapabilityPolling";

function runtimeReadiness(capabilities: SystemCapabilitiesResponse | null): boolean | null {
  const probe = capabilities?.runtime_probe;
  if (!probe || probe.refresh_in_progress || probe.state === "pending" || probe.state === "stale") return null;
  return probe.tts?.available ?? null;
}

export function useRuntimeIdentity(apiClient: ApiClient | null): RuntimeIdentity & { ttsReady: boolean | null } {
  const [identity, setIdentity] = useState<RuntimeIdentity & { ttsReady: boolean | null }>({ ttsLabel: "—", cleanerLabel: "—", ttsReady: null });
  const capabilitiesRef = useRef<SystemCapabilitiesResponse | null>(null);
  const modelOptionsRef = useRef<ModelOptionsResponse | null>(null);

  useEffect(() => {
    if (!apiClient) {
      capabilitiesRef.current = null;
      modelOptionsRef.current = null;
      setIdentity({ ttsLabel: "—", cleanerLabel: "—", ttsReady: null });
      return undefined;
    }
    let cancelled = false;
    capabilitiesRef.current = null;
    modelOptionsRef.current = null;
    setIdentity({ ttsLabel: "—", cleanerLabel: "—", ttsReady: null });
    const updateIdentity = () => {
      if (cancelled || !modelOptionsRef.current) return;
      setIdentity({
        ...buildRuntimeIdentityFromSnapshot({
          capabilities: capabilitiesRef.current,
          modelOptions: modelOptionsRef.current,
          settings: readPersistedSettings(),
        }),
        ttsReady: runtimeReadiness(capabilitiesRef.current),
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
          setIdentity({ ttsLabel: "—", cleanerLabel: "—", ttsReady: null });
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
        ttsReady: runtimeReadiness(capabilitiesRef.current),
      });
    };
    window.addEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    return () => {
      window.removeEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    };
  }, [apiClient]);

  return identity;
}

export function useRuntimeIdentityLabel(apiClient: ApiClient | null): string {
  return formatRuntimeIdentity(useRuntimeIdentity(apiClient));
}
