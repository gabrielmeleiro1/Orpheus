import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../types/api";
import {
  DESKTOP_SETTINGS_CHANGED_EVENT,
  readPersistedSettings,
} from "../features/conversion/conversionOptionsShared";
import { buildRuntimeIdentityLabelFromSnapshot } from "./runtimeIdentity";
import { getRuntimeBootstrap } from "./runtimeBootstrapCache";

export function useRuntimeIdentityLabel(apiClient: ApiClient | null): string {
  const [label, setLabel] = useState("TTS: — · Cleaner: —");
  const capabilitiesRef = useRef<SystemCapabilitiesResponse | null>(null);
  const modelOptionsRef = useRef<ModelOptionsResponse | null>(null);

  useEffect(() => {
    if (!apiClient) {
      capabilitiesRef.current = null;
      modelOptionsRef.current = null;
      setLabel("TTS: — · Cleaner: —");
      return undefined;
    }
    let cancelled = false;
    void getRuntimeBootstrap(apiClient)
      .then(({ capabilities, models: modelOptions }) => {
        if (cancelled) {
          return;
        }
        capabilitiesRef.current = capabilities;
        modelOptionsRef.current = modelOptions;
        setLabel(
          buildRuntimeIdentityLabelFromSnapshot({
            capabilities,
            modelOptions,
            settings: readPersistedSettings(),
          }),
        );
      })
      .catch(() => {
        if (!cancelled) {
          setLabel("TTS: — · Cleaner: —");
        }
      });
    return () => {
      cancelled = true;
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
      setLabel(
        buildRuntimeIdentityLabelFromSnapshot({
          capabilities: capabilitiesRef.current,
          modelOptions: modelOptionsRef.current,
          settings: readPersistedSettings(),
        }),
      );
    };
    window.addEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    return () => {
      window.removeEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    };
  }, [apiClient]);

  return label;
}
