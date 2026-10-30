// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { createApiClient } from "../../api/client";
import { useEventStreamTransport } from "./useEventStreamTransport";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("reconnects after token acquisition stalls", async () => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(1);
  let signal: AbortSignal | null | undefined;
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementationOnce((_input, init) => {
    signal = init?.signal;
    return new Promise((_resolve, reject) => {
      signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    });
  }).mockImplementation(async () => new Response(JSON.stringify({ token: "scoped", expires_at: Date.now() / 1000 + 300 })));
  const connected = vi.fn();
  vi.stubGlobal("EventSource", class {
    close = vi.fn();
    constructor(url: string) { connected(url); }
  });
  const apiClient = createApiClient({ baseUrl: "http://desktop.test", token: "secret" });
  const onLog = vi.fn();
  const onOpen = vi.fn();
  const onPayload = vi.fn();
  let state!: ReturnType<typeof useEventStreamTransport>;
  function Probe() {
    state = useEventStreamTransport({ apiClient, baseUrl: "http://desktop.test", onLog, onOpen, onPayload });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  await act(async () => root.render(<Probe />));
  try {
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(signal?.aborted).toBe(true);
    expect(state).toBe("error");
    expect(connected).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(500));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(connected).toHaveBeenCalledWith("http://desktop.test/api/events/stream?token=scoped");
  } finally {
    await act(async () => root.unmount());
  }
});
