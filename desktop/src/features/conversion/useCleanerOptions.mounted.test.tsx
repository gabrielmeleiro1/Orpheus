// @vitest-environment happy-dom

import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ApiClient } from "../../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../../types/api";
import { useCleanerOptions, type UseCleanerOptionsResult } from "./useCleanerOptions";
import type { PersistedSettings } from "./conversionOptionsShared";

function capabilities(
  mlxAvailable: boolean,
  probeState: SystemCapabilitiesResponse["runtime_probe"]["state"] = mlxAvailable ? "fresh" : "pending",
): SystemCapabilitiesResponse {
  return {
    default_tts_engine: "kokoro-mlx",
    default_tts_quantization: "bf16",
    cleaner_runtime_availability: { mlx: mlxAvailable, openai: true },
    mlx_runtime_available: mlxAvailable,
    runtime_probe: {
      state: probeState,
      refresh_in_progress: !mlxAvailable,
    },
  };
}

const modelOptions: ModelOptionsResponse = {
  tts_engines: [],
  cleaner_models: ["local-model"],
  api_cleaner_models: ["gpt-4o-mini"],
  cleaner_backend_options: [
    {
      name: "openai",
      display_name: "Cloud API",
      is_local: false,
      requires_api_key: true,
      models: ["gpt-4o-mini"],
    },
    {
      name: "mlx",
      display_name: "Local MLX",
      is_local: true,
      requires_api_key: false,
      models: ["local-model"],
    },
  ],
};

function CleanerProbe({
  apiClient,
  systemCapabilities,
  persisted = {},
}: {
  apiClient: ApiClient;
  systemCapabilities: SystemCapabilitiesResponse | null;
  persisted?: PersistedSettings;
}): null {
  const persistedSettingsRef = useRef(persisted);
  const result = useCleanerOptions({
    apiClient,
    modelOptions,
    systemCapabilities,
    persistedSettingsRef,
  });
  (globalThis as typeof globalThis & { cleanerProbe?: UseCleanerOptionsResult }).cleanerProbe = result;
  return null;
}

function getCleanerProbe(): UseCleanerOptionsResult {
  return (globalThis as typeof globalThis & { cleanerProbe: UseCleanerOptionsResult }).cleanerProbe;
}

describe("useCleanerOptions capability transitions", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => {
      root.unmount();
    });
    container.remove();
    delete (globalThis as typeof globalThis & { cleanerProbe?: UseCleanerOptionsResult }).cleanerProbe;
  });

  it("promotes Local MLX once a pending probe reports the local runtime ready", async () => {
    const apiClient = {} as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <CleanerProbe apiClient={apiClient} systemCapabilities={capabilities(false, "pending")} />,
      );
    });

    expect(getCleanerProbe().cleanerBackendOptions.map((option) => option.label)).toEqual(["Cloud API"]);
    expect(getCleanerProbe().cleanerBackend).toBe("openai");
    expect(getCleanerProbe().cleanerBackendConfigured).toBe(false);

    await act(async () => {
      root.render(
        <CleanerProbe apiClient={apiClient} systemCapabilities={capabilities(true, "fresh")} />,
      );
    });

    expect(getCleanerProbe().cleanerBackendOptions.map((option) => option.label)).toEqual([
      "Cloud API",
      "Local MLX",
    ]);
    expect(getCleanerProbe().cleanerBackend).toBe("mlx");
    expect(getCleanerProbe().cleanerBackendConfigured).toBe(false);
  });

  it("preserves an explicitly configured Cloud API choice after local runtime becomes ready", async () => {
    const apiClient = {} as ApiClient;
    const persisted: PersistedSettings = {
      cleaner_backend: "openai",
      cleaner_backend_configured: true,
    };

    await act(async () => {
      root = createRoot(container);
      root.render(
        <CleanerProbe
          apiClient={apiClient}
          systemCapabilities={capabilities(false, "pending")}
          persisted={persisted}
        />,
      );
    });
    expect(getCleanerProbe().cleanerBackend).toBe("openai");
    expect(getCleanerProbe().cleanerBackendConfigured).toBe(true);

    await act(async () => {
      root.render(
        <CleanerProbe
          apiClient={apiClient}
          systemCapabilities={capabilities(true, "fresh")}
          persisted={persisted}
        />,
      );
    });

    expect(getCleanerProbe().cleanerBackend).toBe("openai");
    expect(getCleanerProbe().cleanerBackendConfigured).toBe(true);
    expect(getCleanerProbe().cleanerBackendOptions.some((option) => option.value === "mlx")).toBe(true);
  });
});
