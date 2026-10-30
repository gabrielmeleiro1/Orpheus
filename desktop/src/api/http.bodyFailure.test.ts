import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, createHttpTransport } from "./http";

afterEach(() => vi.restoreAllMocks());

// A body whose connection drops after the headers arrived.
function brokenBody(status: number): Response {
  return new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("{\"partial\":"));
      controller.error(new TypeError("network connection was lost"));
    },
  }), { status });
}

describe("response body failures", () => {
  it.each([
    ["requestJson", 200],
    ["requestJson", 503],
    ["requestBlob", 200],
    ["requestBlob", 503],
  ] as const)("reports a lost %s body (status %i) as a retriable network error", async (method, status) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(brokenBody(status));
    const error = await createHttpTransport("http://desktop.test", "")[method]("/api/library").catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: "network", status: null, retriable: true, detail: "network connection was lost" });
  });
});
