import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { pollTerminalState } from "./terminalStateWaiterModel";

function createClient(job: Awaited<ReturnType<ApiClient["getActiveConversion"]>>["job"]): ApiClient {
  return {
    getActiveConversion: vi.fn(async () => ({ job })),
  } as unknown as ApiClient;
}

function createFailingClient(error: Error): ApiClient {
  return {
    getActiveConversion: vi.fn(async () => {
      throw error;
    }),
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

    expect(result).toEqual({ kind: "terminal", missingPollCount: 0, errorPollCount: 0 });
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
      errorPollCount: 0,
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

    expect(result).toEqual({ kind: "pending", missingPollCount: 0, errorPollCount: 0 });
  });

  it("counts poll exceptions as pending below the error threshold", async () => {
    const result = await pollTerminalState({
      apiClient: createFailingClient(new Error("connection refused")),
      jobId: "job-1",
      missingPollCount: 1,
      errorPollCount: 0,
      missingPollThreshold: 3,
      errorPollThreshold: 5,
      onTerminalStatus: vi.fn(),
    });

    expect(result).toEqual({ kind: "pending", missingPollCount: 1, errorPollCount: 1 });
  });

  it("resolves as unknown once consecutive poll exceptions reach the error threshold", async () => {
    const result = await pollTerminalState({
      apiClient: createFailingClient(new Error("connection refused")),
      jobId: "job-1",
      missingPollCount: 0,
      errorPollCount: 4,
      missingPollThreshold: 3,
      errorPollThreshold: 5,
      onTerminalStatus: vi.fn(),
    });

    expect(result).toEqual({
      kind: "missing-threshold",
      missingPollCount: 0,
      errorPollCount: 5,
      event: {
        jobId: "job-1",
        status: "unknown",
        message: "conversion state unreachable; terminal event likely missed",
      },
    });
  });

  it("resets the error count once a poll succeeds again", async () => {
    const result = await pollTerminalState({
      apiClient: createClient({ id: "job-1", book_id: null, status: "running", error: null }),
      jobId: "job-1",
      missingPollCount: 0,
      errorPollCount: 4,
      missingPollThreshold: 3,
      errorPollThreshold: 5,
      onTerminalStatus: vi.fn(),
    });

    expect(result).toEqual({ kind: "pending", missingPollCount: 0, errorPollCount: 0 });
  });
});
