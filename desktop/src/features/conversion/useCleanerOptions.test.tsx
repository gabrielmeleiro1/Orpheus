// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ModelOptionsResponse } from "../../types/api";
import type { PersistedSettings } from "./conversionOptionsShared";
import { compactProviderError, useCleanerOptions } from "./useCleanerOptions";

const cloudOnlyModelOptions = {
  cleaner_backend_options: [
    {
      name: "openai",
      display_name: "Cloud API",
      is_local: false,
      requires_api_key: true,
      // Canned metadata models must never surface for API-key backends.
      models: ["gpt-5-nano", "gpt-5-mini"],
    },
  ],
} as unknown as ModelOptionsResponse;

interface ProbeProps {
  apiClient?: unknown;
  modelOptions?: ModelOptionsResponse | null;
  persisted?: PersistedSettings;
}

function Probe({ apiClient = null, modelOptions = cloudOnlyModelOptions, persisted = {} }: ProbeProps): null {
  const result = useCleanerOptions({
    apiClient: apiClient as never,
    modelOptions,
    systemCapabilities: null,
    persistedSettingsRef: { current: persisted },
  });
  (globalThis as typeof globalThis & { cleanerOptionsProbe?: typeof result }).cleanerOptionsProbe = result;
  return null;
}

const getProbe = () =>
  (globalThis as typeof globalThis & { cleanerOptionsProbe: ReturnType<typeof useCleanerOptions> })
    .cleanerOptionsProbe;

const VALID_KEY = "sk-test-0123456789abcdefghij";
const SECOND_VALID_KEY = "sk-test-9876543210zyxwvutsrq";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

describe("compactProviderError", () => {
  it("extracts the human sentence from a wrapped provider JSON body", () => {
    const wrapped =
      'API request failed (502): OpenAI models HTTP 401: {\n  "error": {\n    "message": "Incorrect API key provided: sk-****",\n    "type": "invalid_request_error"\n  }\n}';
    expect(compactProviderError(wrapped)).toBe("Incorrect API key provided: sk-****");
  });

  it("returns plain messages untouched", () => {
    expect(compactProviderError("Backend is not connected.")).toBe("Backend is not connected.");
  });
});

describe("useCleanerOptions API-key model fetching", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    delete (globalThis as typeof globalThis & { cleanerOptionsProbe?: unknown }).cleanerOptionsProbe;
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  async function renderProbe(apiClient: unknown) {
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe apiClient={apiClient} />);
    });
  }

  it("offers no canned models for an API-key backend before a fetch", async () => {
    await renderProbe({ fetchOpenAiModelIds: vi.fn(async () => []) });

    expect(getProbe().cleanerBackend).toBe("openai");
    expect(getProbe().cleanerModelOptions).toEqual([]);
    expect(getProbe().cleanerModel).toBe("");
  });

  it("auto-fetches provider models once a pasted key settles", async () => {
    const fetchOpenAiModelIds = vi.fn(async () => ["gpt-real-a", "gpt-real-b"]);
    await renderProbe({ fetchOpenAiModelIds });

    await act(async () => {
      getProbe().setCleanerApiKey(VALID_KEY);
    });
    expect(fetchOpenAiModelIds).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(700);
    });

    expect(fetchOpenAiModelIds).toHaveBeenCalledTimes(1);
    expect(fetchOpenAiModelIds).toHaveBeenCalledWith(VALID_KEY, { timeoutMs: 30_000 });
    expect(getProbe().cleanerModelOptions).toEqual([
      { value: "gpt-real-a", label: "gpt-real-a" },
      { value: "gpt-real-b", label: "gpt-real-b" },
    ]);
    expect(getProbe().cleanerModel).toBe("gpt-real-a");

    // The same settled key must not re-fetch on later renders.
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(fetchOpenAiModelIds).toHaveBeenCalledTimes(1);
  });

  it("does not start an automatic duplicate while a manual fetch is pending", async () => {
    const firstFetch = deferred<string[]>();
    const fetchOpenAiModelIds = vi.fn(() => firstFetch.promise);
    await renderProbe({ fetchOpenAiModelIds });

    await act(async () => {
      getProbe().setCleanerApiKey(VALID_KEY);
    });
    await act(async () => {
      void getProbe().fetchOpenAiModelIds();
      await Promise.resolve();
    });
    expect(fetchOpenAiModelIds).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(700);
      await Promise.resolve();
    });
    expect(fetchOpenAiModelIds).toHaveBeenCalledTimes(1);

    await act(async () => {
      firstFetch.resolve(["gpt-real-a"]);
      await Promise.resolve();
    });
  });

  it("clears old-key models immediately and fences their stale response", async () => {
    const oldFetch = deferred<string[]>();
    const newFetch = deferred<string[]>();
    const fetchOpenAiModelIds = vi
      .fn()
      .mockReturnValueOnce(oldFetch.promise)
      .mockReturnValueOnce(newFetch.promise);
    await renderProbe({ fetchOpenAiModelIds });

    await act(async () => {
      getProbe().setCleanerApiKey(VALID_KEY);
    });
    await act(async () => {
      vi.advanceTimersByTime(700);
      await Promise.resolve();
    });
    expect(fetchOpenAiModelIds).toHaveBeenCalledTimes(1);

    await act(async () => {
      getProbe().setCleanerApiKey(SECOND_VALID_KEY);
    });
    expect(getProbe().cleanerModelOptions).toEqual([]);
    expect(getProbe().cleanerModel).toBe("");

    await act(async () => {
      oldFetch.resolve(["old-account-model"]);
      await Promise.resolve();
    });
    expect(getProbe().cleanerModelOptions).toEqual([]);
    expect(getProbe().cleanerModel).toBe("");

    await act(async () => {
      vi.advanceTimersByTime(700);
      await Promise.resolve();
    });
    expect(fetchOpenAiModelIds).toHaveBeenCalledTimes(2);

    await act(async () => {
      newFetch.resolve(["new-account-model"]);
      await Promise.resolve();
    });
    expect(getProbe().cleanerModelOptions).toEqual([
      { value: "new-account-model", label: "new-account-model" },
    ]);
    expect(getProbe().cleanerModel).toBe("new-account-model");
  });

  it("skips auto-fetch for a fragment too short to be a key", async () => {
    const fetchOpenAiModelIds = vi.fn(async () => ["gpt-real-a"]);
    await renderProbe({ fetchOpenAiModelIds });

    await act(async () => {
      getProbe().setCleanerApiKey("sk-abc");
    });
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    expect(fetchOpenAiModelIds).not.toHaveBeenCalled();
  });

  it("clears fetched models when the key is removed", async () => {
    const fetchOpenAiModelIds = vi.fn(async () => ["gpt-real-a"]);
    await renderProbe({ fetchOpenAiModelIds });

    await act(async () => {
      getProbe().setCleanerApiKey(VALID_KEY);
    });
    await act(async () => {
      vi.advanceTimersByTime(700);
    });
    expect(getProbe().cleanerModelOptions).toHaveLength(1);

    await act(async () => {
      getProbe().setCleanerApiKey("");
    });

    expect(getProbe().cleanerModelOptions).toEqual([]);
    expect(getProbe().cleanerModel).toBe("");
    expect(getProbe().openAiModelFetchStatus).toBe("Paste an API key to fetch available models.");
  });
});
