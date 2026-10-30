import { afterEach, describe, expect, it, vi } from "vitest";

import { createHttpTransport } from "./http";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("request body cancellation", () => {
  it.each([
    ["requestJson", 200],
    ["requestJson", 400],
    ["requestBlob", 200],
    ["requestBlob", 400],
  ] as const)("protects %s bodies with status %i through consumption", async (method, status) => {
    for (const cancellation of ["timeout", "aborted"] as const) {
      vi.useFakeTimers();
      let requestSignal: AbortSignal | undefined;
      vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
        requestSignal = init?.signal ?? undefined;
        return new Response(new ReadableStream({
          start(controller) {
            requestSignal?.addEventListener("abort", () => {
              controller.error(new DOMException("request aborted", "AbortError"));
            });
          },
        }), { status });
      });
      const caller = new AbortController();
      const transport = createHttpTransport("http://desktop.test", "");
      const result = transport[method]("/body", { signal: caller.signal, timeoutMs: 100 })
        .catch((error: unknown) => error);
      await vi.advanceTimersByTimeAsync(0);
      if (cancellation === "timeout") {
        await vi.advanceTimersByTimeAsync(100);
      } else {
        caller.abort();
      }
      expect(requestSignal?.aborted).toBe(true);
      expect(await result).toMatchObject({
        code: cancellation,
        status: null,
        detail: cancellation === "timeout" ? "request timed out" : "request aborted",
        retriable: cancellation === "timeout",
      });
      expect(vi.getTimerCount()).toBe(0);
      vi.restoreAllMocks();
    }
  });
});

it("classifies an external deadline as a timeout", async () => {
  vi.useFakeTimers();
  const controller = new AbortController();
  vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  const result = createHttpTransport("http://desktop.test", "").requestJson("/probe", { signal: controller.signal })
    .catch((error: unknown) => error);
  setTimeout(() => controller.abort(new DOMException("request timed out", "TimeoutError")), 100);
  await vi.advanceTimersByTimeAsync(100);
  expect(await result).toMatchObject({ code: "timeout", retriable: true });
});

describe("request targets", () => {
  it.each(["https://example.com/api", "//example.com/api", "HTTP://example.com"])(
    "refuses to send the bearer token to %s",
    async (path) => {
      const fetchMock = vi.spyOn(globalThis, "fetch");
      const transport = createHttpTransport("http://127.0.0.1:41001", "session-token");
      await expect(transport.requestJson(path)).rejects.toThrow("relative to the backend origin");
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("joins relative paths onto the backend origin", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}"));
    const transport = createHttpTransport("http://127.0.0.1:41001/", "session-token");
    await transport.requestJson("/api/library");
    await transport.requestJson("health");
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:41001/api/library",
      "http://127.0.0.1:41001/health",
    ]);
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Authorization")).toBe("Bearer session-token");
  });
});

describe("HTTP error details", () => {
  it.each(["requestJson", "requestBlob"] as const)("preserves messages and transport classification for %s", async (method) => {
    const cases = [
      { detail: "Reload the library", serverCode: null },
      { detail: { code: "library_replaced", message: "Reload the library" }, serverCode: "library_replaced" },
      { detail: { error: "setup_in_progress", message: "Reload the library" }, serverCode: "setup_in_progress" },
      { detail: { code: "library_replaced", error: "legacy_code", message: "Reload the library" }, serverCode: "library_replaced" },
      { detail: { code: 123, error: "setup_in_progress", message: "Reload the library" }, serverCode: "setup_in_progress" },
    ];
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const transport = createHttpTransport("http://desktop.test", "");
    for (const { detail, serverCode } of cases) {
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail }), { status: 409 }));
      await expect(transport[method]("/resource")).rejects.toMatchObject({
        status: 409,
        code: "http",
        serverCode,
        detail: "Reload the library",
        message: "Reload the library",
        retriable: false,
      });
    }
  });
});
