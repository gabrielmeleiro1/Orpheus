// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DesktopBackendStatus } from "../types/api";

const { waitForDesktopEndpoint, readDesktopBackendStatus } = vi.hoisted(() => ({
  waitForDesktopEndpoint: vi.fn(),
  readDesktopBackendStatus: vi.fn<() => Promise<DesktopBackendStatus>>(),
}));
vi.mock("./desktopBackend", () => ({
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
    readDesktopBackendStatus.mockResolvedValue({ lifecycle: "ready", endpoint: null, error: null });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ status: "ok" }), { status: 200, headers: { "Content-Type": "application/json" } })));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("cancels the endpoint wait when the hook unmounts", async () => {
    let signal: AbortSignal | undefined;
    waitForDesktopEndpoint.mockImplementation((nextSignal?: AbortSignal) => {
      signal = nextSignal;
      return new Promise(() => undefined);
    });
    function Harness() {
      latest = useBackendClient();
      return null;
    }
    await act(async () => root.render(<Harness />));
    expect(signal?.aborted).toBe(false);
    await act(async () => root.unmount());
    expect(signal?.aborted).toBe(true);
    root = createRoot(container);
  });

  it.each(["ready", "failed", "stopped"] as const)("resets exhausted %s recovery when the user explicitly retries", async (lifecycle) => {
    vi.useFakeTimers();
    readDesktopBackendStatus.mockResolvedValue({ lifecycle, endpoint: null, error: null });
    waitForDesktopEndpoint.mockResolvedValue(null);
    function Harness() {
      latest = useBackendClient();
      return <span>{latest.statusMessage}</span>;
    }
    await act(async () => root.render(<Harness />));
    for (let attempt = 0; attempt < 6; attempt += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    }
    expect(container.textContent).toContain("failed to recover after multiple attempts");
    const exhaustedCalls = waitForDesktopEndpoint.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(waitForDesktopEndpoint).toHaveBeenCalledTimes(exhaustedCalls);

    await act(async () => { latest?.reconnectBackend(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(waitForDesktopEndpoint).toHaveBeenCalledTimes(exhaustedCalls + 2);

    readDesktopBackendStatus.mockResolvedValue({ lifecycle: "ready", endpoint: null, error: null });
    waitForDesktopEndpoint.mockResolvedValue({ host: "127.0.0.1", port: 41002, base_url: "http://127.0.0.1:41002", token: "second" });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(latest?.connectionState).toBe("connected");
    expect(latest?.apiClient).not.toBeNull();
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

  it("reacquires the endpoint as soon as the supervisor reports a relaunched backend", async () => {
    const first = { host: "127.0.0.1", port: 41001, base_url: "http://127.0.0.1:41001", token: "first" };
    const second = { host: "127.0.0.1", port: 41002, base_url: "http://127.0.0.1:41002", token: "second" };
    waitForDesktopEndpoint.mockResolvedValueOnce(first).mockResolvedValueOnce(second);
    readDesktopBackendStatus.mockResolvedValueOnce({ lifecycle: "ready", endpoint: first, error: null })
      .mockResolvedValue({ lifecycle: "ready", endpoint: second, error: null });

    vi.useFakeTimers();
    function Harness() {
      latest = useBackendClient();
      return <span>{latest.backendBaseUrl}</span>;
    }
    await act(async () => { root.render(<Harness />); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(latest?.connectionState).toBe("connected");
    const firstClient = latest?.apiClient;

    // The next health tick (interval mocked to 60 s) observes the new socket.
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(container.textContent).toBe("http://127.0.0.1:41002");
    expect(latest?.apiClient).not.toBe(firstClient);
    expect(waitForDesktopEndpoint).toHaveBeenCalledTimes(2);
  });

  it("keeps the startup failure visible after automatic recovery is exhausted", async () => {
    vi.useFakeTimers();
    const error = "Desktop backend sidecar is missing. Reinstall Orpheus.";
    readDesktopBackendStatus.mockResolvedValue({ lifecycle: "failed", endpoint: null, error });
    waitForDesktopEndpoint.mockRejectedValue(new Error(error));
    function Harness() {
      latest = useBackendClient();
      return <span>{latest.statusMessage}</span>;
    }
    await act(async () => root.render(<Harness />));
    for (let attempt = 0; attempt < 6; attempt += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    }
    expect(container.textContent).toContain("failed to recover after multiple attempts");
    expect(container.textContent).toContain(error);
  });
});
