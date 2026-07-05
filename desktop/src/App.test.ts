import { describe, expect, it } from "vitest";

import { nextBrowserHealthState, resolveBackendMonitorFailure } from "./App";

describe("resolveBackendMonitorFailure", () => {
  it("preserves the current endpoint after a transient monitor error while lifecycle remains ready", () => {
    const result = resolveBackendMonitorFailure({
      lifecycle: "ready",
      endpoint: { host: "127.0.0.1", port: 48123, base_url: "http://127.0.0.1:48123" },
      error: null,
    }, new Error("network hiccup"));

    expect(result).toEqual({
      clearClient: false,
      connectionState: "checking",
      health: null,
      statusMessage: "network hiccup",
    });
  });

  it("clears the current endpoint when the Tauri backend lifecycle is terminal", () => {
    const result = resolveBackendMonitorFailure({
      lifecycle: "failed",
      endpoint: null,
      error: "backend crashed",
    }, new Error("health failed"));

    expect(result).toEqual({
      clearClient: true,
      connectionState: "error",
      health: null,
      statusMessage: "backend crashed",
    });
  });
});

describe("nextBrowserHealthState", () => {
  it("stays in checking until the failure threshold, then reports error", () => {
    const first = nextBrowserHealthState(0, "failure", 3);
    expect(first).toEqual({ failures: 1, connectionState: "checking" });

    const second = nextBrowserHealthState(first.failures, "failure", 3);
    expect(second).toEqual({ failures: 2, connectionState: "checking" });

    const third = nextBrowserHealthState(second.failures, "failure", 3);
    expect(third).toEqual({ failures: 3, connectionState: "error" });
  });

  it("recovers to connected and resets the failure count on a successful poll", () => {
    expect(nextBrowserHealthState(5, "success", 3)).toEqual({
      failures: 0,
      connectionState: "connected",
    });
  });
});
