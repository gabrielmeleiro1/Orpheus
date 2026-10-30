// @vitest-environment happy-dom

import { renderToString } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";

import { createApiClient } from "../../api/client";
import { useTerminalStateWaiters } from "./useTerminalStateWaiters";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("retries a timed out terminal poll on the next tick", async () => {
  vi.useFakeTimers();
  let requestSignal: AbortSignal | undefined;
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementationOnce((_input, init) => {
    requestSignal = init?.signal ?? undefined;
    return new Promise((_resolve, reject) => {
      requestSignal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    });
  }).mockImplementation(async () => new Response(JSON.stringify({ active: false, job: null })));
  const client = createApiClient({ baseUrl: "http://desktop.test" });
  const failures: unknown[] = [];
  const getActiveConversion = client.getActiveConversion;
  client.getActiveConversion = (options) => getActiveConversion(options).catch((error: unknown) => {
    failures.push(error);
    throw error;
  });
  let waiters!: ReturnType<typeof useTerminalStateWaiters>;
  function Probe() {
    waiters = useTerminalStateWaiters({ apiClient: client, onTerminalStatus: vi.fn() });
    return null;
  }
  renderToString(<Probe />);
  const terminal = waiters.waitForTerminalState("job-1");
  try {
    await vi.advanceTimersByTimeAsync(8000);
    expect(requestSignal?.aborted).toBe(true);
    expect(failures).toEqual([expect.objectContaining({ code: "timeout", retriable: true })]);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(3000);
    expect(await terminal).toMatchObject({ jobId: "job-1", status: "unknown" });
    expect(vi.getTimerCount()).toBe(0);
  } finally {
    waiters.resolveTerminalState({ jobId: "job-1", status: "unknown", message: "" });
  }
});

it("answers late waiters for recent jobs while evicting the oldest outcomes", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
    async () => new Response(JSON.stringify({ active: false, job: null })),
  );
  const client = createApiClient({ baseUrl: "http://desktop.test" });
  let waiters!: ReturnType<typeof useTerminalStateWaiters>;
  function Probe() {
    waiters = useTerminalStateWaiters({ apiClient: client, onTerminalStatus: vi.fn() });
    return null;
  }
  renderToString(<Probe />);
  for (let index = 0; index <= 40; index += 1) {
    waiters.resolveTerminalState({ jobId: `job-${index}`, status: "completed", message: `done ${index}` });
  }

  await expect(waiters.waitForTerminalState("job-40")).resolves.toMatchObject({ message: "done 40" });
  await expect(waiters.waitForTerminalState("job-9")).resolves.toMatchObject({ message: "done 9" });
  expect(fetchMock).not.toHaveBeenCalled();

  vi.useFakeTimers();
  const evicted = waiters.waitForTerminalState("job-0");
  expect(fetchMock).toHaveBeenCalledTimes(1);
  waiters.resolveTerminalState({ jobId: "job-0", status: "completed", message: "polled" });
  await expect(evicted).resolves.toMatchObject({ message: "polled" });
  expect(vi.getTimerCount()).toBe(0);
});
