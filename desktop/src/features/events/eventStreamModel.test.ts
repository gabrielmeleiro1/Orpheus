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

  it("treats null, blank and boolean numeric fields as absent rather than zero", () => {
    const payload = normalizeEventPayload({
      event_type: "progress",
      overall_progress: null,
      progress: 0.4,
      eta: null,
      chapter_total: "",
      chunk_total: true,
    });
    expect(payload).toEqual({ event_type: "progress", progress: 0.4 });

    const state = eventStreamReducer(createInitialEventStreamState(), {
      type: "progress",
      payload: payload!,
    }).state;
    expect(state.progressPct).toBe(40);
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
    expect(result.state.etaText).toBe("ETA ~1m 5s");
    expect(result.state.stages[1]).toMatchObject({
      name: "chunking",
      state: "active",
      pct: 55,
      detail: [
        { label: "Chapter", value: "3 of 3", pct: 100 },
        { label: "Chunks", value: "2/4", pct: 50 },
      ],
    });
  });

  it("retains completed chapter counts across a later zeroed packaging snapshot", () => {
    const withWorkerResult = eventStreamReducer(createInitialEventStreamState(), {
      type: "progress",
      payload: {
        event_type: "progress",
        job_id: "job-1",
        stage: "synthesis",
        progress: 0.9,
        performance: { completed_chapters: 1, failed_chapters: 0 },
      },
    });
    const packaging = eventStreamReducer(withWorkerResult.state, {
      type: "progress",
      payload: {
        event_type: "progress",
        job_id: "job-1",
        stage: "packaging",
        progress: 0.95,
        performance: { completed_chapters: 0, failed_chapters: 0 },
      },
    });

    expect(packaging.state.performanceSnapshot?.completed_chapters).toBe(1);
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
    const running = {
      ...createInitialEventStreamState(),
      uiState: "running" as const,
    };
    const logged = eventStreamReducer(running, {
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

  it("ignores log-carried performance once the job is no longer running", () => {
    // Reproduces the live-captured stream: the backend's terminal progress
    // event carries the truthful final snapshot, then a trailing "[PERF]
    // worker_pool_*" log arrives with the idle pool snapshot (parallel mode,
    // zero counters). That log must not clobber the final numbers.
    const finalSnapshot = { pipeline_mode: "sequential", completed_chapters: 1 };
    const completed = {
      ...createInitialEventStreamState(),
      uiState: "completed" as const,
      performanceSnapshot: finalSnapshot,
    };
    const logged = eventStreamReducer(completed, {
      type: "log",
      payload: {
        event_type: "log",
        level: "info",
        message: "[PERF] worker_pool_reuse_hits=0",
        performance: { pipeline_mode: "parallel", completed_chapters: 0 },
      },
      logId: 4,
      fallbackTimestamp: "11:00:02",
    });

    expect(logged.state.performanceSnapshot).toEqual(finalSnapshot);
    expect(logged.state.logs[0].msg).toBe("[PERF] worker_pool_reuse_hits=0");
  });

  it("handles refresh and cancellation actions", () => {
    const running = {
      ...createInitialEventStreamState(),
      uiState: "running" as const,
      activeJobId: "job-1",
      runningJobId: "job-1",
      statusText: "Conversion running",
      isCancelling: true,
    };

    expect(eventStreamReducer(running, { type: "refreshInactive" }).state).toMatchObject({
      activeJobId: null,
      runningJobId: null,
      isCancelling: false,
      uiState: "idle",
      statusText: "Ready",
    });
    expect(
      eventStreamReducer(running, { type: "refreshActive", job: { id: "job-2", status: "running" } }).state,
    ).toMatchObject({
      activeJobId: "job-2",
      uiState: "running",
      statusText: "Conversion running",
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
      statusText: "Cancellation requested — waiting for the backend to stop",
      currentMessage: "Cancellation requested",
    });
    expect(accepted.state.logs[0].msg).toBe("job job-1 cancellation requested");
    expect(eventStreamReducer(running, { type: "setActiveJobId", jobId: "job-3" }).state.activeJobId).toBe(
      "job-3",
    );
  });

  it("does not relatch a job that failed before the start response resolved", () => {
    const failed = eventStreamReducer(createInitialEventStreamState(), {
      type: "state",
      payload: {
        event_type: "state",
        job_id: "job-fast-fail",
        state: "failed",
        message: "conversion failed",
      },
    }).state;

    const started = eventStreamReducer(failed, {
      type: "conversionStarted",
      jobId: "job-fast-fail",
    });

    expect(started.ignored).toBe(true);
    expect(started.state).toMatchObject({
      activeJobId: null,
      uiState: "failed",
      isCancelling: false,
    });
  });

  it("clears conversion telemetry when the backend client disappears", () => {
    const running = eventStreamReducer(createInitialEventStreamState(), {
      type: "state",
      payload: { event_type: "state", state: "running", job_id: "job-1", message: "started" },
    }).state;
    const withTelemetry = eventStreamReducer(running, {
      type: "progress",
      payload: {
        event_type: "progress",
        job_id: "job-1",
        stage: "synthesizing",
        overall_progress: 72,
        eta: 45,
        message: "working",
        performance: { active_workers: 2 },
      },
    }).state;

    const disconnected = eventStreamReducer(withTelemetry, { type: "refreshNoClient" }).state;

    expect(disconnected).toMatchObject({
      uiState: "idle",
      statusText: "Backend disconnected",
      progressPct: 0,
      etaText: "",
      stages: createProgressStages(),
      currentStage: "",
      currentMessage: "",
      performanceSnapshot: null,
      activeJobId: null,
      isCancelling: false,
      cancellingJobId: null,
      runningJobId: null,
    });
  });

  it("keeps the live progress message while the reconcile poll confirms the same job", () => {
    const running = eventStreamReducer(createInitialEventStreamState(), {
      type: "state",
      payload: { event_type: "state", state: "running", job_id: "job-1" },
    }).state;
    const progressing = eventStreamReducer(running, {
      type: "progress",
      payload: { event_type: "progress", job_id: "job-1", stage: "synthesizing", message: "Chapter 3 of 12" },
    }).state;

    const confirmed = eventStreamReducer(progressing, {
      type: "refreshActive",
      job: { id: "job-1", status: "running" },
    }).state;
    expect(confirmed).toBe(progressing);
    expect(confirmed.statusText).toBe("Chapter 3 of 12");
  });

  it("does not keep a vanished job's live message once the backend reports no job", () => {
    const progressing = eventStreamReducer(createInitialEventStreamState(), {
      type: "progress",
      payload: { event_type: "progress", job_id: "job-1", stage: "synthesizing", message: "Chapter 3 of 12" },
    }).state;
    // The backend no longer knows a running job: it was lost with the process.
    expect(eventStreamReducer(progressing, { type: "refreshInactive" }).state).toMatchObject({
      uiState: "failed",
      activeJobId: null,
      statusText: expect.stringMatching(/interrupted.*Repair audio/),
    });

    const failed = eventStreamReducer(progressing, {
      type: "state",
      payload: { event_type: "state", job_id: "job-1", state: "failed", message: "Disk full" },
    }).state;
    expect(eventStreamReducer(failed, { type: "refreshInactive" }).state.statusText).toBe("Disk full");
  });

  it("keeps the cancellation status while the reconcile poll still sees the job running", () => {
    const running = eventStreamReducer(createInitialEventStreamState(), {
      type: "state",
      payload: { event_type: "state", state: "running", job_id: "job-1", message: "started" },
    }).state;
    const started = eventStreamReducer(running, { type: "cancelStart" }).state;
    const cancelling = eventStreamReducer(started, {
      type: "cancelAccepted",
      jobId: "job-1",
      logId: 1,
      fallbackTimestamp: "12:00:00",
    }).state;
    expect(cancelling).toMatchObject({
      isCancelling: true,
      cancellingJobId: "job-1",
      statusText: "Cancellation requested — waiting for the backend to stop",
    });

    // The fallback reconcile poll fires while the backend is still winding the
    // job down and reports it as non-terminal "running". It must not clobber the
    // "cancellation requested…" status with "active job: running".
    const reconciled = eventStreamReducer(cancelling, {
      type: "refreshActive",
      job: { id: "job-1", status: "running" },
    }).state;
    expect(reconciled).toMatchObject({
      uiState: "running",
      isCancelling: true,
      cancellingJobId: "job-1",
      statusText: "Cancellation requested — waiting for the backend to stop",
    });

    // Once the backend actually stops the job, the terminal reconcile flips the
    // UI to the cancelled state as normal.
    const stopped = eventStreamReducer(reconciled, {
      type: "state",
      payload: { event_type: "state", state: "cancelled", job_id: "job-1" },
    }).state;
    expect(stopped).toMatchObject({
      uiState: "cancelled",
      statusText: "Job cancelled",
      isCancelling: false,
    });
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

  it("does not log or reapply a finished job's terminal state again on reconnect", () => {
    const completedAction = {
      type: "state" as const,
      payload: { event_type: "state", state: "completed", job_id: "job-1", message: "conversion completed" },
      logId: 1,
    };
    const completed = eventStreamReducer(runJob("job-1"), completedAction).state;
    expect(completed.logs.map((entry) => entry.msg)).toEqual(["state completed (job-1)"]);

    const replayed = eventStreamReducer(completed, { ...completedAction, logId: 2 });
    expect(replayed.ignored).toBe(true);
    expect(replayed.state).toBe(completed);
    expect(replayed.terminalEvent).toMatchObject({ jobId: "job-1", status: "completed" });
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

describe("event log scope", () => {
  it("starts a new conversion with its own log", () => {
    let state = eventStreamReducer(createInitialEventStreamState(), {
      type: "state", payload: { event_type: "state", job_id: "job-1", state: "running" }, logId: 1,
    }).state;
    for (let index = 0; index < 5; index += 1) {
      state = eventStreamReducer(state, { type: "log", payload: { event_type: "log", job_id: "job-1", message: `old ${index}` }, logId: 10 + index }).state;
    }
    state = eventStreamReducer(state, { type: "state", payload: { event_type: "state", job_id: "job-1", state: "completed" }, logId: 20 }).state;
    expect(state.logs.length).toBeGreaterThan(5);
    state = eventStreamReducer(state, { type: "state", payload: { event_type: "state", job_id: "job-2", state: "running" }, logId: 21 }).state;
    expect(state.logs.map((entry) => entry.msg)).toEqual(["state running (job-2)"]);
  });
});
