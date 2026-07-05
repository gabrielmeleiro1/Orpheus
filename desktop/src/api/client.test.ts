import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client errors", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("throws typed HTTP errors with string detail while preserving message text", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ detail: "bad source" }, 400));
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });

    await expect(client.requestJson("/api/test")).rejects.toMatchObject({
      name: "ApiError",
      message: "API request failed (400): bad source",
      status: 400,
      code: "http",
      detail: "bad source",
      retriable: false,
    });
  });

  it("summarizes FastAPI validation detail arrays", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        {
          detail: [
            { loc: ["body", "source_path"], msg: "field required", type: "missing" },
            { loc: ["body", "runtime_options", "voice"], msg: "none is not allowed", type: "value_error" },
          ],
        },
        422,
      ),
    );
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });

    await expect(client.requestJson("/api/test")).rejects.toMatchObject({
      message: "API request failed (422): source_path: field required; runtime_options.voice: none is not allowed",
      status: 422,
      code: "http",
      detail: "source_path: field required; runtime_options.voice: none is not allowed",
      retriable: false,
    });
  });

  it("uses message, text, empty, and malformed JSON bodies like the previous message formatter", async () => {
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });
    const fetchMock = vi.spyOn(globalThis, "fetch");

    fetchMock.mockResolvedValueOnce(jsonResponse({ message: "not found" }, 404));
    await expect(client.requestJson("/api/message")).rejects.toThrow("API request failed (404): not found");

    fetchMock.mockResolvedValueOnce(new Response("plain failure", { status: 500 }));
    await expect(client.requestJson("/api/text")).rejects.toMatchObject({
      message: "API request failed (500): plain failure",
      detail: "plain failure",
      retriable: true,
    });

    fetchMock.mockResolvedValueOnce(new Response("", { status: 404 }));
    await expect(client.requestJson("/api/empty")).rejects.toMatchObject({
      message: "API request failed (404)",
      detail: "",
    });

    fetchMock.mockResolvedValueOnce(new Response("{bad json", { status: 400 }));
    await expect(client.requestJson("/api/malformed")).rejects.toMatchObject({
      message: "API request failed (400): {bad json",
      detail: "{bad json",
    });
  });

  it("throws invalid-json errors for malformed successful JSON responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{bad json", { status: 200 }));
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });

    await expect(client.requestJson("/api/test")).rejects.toMatchObject({
      name: "ApiError",
      status: null,
      code: "invalid-json",
      detail: "invalid JSON response",
      retriable: false,
    });
  });

  it("throws typed network failures", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("fetch failed"));
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });

    await expect(client.requestJson("/api/test")).rejects.toMatchObject({
      name: "ApiError",
      message: "fetch failed",
      status: null,
      code: "network",
      detail: "fetch failed",
      retriable: true,
    });
  });

  it("throws aborted errors when the caller signal aborts", async () => {
    const controller = new AbortController();
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
        controller.abort();
      });
    });
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });

    await expect(client.requestJson("/api/test", { signal: controller.signal })).rejects.toMatchObject({
      status: null,
      code: "aborted",
      detail: "request aborted",
      retriable: false,
    });
  });

  it("throws timeout errors when timeoutMs elapses", async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      });
    });
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });
    const request = client.requestBlob("/api/test", { timeoutMs: 10 });
    const expectation = expect(request).rejects.toMatchObject({
      status: null,
      code: "timeout",
      detail: "request timed out",
      retriable: true,
    });

    await vi.advanceTimersByTimeAsync(10);

    await expectation;
  });

  it("threads request options through endpoint methods", async () => {
    const controller = new AbortController();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ status: "ok" }));
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });

    await client.getHealth({ signal: controller.signal, timeoutMs: 1000 });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1/health",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });
});

describe("api client request contracts", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends favorite library filters when provided", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ items: [], count: 0 }))
      .mockResolvedValueOnce(jsonResponse({ items: [], count: 0 }));
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });

    await client.getLibrary("", undefined, { favorite: false });
    await client.getLibrary("dune", 20, { conversionMode: "audiobook", favorite: true });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1/api/library?favorite=false",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1/api/library?search=dune&limit=20&conversion_mode=audiobook&favorite=true",
      expect.any(Object),
    );
  });

  it("only sends the OpenAI API key when fetching model IDs", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ provider: "openai", base_url: "https://api.openai.com/v1", models: [] }));
    const client = createApiClient({ baseUrl: "http://127.0.0.1" });

    await client.fetchOpenAiModelIds(" sk-test ");

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ api_key: "sk-test" });
  });
});
