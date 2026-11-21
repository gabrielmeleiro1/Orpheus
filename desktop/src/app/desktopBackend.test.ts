import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invoke = vi.hoisted(() => vi.fn());

vi.mock("@tauri-apps/api/core", () => ({ invoke }));

import { waitForDesktopEndpoint } from "./desktopBackend";

describe("waitForDesktopEndpoint", () => {
  it.each(["backend_status", "backend_endpoint"])("bounds a hung %s IPC call and ignores its late rejection", async (hungCommand) => {
    vi.useFakeTimers();
    let rejectLate!: (error: Error) => void;
    const hung = new Promise((_, reject) => { rejectLate = reject; });
    invoke.mockImplementation((command: string) => command === hungCommand ? hung
      : Promise.resolve({ lifecycle: "ready", endpoint: { base_url: "http://desktop.test" } }));
    const result = waitForDesktopEndpoint();
    await vi.advanceTimersByTimeAsync(120_000);
    await expect(result).resolves.toBeNull();
    rejectLate(new Error("late IPC failure"));
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(0);
  });
  it("allows a cold backend to finish after ninety seconds", async () => {
    vi.useFakeTimers();
    const startedAt = Date.now();
    const endpoint = { host: "127.0.0.1", port: 43123, base_url: "http://127.0.0.1:43123", token: "test" };
    invoke.mockImplementation(async (command: string) => command === "backend_endpoint"
      ? endpoint
      : Date.now() - startedAt < 90_000
        ? { lifecycle: "starting", endpoint: null, error: null }
        : { lifecycle: "ready", endpoint, error: null });
    const result = waitForDesktopEndpoint();
    await vi.advanceTimersByTimeAsync(90_150);
    await expect(result).resolves.toEqual(endpoint);
  });
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
