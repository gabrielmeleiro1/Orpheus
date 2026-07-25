// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { waitForDesktopEndpoint, readDesktopBackendStatus } = vi.hoisted(() => ({
  waitForDesktopEndpoint: vi.fn(),
  readDesktopBackendStatus: vi.fn(async () => ({ lifecycle: "ready", endpoint: null, error: null })),
}));
vi.mock("../features/dashboard/dashboardHelpers", () => ({
  BACKEND_HEALTH_CHECK_INTERVAL_MS: 60_000,
  describeDesktopBackendStatus: vi.fn(() => "ready"),
  readDesktopBackendStatus,
  restartDesktopBackend: vi.fn(),
  waitForDesktopEndpoint,
}));
vi.mock("../platform/tauriRuntime", () => ({ isTauriRuntime: () => true }));

import { retainEquivalentHealth, useBackendClient } from "./useBackendClient";

describe("retainEquivalentHealth", () => {
  it("keeps object identity when the health status has not changed", () => {
    const previous = { status: "ok" };
    expect(retainEquivalentHealth(previous, { status: "ok" })).toBe(previous);
    expect(retainEquivalentHealth(previous, { status: "degraded" })).toEqual({ status: "degraded" });
  });
});

describe("useBackendClient explicit reconnect", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  let latest: ReturnType<typeof useBackendClient> | null;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    latest = null;
    waitForDesktopEndpoint.mockReset();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ status: "ok" }), { status: 200, headers: { "Content-Type": "application/json" } })));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it("drops the stale client and reacquires the rotated endpoint", async () => {
    waitForDesktopEndpoint
      .mockResolvedValueOnce({ host: "127.0.0.1", port: 41001, base_url: "http://127.0.0.1:41001", token: "first" })
      .mockResolvedValueOnce({ host: "127.0.0.1", port: 41002, base_url: "http://127.0.0.1:41002", token: "second" });

    function Harness() {
      latest = useBackendClient();
      return <span>{latest.backendBaseUrl}</span>;
    }
    await act(async () => { root.render(<Harness />); });
    expect(container.textContent).toBe("http://127.0.0.1:41001");
    const firstClient = latest?.apiClient;

    await act(async () => { latest?.reconnectBackend(); });
    expect(container.textContent).toBe("http://127.0.0.1:41002");
    expect(latest?.apiClient).not.toBe(firstClient);
    expect(waitForDesktopEndpoint).toHaveBeenCalledTimes(2);
  });
});
