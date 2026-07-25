import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api/client";
import { getRuntimeBootstrap, updateRuntimeBootstrapCapabilities } from "./runtimeBootstrapCache";

function clientWith(
  getModelOptions: ApiClient["getModelOptions"],
  getSystemCapabilities: ApiClient["getSystemCapabilities"],
): ApiClient {
  return { getModelOptions, getSystemCapabilities } as ApiClient;
}

describe("runtimeBootstrapCache", () => {
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
