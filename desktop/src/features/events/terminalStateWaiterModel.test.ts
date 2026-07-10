import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { pollTerminalState } from "./terminalStateWaiterModel";

function createClient(job: Awaited<ReturnType<ApiClient["getActiveConversion"]>>["job"]): ApiClient {
  return {
    getActiveConversion: vi.fn(async () => ({ job })),
  } as unknown as ApiClient;
}

describe("terminal state waiter polling", () => {
  it("reports terminal active jobs from a mocked client", async () => {
    const onTerminalStatus = vi.fn();
    const result = await pollTerminalState({
      apiClient: createClient({ id: "job-1", book_id: null, status: "failed", error: "boom" }),
      jobId: "job-1",
      missingPollCount: 0,
      missingPollThreshold: 3,
      onTerminalStatus,
    });

    expect(result).toEqual({ kind: "terminal", missingPollCount: 0 });
    expect(onTerminalStatus).toHaveBeenCalledWith({
      event_type: "state",
      job_id: "job-1",
      state: "failed",
      message: "boom",
    });
  });

  it("resolves as unknown after repeated missing or different active jobs", async () => {
    const result = await pollTerminalState({
      apiClient: createClient({ id: "job-2", book_id: null, status: "running", error: null }),
      jobId: "job-1",
      missingPollCount: 2,
      missingPollThreshold: 3,
      onTerminalStatus: vi.fn(),
    });

    expect(result).toEqual({
      kind: "missing-threshold",
      missingPollCount: 3,
      event: {
        jobId: "job-1",
        status: "unknown",
        message: "conversion no longer active; terminal event likely missed",
      },
    });
  });

  it("resets missing count while the watched job is still active", async () => {
    const result = await pollTerminalState({
      apiClient: createClient({ id: "job-1", book_id: null, status: "running", error: null }),
      jobId: "job-1",
      missingPollCount: 2,
      missingPollThreshold: 3,
      onTerminalStatus: vi.fn(),
    });

    expect(result).toEqual({ kind: "pending", missingPollCount: 0 });
  });
});
