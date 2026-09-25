// @vitest-environment happy-dom

import { act, useRef, type MutableRefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ApiClient } from "../../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../../types/api";
import { useCleanerOptions, type UseCleanerOptionsResult } from "./useCleanerOptions";
import { syncSetupPreferences } from "../setup/setupPreferences";
import { readPersistedSettings } from "./conversionOptionsShared";
import { buildCleanerRuntimePayloadFields } from "./cleanerBackendHelpers";
import { buildRuntimeIdentityFromSnapshot } from "../../app/runtimeIdentity";
import type { RuntimeSetupStatusResponse } from "../../types/api";
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
  persistedRef,
  models = modelOptions,
}: {
  apiClient: ApiClient;
  systemCapabilities: SystemCapabilitiesResponse | null;
  persisted?: PersistedSettings;
  persistedRef?: MutableRefObject<PersistedSettings>;
  models?: ModelOptionsResponse;
}): null {
  const ownedPersistedSettingsRef = useRef(persisted);
  const persistedSettingsRef = persistedRef ?? ownedPersistedSettingsRef;
  const result = useCleanerOptions({
    apiClient,
    modelOptions: models,
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

  it.each([undefined, "", "   ", "cuda:1"])("uses the backend default for an empty persisted CUDA device (%s)", async (device) => {
    const models = { ...modelOptions, cleaner_backend_options: [
      { name: "cuda", display_name: "Local CUDA", is_local: true, requires_api_key: false, models: ["local-model"], default_device: "cuda" },
    ] };
    const persisted = { cleaner_backend_configured: true, cleaner_backend: "cuda", cleaner_cuda_device: device };
    root = createRoot(container);
    await act(async () => root.render(<CleanerProbe apiClient={{} as ApiClient} systemCapabilities={null} persisted={persisted} models={models} />));
    expect(getCleanerProbe().cleanerCudaDevice).toBe(device?.trim() || "cuda");
    await act(async () => getCleanerProbe().setCleanerCudaDevice(""));
    expect(getCleanerProbe().cleanerCudaDevice).toBe("cuda");
  });

  it("defaults a fresh CUDA profile when older backend metadata omits the device", async () => {
    const models = { ...modelOptions, cleaner_backend_options: [
      { name: "cuda", display_name: "Local CUDA", is_local: true, requires_api_key: false, models: ["local-model"] },
    ] };
    root = createRoot(container);
    await act(async () => root.render(<CleanerProbe apiClient={{} as ApiClient} systemCapabilities={null} models={models} />));
    expect(getCleanerProbe().cleanerCudaDevice).toBe("cuda");
  });

  it("keeps fresh-profile no-cleaner setup through probe refresh and sends basic by default", async () => {
    localStorage.clear();
    syncSetupPreferences({ configured: true, selected_profile: "cuda", selected_model_groups: [{ id: "tts-cpu-cuda" }] } as RuntimeSetupStatusResponse);
    const persisted = readPersistedSettings();
    const models = { ...modelOptions, cleaner_backend_options: [
      ...modelOptions.cleaner_backend_options!,
      { name: "basic", display_name: "Basic cleanup", is_local: true, requires_api_key: false, requires_model: false, models: [] },
      { name: "cuda", display_name: "Local CUDA", is_local: true, requires_api_key: false, requires_model: true, models: ["Qwen/Qwen2.5-1.5B-Instruct"] },
    ] };
    const apiClient = {} as ApiClient;
    const pending = { ...capabilities(false), cleaner_runtime_availability: { basic: true, cuda: false, mlx: false } };
    const fresh = { ...capabilities(true), cleaner_runtime_availability: { basic: true, cuda: true, mlx: true } };
    root = createRoot(container);
    await act(async () => root.render(<CleanerProbe apiClient={apiClient} systemCapabilities={pending} persisted={persisted} models={models} />));
    expect(getCleanerProbe().cleanerBackend).toBe("basic");
    expect(getCleanerProbe().cleanerBackendOptions.some((option) => option.value === "cuda")).toBe(false);
    await act(async () => root.render(<CleanerProbe apiClient={apiClient} systemCapabilities={fresh} persisted={persisted} models={models} />));
    const options = getCleanerProbe();
    expect(options.cleanerBackend).toBe("basic");
    expect(options.cleanerModel).toBe("");
    expect(buildCleanerRuntimePayloadFields({ ...options, selectedBackend: options.selectedCleanerBackend })).toEqual({ cleaner_backend: "basic", cleaner_model: null });
    expect(buildRuntimeIdentityFromSnapshot({ capabilities: fresh, modelOptions: models, settings: persisted }).cleanerLabel).toBe("Basic cleanup");
    await act(async () => options.setCleanerBackend("cuda"));
    expect(getCleanerProbe().cleanerBackend).toBe("cuda");
    localStorage.clear();
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

  it("migrates a legacy Local MLX preference and restores it after startup probing", async () => {
    const apiClient = {} as ApiClient;
    const persistedRef: MutableRefObject<PersistedSettings> = {
      current: {
        cleaner_backend: "mlx",
        cleaner_backend_configured: true,
        cleaner_model: "local-model",
      },
    };

    await act(async () => {
      root = createRoot(container);
      root.render(
        <CleanerProbe
          apiClient={apiClient}
          systemCapabilities={capabilities(false, "pending")}
          persistedRef={persistedRef}
        />,
      );
    });

    expect(getCleanerProbe().cleanerBackend).toBe("openai");
    expect(persistedRef.current.preferred_cleaner_backend).toBe("mlx");
    expect(persistedRef.current.preferred_cleaner_model).toBe("local-model");

    await act(async () => {
      root.render(
        <CleanerProbe
          apiClient={apiClient}
          systemCapabilities={capabilities(true, "fresh")}
          persistedRef={persistedRef}
        />,
      );
    });

    expect(getCleanerProbe().cleanerBackend).toBe("mlx");
    expect(getCleanerProbe().cleanerModel).toBe("local-model");
  });

  it("updates the persisted backend preference on an explicit user switch", async () => {
    const apiClient = {} as ApiClient;
    const persistedRef: MutableRefObject<PersistedSettings> = {
      current: {
        cleaner_backend: "mlx",
        preferred_cleaner_backend: "mlx",
        cleaner_backend_configured: true,
      },
    };

    await act(async () => {
      root = createRoot(container);
      root.render(
        <CleanerProbe
          apiClient={apiClient}
          systemCapabilities={capabilities(true, "fresh")}
          persistedRef={persistedRef}
        />,
      );
    });

    await act(async () => getCleanerProbe().setCleanerBackend("openai"));

    expect(persistedRef.current.preferred_cleaner_backend).toBe("openai");
  });
});
