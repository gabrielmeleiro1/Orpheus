import { describe, expect, it, vi } from "vitest";

import { cancelActiveSetup, restartAndReconnectSetup } from "./setupLifecycle";

describe("setup lifecycle actions", () => {
  it("restarts, refetches authoritative readiness, and lets the gate continue", async () => {
    const restart = vi.fn(async () => undefined);
    const reconnect = vi.fn();
    await expect(restartAndReconnectSetup(restart, reconnect)).resolves.toBeUndefined();
    expect(restart).toHaveBeenCalledOnce();
    expect(reconnect).toHaveBeenCalledOnce();
  });

  it("does not refetch after restart failure and remains retryable", async () => {
    const restart = vi.fn()
      .mockRejectedValueOnce(new Error("restart failed"))
      .mockResolvedValueOnce(undefined);
    const reconnect = vi.fn();
    await expect(restartAndReconnectSetup(restart, reconnect)).rejects.toThrow("restart failed");
    expect(reconnect).not.toHaveBeenCalled();
    await expect(restartAndReconnectSetup(restart, reconnect)).resolves.toBeUndefined();
  });

  it("routes cancellation through the registered safe command", async () => {
    const cancel = vi.fn(async () => ({ lifecycle: "cancelled" }));
    await expect(cancelActiveSetup(cancel)).resolves.toEqual({ lifecycle: "cancelled" });
    expect(cancel).toHaveBeenCalledOnce();
  });
});
