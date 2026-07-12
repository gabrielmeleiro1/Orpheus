import { describe, expect, it } from "vitest";

import {
  decideBackendRecovery,
  nextBrowserHealthState,
  resolveBackendMonitorFailure,
} from "./app/useBackendClient";

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

  it("escalates to error once consecutive health failures reach the threshold while lifecycle stays ready", () => {
    const status = {
      lifecycle: "ready" as const,
      endpoint: { host: "127.0.0.1", port: 48123, base_url: "http://127.0.0.1:48123" },
      error: null,
    };

    const belowThreshold = resolveBackendMonitorFailure(status, new Error("health timed out"), 2, 3);
    expect(belowThreshold).toEqual({
      clearClient: false,
      connectionState: "checking",
      health: null,
      statusMessage: "health timed out",
    });

    const atThreshold = resolveBackendMonitorFailure(status, new Error("health timed out"), 3, 3);
    expect(atThreshold).toEqual({
      clearClient: true,
      connectionState: "error",
      health: null,
      statusMessage: "health timed out",
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


describe("decideBackendRecovery", () => {
  it("restarts failed backends and consumes a recovery attempt", () => {
    expect(decideBackendRecovery("failed", 0, 5)).toEqual({
      action: "restart",
      consumeAttempt: true,
    });
    expect(decideBackendRecovery("stopped", 4, 5)).toEqual({
      action: "restart",
      consumeAttempt: true,
    });
  });

  it("gives up only after the restart attempt budget is exhausted", () => {
    expect(decideBackendRecovery("failed", 5, 5)).toEqual({ action: "give_up" });
    expect(decideBackendRecovery("stopped", 5, 5)).toEqual({ action: "give_up" });
  });

  it("keeps waiting for a still-starting backend without consuming restart attempts", () => {
    expect(decideBackendRecovery("starting", 0, 5)).toEqual({
      action: "wait",
      consumeAttempt: false,
    });
    expect(decideBackendRecovery("starting", 4, 5)).toEqual({
      action: "wait",
      consumeAttempt: false,
    });
    // Even at the restart cap, a slow boot must not be treated as a spent restart.
    expect(decideBackendRecovery("starting", 5, 5)).toEqual({
      action: "wait",
      consumeAttempt: false,
    });
  });

  it("re-bootstraps a ready backend without consuming restart attempts", () => {
    expect(decideBackendRecovery("ready", 2, 5)).toEqual({
      action: "wait",
      consumeAttempt: false,
    });
  });
});
