import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invoke = vi.hoisted(() => vi.fn());

vi.mock("@tauri-apps/api/core", () => ({ invoke }));

import { waitForDesktopEndpoint } from "./dashboardHelpers";

describe("waitForDesktopEndpoint", () => {
  beforeEach(() => {
    invoke.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("requests credentials only after backend_status reports a ready endpoint", async () => {
    const endpoint = {
      host: "127.0.0.1",
      port: 43123,
      base_url: "http://127.0.0.1:43123",
      token: "desktop-token",
    };
    invoke
      .mockResolvedValueOnce({
        lifecycle: "ready",
        endpoint: { host: endpoint.host, port: endpoint.port, base_url: endpoint.base_url },
        error: null,
      })
      .mockResolvedValueOnce(endpoint);

    await expect(waitForDesktopEndpoint()).resolves.toEqual(endpoint);

    expect(invoke.mock.calls).toEqual([
      ["backend_status"],
      ["backend_endpoint"],
    ]);
  });

  it("does not request credentials on every poll while the backend is starting", async () => {
    vi.useFakeTimers();
    const endpoint = {
      host: "127.0.0.1",
      port: 43123,
      base_url: "http://127.0.0.1:43123",
      token: "desktop-token",
    };
    invoke
      .mockResolvedValueOnce({ lifecycle: "starting", endpoint: null, error: null })
      .mockResolvedValueOnce({
        lifecycle: "ready",
        endpoint: { host: endpoint.host, port: endpoint.port, base_url: endpoint.base_url },
        error: null,
      })
      .mockResolvedValueOnce(endpoint);

    const result = waitForDesktopEndpoint();
    await vi.advanceTimersByTimeAsync(150);
    await expect(result).resolves.toEqual(endpoint);

    expect(invoke.mock.calls).toEqual([
      ["backend_status"],
      ["backend_status"],
      ["backend_endpoint"],
    ]);
  });
});
