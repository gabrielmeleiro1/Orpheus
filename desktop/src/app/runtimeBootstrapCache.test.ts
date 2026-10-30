import { describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../api/client";
import { getRuntimeBootstrap, invalidateRuntimeBootstrap, updateRuntimeBootstrapCapabilities } from "./runtimeBootstrapCache";

function clientWith(
  getModelOptions: ApiClient["getModelOptions"],
  getSystemCapabilities: ApiClient["getSystemCapabilities"],
): ApiClient {
  return { getModelOptions, getSystemCapabilities } as ApiClient;
}

describe("runtimeBootstrapCache", () => {
  it.each(["/api/options/models", "/api/system/capabilities"])("evicts a stalled %s read even after a capability update", async (path) => {
    vi.useFakeTimers();
    let signal: AbortSignal | null | undefined;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (String(input).endsWith(path)) {
        signal = init?.signal;
        return new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        });
      }
      return Promise.resolve(new Response(JSON.stringify({})));
    });
    const client = createApiClient({ baseUrl: "http://desktop.test" });
    const failed = vi.fn();
    void getRuntimeBootstrap(client).catch(failed);
    updateRuntimeBootstrapCapabilities(client, {} as Awaited<ReturnType<ApiClient["getSystemCapabilities"]>>);
    void getRuntimeBootstrap(client).catch(() => {});
    try {
      await vi.advanceTimersByTimeAsync(8000);
      expect(signal?.aborted).toBe(true);
      expect(failed).toHaveBeenCalledWith(expect.objectContaining({ code: "timeout", retriable: true }));
      fetchMock.mockImplementation(async () => new Response(JSON.stringify({})));
      await expect(getRuntimeBootstrap(client)).resolves.toEqual({ models: {}, capabilities: {} });
      expect(fetchMock).toHaveBeenCalledTimes(4);
    } finally {
      vi.restoreAllMocks();
      vi.useRealTimers();
    }
  });

  it("reloads model and capability data after runtime settings are saved", async () => {
    const models = {} as Awaited<ReturnType<ApiClient["getModelOptions"]>>;
    const capabilities = {} as Awaited<ReturnType<ApiClient["getSystemCapabilities"]>>;
    const client = clientWith(vi.fn(async () => models), vi.fn(async () => capabilities));
    await getRuntimeBootstrap(client);
    invalidateRuntimeBootstrap(client);
    await getRuntimeBootstrap(client);
    expect(client.getModelOptions).toHaveBeenCalledTimes(2);
    expect(client.getSystemCapabilities).toHaveBeenCalledTimes(2);
  });
  it("shares one in-flight bootstrap between consumers of the same client", async () => {
    const models = { tts_engines: [] } as unknown as Awaited<ReturnType<ApiClient["getModelOptions"]>>;
    const capabilities = {} as Awaited<ReturnType<ApiClient["getSystemCapabilities"]>>;
    const getModelOptions = vi.fn(async () => models);
    const getSystemCapabilities = vi.fn(async () => capabilities);
    const client = clientWith(getModelOptions, getSystemCapabilities);

    const [first, second] = await Promise.all([
      getRuntimeBootstrap(client),
      getRuntimeBootstrap(client),
    ]);

    expect(first).toEqual({ models, capabilities });
    expect(second).toBe(first);
    expect(getModelOptions).toHaveBeenCalledTimes(1);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);
  });

  it("evicts a rejected bootstrap so a later consumer can retry", async () => {
    const models = { tts_engines: [] } as unknown as Awaited<ReturnType<ApiClient["getModelOptions"]>>;
    const capabilities = {} as Awaited<ReturnType<ApiClient["getSystemCapabilities"]>>;
    const getModelOptions = vi
      .fn<ApiClient["getModelOptions"]>()
      .mockRejectedValueOnce(new Error("temporary"))
      .mockResolvedValue(models);
    const getSystemCapabilities = vi.fn(async () => capabilities);
    const client = clientWith(getModelOptions, getSystemCapabilities);

    await expect(getRuntimeBootstrap(client)).rejects.toThrow("temporary");
    await expect(getRuntimeBootstrap(client)).resolves.toEqual({ models, capabilities });

    expect(getModelOptions).toHaveBeenCalledTimes(2);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(2);
  });

  it("updates cached capabilities without refetching models", async () => {
    const models = { tts_engines: [{ id: "kokoro-mlx" }] } as unknown as Awaited<
      ReturnType<ApiClient["getModelOptions"]>
    >;
    const stale = { mlx_runtime_available: false } as Awaited<
      ReturnType<ApiClient["getSystemCapabilities"]>
    >;
    const fresh = { mlx_runtime_available: true } as Awaited<
      ReturnType<ApiClient["getSystemCapabilities"]>
    >;
    const getModelOptions = vi.fn(async () => models);
    const getSystemCapabilities = vi.fn(async () => stale);
    const client = clientWith(getModelOptions, getSystemCapabilities);

    await expect(getRuntimeBootstrap(client)).resolves.toEqual({ models, capabilities: stale });
    updateRuntimeBootstrapCapabilities(client, fresh);
    await expect(getRuntimeBootstrap(client)).resolves.toEqual({ models, capabilities: fresh });
    expect(getModelOptions).toHaveBeenCalledTimes(1);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);
  });
});
