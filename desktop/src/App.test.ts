import { describe, expect, it } from "vitest";

import { resolveBackendMonitorFailure } from "./App";

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
