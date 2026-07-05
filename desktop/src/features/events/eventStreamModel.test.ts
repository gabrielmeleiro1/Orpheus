import { describe, expect, it } from "vitest";

import type { DesktopEventPayload, ProgressStageName } from "../../types/api";
import {
  createInitialEventStreamState,
  createProgressStages,
  eventStreamReducer,
  normalizeEventPayload,
  normalizeTerminalStatus,
  terminalEventFromStatePayload,
} from "./eventStreamModel";

describe("event stream payload normalization", () => {
  it("normalizes strings, numeric fields, and performance snapshots", () => {
    expect(
      normalizeEventPayload({
        event_type: " Progress ",
        event_id: " evt-1 ",
        timestamp: " 2026-01-02T03:04:05Z ",
        state: " RUNNING ",
        stage: " Synthesizing ",
        message: " Working ",
        level: " INFO ",
        job_id: " job-1 ",
        progress: "0.25",
        overall_progress: "25.4",
        stage_progress: "0.5",
        chapter_current: "2.2",
        chapter_total: "4",
        chunk_current: "3",
        chunk_total: "9",
        eta: "61",
        performance: {
          pipeline_mode: " PARALLEL ",
          parallel_worker_mode: " MANUAL ",
          active_chapter_titles: [" One ", "", 4, " Two "],
          requested_parallel_chapters: "2.126",
          active_workers: "2",
          eta_seconds: "5.678",
        },
      }),
    ).toEqual({
      event_type: "progress",
      event_id: "evt-1",
      timestamp: "2026-01-02T03:04:05Z",
      state: "running",
      stage: "Synthesizing",
      message: "Working",
      level: "info",
      job_id: "job-1",
      progress: 0.25,
      overall_progress: 25.4,
      stage_progress: 0.5,
      chapter_current: 2.2,
      chapter_total: 4,
      chunk_current: 3,
      chunk_total: 9,
      eta: 61,
      performance: {
        pipeline_mode: "parallel",
        parallel_worker_mode: "manual",
        active_chapter_titles: ["One", "Two"],
        requested_parallel_chapters: 2.13,
        active_workers: 2,
        eta_seconds: 5.68,
      },
    });
  });

  it("returns null for non-object payloads and payloads without an event type", () => {
    expect(normalizeEventPayload(null)).toBeNull();
    expect(normalizeEventPayload("progress")).toBeNull();
    expect(normalizeEventPayload({ stage: "chunking" })).toBeNull();
  });
});

describe("event stream reducer progress stages", () => {
  it.each([
    ["idle", "ingestion"],
    ["ingesting", "ingestion"],
    ["chunking", "chunking"],
    ["cleaning", "cleaning"],
    ["synthesizing", "synthesis"],
    ["encoding", "encoding"],
    ["packaging", "packaging"],
    ["error", "ingestion"],
    ["cancelled", "ingestion"],
  ] satisfies Array<[string, ProgressStageName]>)(
    "maps backend PipelineStage %s to frontend stage %s",
    (backendStage, frontendStage) => {
      const result = eventStreamReducer(createInitialEventStreamState(), {
        type: "progress",
        payload: {
          event_type: "progress",
          stage: backendStage,
          progress: 0.4,
          message: "working",
        },
      });

      expect(result.state.stages.find((stage) => stage.state === "active")?.name).toBe(frontendStage);
    },
  );

  it("marks all stages done for the backend complete stage", () => {
    const result = eventStreamReducer(createInitialEventStreamState(), {
      type: "progress",
      payload: { event_type: "progress", stage: "complete", progress: 1 },
    });

    expect(result.state.progressPct).toBe(100);
    expect(result.state.stages).toEqual(
      createProgressStages().map((stage) => ({ ...stage, state: "done", pct: 100, detail: [] })),
    );
  });

  it("normalizes progress percentages, eta labels, and counter details", () => {
    const result = eventStreamReducer(createInitialEventStreamState(), {
      type: "progress",
      payload: {
        event_type: "progress",
        stage: "chunking",
        overall_progress: 0.333,
        stage_progress: 55.4,
        chapter_current: 8,
        chapter_total: 3,
        chunk_current: 2,
        chunk_total: 4,
        eta: 65,
        message: "chunking source",
      },
    });

    expect(result.state.progressPct).toBe(33);
    expect(result.state.etaText).toBe("eta ~1m 5s");
    expect(result.state.stages[1]).toMatchObject({
      name: "chunking",
      state: "active",
      pct: 55,
      detail: [
        { label: "chapter", value: "3/3", pct: 100 },
        { label: "chunk", value: "2/4", pct: 50 },
      ],
    });
  });
});

describe("event stream reducer actions", () => {
  it("handles state transitions and terminal events", () => {
    const running = eventStreamReducer(createInitialEventStreamState(), {
      type: "state",
      payload: { event_type: "state", state: "running", job_id: "job-1", message: "started" },
      logId: 1,
      fallbackTimestamp: "10:00:00",
    });

    expect(running.state).toMatchObject({
      uiState: "running",
      activeJobId: "job-1",
      runningJobId: "job-1",
      statusText: "started",
    });
    expect(running.state.logs[0]).toMatchObject({
      id: 1,
      level: "info",
      msg: "state running (job-1)",
      ts: "10:00:00",
    });

    const completed = eventStreamReducer(running.state, {
      type: "state",
      payload: { event_type: "state", state: "completed", job_id: "job-1", message: "done" },
    });

    expect(completed.terminalEvent).toEqual({ jobId: "job-1", status: "completed", message: "done" });
    expect(completed.state).toMatchObject({
      uiState: "completed",
      activeJobId: null,
      runningJobId: null,
      progressPct: 100,
      statusText: "done",
    });
  });

  it("handles log and pushLog actions", () => {
    const payload: DesktopEventPayload = {
      event_type: "log",
      level: " warning ",
      message: " hello ",
      performance: { active_workers: 2 },
    };
    const logged = eventStreamReducer(createInitialEventStreamState(), {
      type: "log",
      payload,
      logId: 2,
      fallbackTimestamp: "11:00:00",
    });
    const pushed = eventStreamReducer(logged.state, {
      type: "pushLog",
      level: "",
      message: "second",
      logId: 3,
      fallbackTimestamp: "11:00:01",
    });

    expect(logged.state.performanceSnapshot).toEqual({ active_workers: 2 });
    expect(pushed.state.logs.map((entry) => entry.msg)).toEqual(["second", "hello"]);
    expect(pushed.state.logs[0].level).toBe("info");
  });

  it("handles refresh and cancellation actions", () => {
    const running = {
      ...createInitialEventStreamState(),
      uiState: "running" as const,
      activeJobId: "job-1",
      runningJobId: "job-1",
      statusText: "conversion running",
      isCancelling: true,
    };

    expect(eventStreamReducer(running, { type: "refreshNoClient" }).state).toMatchObject({
      activeJobId: null,
      uiState: "idle",
    });
    expect(eventStreamReducer(running, { type: "refreshInactive" }).state).toMatchObject({
      activeJobId: null,
      runningJobId: null,
      isCancelling: false,
      uiState: "idle",
      statusText: "ready",
    });
    expect(
      eventStreamReducer(running, { type: "refreshActive", job: { id: "job-2", status: "running" } }).state,
    ).toMatchObject({
      activeJobId: "job-2",
      uiState: "running",
      statusText: "active job: running",
    });
    expect(eventStreamReducer(running, { type: "cancelStart" }).state.isCancelling).toBe(true);
    expect(eventStreamReducer(running, { type: "cancelRejected" }).state.isCancelling).toBe(false);

    const accepted = eventStreamReducer(running, {
      type: "cancelAccepted",
      jobId: "job-1",
      logId: 4,
      fallbackTimestamp: "12:00:00",
    });
    expect(accepted.state).toMatchObject({
      cancellingJobId: "job-1",
      statusText: "cancellation requested; waiting for backend to stop",
      currentMessage: "cancellation requested",
    });
    expect(accepted.state.logs[0].msg).toBe("job job-1 cancellation requested");
    expect(eventStreamReducer(running, { type: "setActiveJobId", jobId: "job-3" }).state.activeJobId).toBe(
      "job-3",
    );
  });
});

describe("event stream terminal job filtering", () => {
  function runJob(jobId: string) {
    return eventStreamReducer(createInitialEventStreamState(), {
      type: "state",
      payload: { event_type: "state", state: "running", job_id: jobId, message: "started" },
    }).state;
  }

  it("ignores a late progress event after the same job is cancelled", () => {
    const running = runJob("job-1");
    const cancelled = eventStreamReducer(running, {
      type: "state",
      payload: { event_type: "state", state: "cancelled", job_id: "job-1" },
    }).state;
    expect(cancelled.uiState).toBe("cancelled");

    const late = eventStreamReducer(cancelled, {
      type: "progress",
      payload: { event_type: "progress", job_id: "job-1", stage: "synthesis", progress: 42 },
    });
    expect(late.state.uiState).toBe("cancelled");
    expect(late.state).toBe(cancelled);
  });

  it("ignores a late progress event after the same job completes", () => {
    const running = runJob("job-1");
    const completed = eventStreamReducer(running, {
      type: "state",
      payload: { event_type: "state", state: "completed", job_id: "job-1", message: "done" },
    }).state;
    expect(completed.uiState).toBe("completed");

    const late = eventStreamReducer(completed, {
      type: "progress",
      payload: { event_type: "progress", job_id: "job-1", stage: "synthesis", progress: 88 },
    });
    expect(late.state.uiState).toBe("completed");
    expect(late.state).toBe(completed);
  });

  it("ignores a late running state event for a finished job", () => {
    const running = runJob("job-1");
    const cancelled = eventStreamReducer(running, {
      type: "state",
      payload: { event_type: "state", state: "cancelled", job_id: "job-1" },
    }).state;

    const lateRunning = eventStreamReducer(cancelled, {
      type: "state",
      payload: { event_type: "state", state: "running", job_id: "job-1", message: "resumed" },
    });
    expect(lateRunning.ignored).toBe(true);
    expect(lateRunning.state.uiState).toBe("cancelled");
  });

  it("still honors the start of a different job after cancellation", () => {
    const running = runJob("job-1");
    const cancelled = eventStreamReducer(running, {
      type: "state",
      payload: { event_type: "state", state: "cancelled", job_id: "job-1" },
    }).state;

    const newRunning = eventStreamReducer(cancelled, {
      type: "state",
      payload: { event_type: "state", state: "running", job_id: "job-2", message: "started" },
    });
    expect(newRunning.ignored).toBeUndefined();
    expect(newRunning.state.uiState).toBe("running");
    expect(newRunning.state.activeJobId).toBe("job-2");

    const progress = eventStreamReducer(newRunning.state, {
      type: "progress",
      payload: { event_type: "progress", job_id: "job-2", stage: "synthesis", progress: 10 },
    });
    expect(progress.state.uiState).toBe("running");
  });
});

describe("terminal state helpers", () => {
  it("normalizes only terminal statuses", () => {
    expect(normalizeTerminalStatus(" completed ")).toBe("completed");
    expect(normalizeTerminalStatus("failed")).toBe("failed");
    expect(normalizeTerminalStatus("cancelled")).toBe("cancelled");
    expect(normalizeTerminalStatus("running")).toBeNull();
  });

  it("creates terminal events from state payloads", () => {
    expect(
      terminalEventFromStatePayload({
        event_type: "state",
        state: "failed",
        job_id: " job-1 ",
        message: "",
      }),
    ).toEqual({ jobId: "job-1", status: "failed", message: "conversion failed" });
  });
});
