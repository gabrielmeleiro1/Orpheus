import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../types/api";
import {
  DESKTOP_SETTINGS_CHANGED_EVENT,
  savePersistedSettings,
} from "../features/conversion/conversionOptionsShared";
import {
  buildRuntimeIdentityLabelFromSnapshot,
  formatRuntimeIdentity,
  resolveRuntimeIdentity,
} from "./runtimeIdentity";

const SETTINGS_KEY = "orpheus.desktop.settings.v1";

const capabilities: SystemCapabilitiesResponse = {
  default_tts_engine: "kokoro",
  default_tts_quantization: "",
  cleaner_runtime_availability: {},
  runtime_probe: {
    state: "fresh",
    refresh_in_progress: false,
    tts: { available: true, latency_ms: 1, error: null },
  },
  primary_runtime: "MLX",
};

const modelOptions = {
  cleaner_backend_options: [
    {
      name: "openai",
      display_name: "Cloud API",
      is_local: false,
      requires_api_key: true,
      models: [],
    },
    {
      name: "mlx",
      display_name: "Local MLX",
      is_local: true,
      requires_api_key: false,
      models: [],
    },
  ],
  tts_engines: [],
} as unknown as ModelOptionsResponse;

function installWindowStub() {
  const store = new Map<string, string>();
  const listeners = new Map<string, Set<EventListener>>();
  const localStorage = {
    getItem(key: string) {
      return store.has(key) ? store.get(key)! : null;
    },
    setItem(key: string, value: string) {
      store.set(key, value);
    },
    removeItem(key: string) {
      store.delete(key);
    },
  };
  const windowStub = {
    localStorage,
    addEventListener(type: string, listener: EventListener) {
      const bucket = listeners.get(type) ?? new Set();
      bucket.add(listener);
      listeners.set(type, bucket);
    },
    removeEventListener(type: string, listener: EventListener) {
      listeners.get(type)?.delete(listener);
    },
    dispatchEvent(event: Event) {
      for (const listener of listeners.get(event.type) ?? []) {
        listener(event);
      }
      return true;
    },
  };
  vi.stubGlobal("window", windowStub);
  return { store, windowStub };
}

describe("resolveRuntimeIdentity", () => {
  it("separates TTS primary runtime from cleaner selection display names", () => {
    const identity = resolveRuntimeIdentity({
      primaryRuntime: "MLX",
      cleanerBackendId: "openai",
      cleanerBackendLabel: "Cloud API",
      ttsEngineLabel: null,
    });

    expect(identity).toEqual({
      ttsLabel: "MLX",
      cleanerLabel: "Cloud API",
    });
    expect(formatRuntimeIdentity(identity)).toBe("TTS: MLX · Cleaner: Cloud API");
  });

  it("falls back cleanly when cleaner or TTS is unknown", () => {
    expect(
      formatRuntimeIdentity(
        resolveRuntimeIdentity({
          primaryRuntime: null,
          cleanerBackendId: "",
          cleanerBackendLabel: null,
          ttsEngineLabel: null,
        }),
      ),
    ).toBe("TTS: — · Cleaner: —");
  });

  it("prefers cleaner display name over raw id", () => {
    const identity = resolveRuntimeIdentity({
      primaryRuntime: "CUDA",
      cleanerBackendId: "mlx",
      cleanerBackendLabel: "Local MLX",
      ttsEngineLabel: "Kokoro",
    });
    expect(identity.ttsLabel).toBe("CUDA");
    expect(identity.cleanerLabel).toBe("Local MLX");
  });
});

describe("runtime identity settings notification", () => {
  beforeEach(() => {
    installWindowStub();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("notifies same-document consumers when persisted settings change", () => {
    const listener = vi.fn();
    window.addEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, listener);
    savePersistedSettings({
      cleaner_backend: "openai",
      cleaner_backend_configured: true,
    });
    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, listener);
  });

  it("recomputes Cloud API -> Local MLX from cached capabilities without API reconnect", () => {
    const cloudLabel = buildRuntimeIdentityLabelFromSnapshot({
      capabilities,
      modelOptions,
      settings: {
        cleaner_backend: "openai",
        cleaner_backend_configured: true,
      },
    });
    expect(cloudLabel).toBe("TTS: MLX · Cleaner: Cloud API");

    const mlxLabel = buildRuntimeIdentityLabelFromSnapshot({
      capabilities,
      modelOptions,
      settings: {
        cleaner_backend: "mlx",
        cleaner_backend_configured: true,
      },
    });
    expect(mlxLabel).toBe("TTS: MLX · Cleaner: Local MLX");

    // Persistence path still notifies without requiring a new API client.
    const listener = vi.fn();
    window.addEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, listener);
    savePersistedSettings({
      cleaner_backend: "mlx",
      cleaner_backend_configured: true,
    });
    expect(window.localStorage.getItem(SETTINGS_KEY)).toContain("mlx");
    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, listener);
  });

  it("ignores an unconfigured persisted backend and reports the auto-selected local one", () => {
    // The conversion views only honor cleaner_backend when
    // cleaner_backend_configured is true; otherwise they auto-pick the
    // preferred local backend. The header label must apply the same policy or
    // it reports "Cloud API" while the app will actually use Local MLX.
    const label = buildRuntimeIdentityLabelFromSnapshot({
      capabilities,
      modelOptions,
      settings: {
        cleaner_backend: "openai",
        cleaner_backend_configured: false,
      },
    });
    expect(label).toBe("TTS: MLX · Cleaner: Local MLX");
  });

  it("falls back to the preferred available backend when nothing is persisted", () => {
    const label = buildRuntimeIdentityLabelFromSnapshot({
      capabilities,
      modelOptions,
      settings: {},
    });
    expect(label).toBe("TTS: MLX · Cleaner: Local MLX");
  });

  it("ignores a configured backend that is no longer available", () => {
    const unavailableMlx: SystemCapabilitiesResponse = {
      ...capabilities,
      cleaner_runtime_availability: { mlx: false },
    };
    const label = buildRuntimeIdentityLabelFromSnapshot({
      capabilities: unavailableMlx,
      modelOptions,
      settings: {
        cleaner_backend: "mlx",
        cleaner_backend_configured: true,
      },
    });
    expect(label).toBe("TTS: MLX · Cleaner: Cloud API");
  });
});
