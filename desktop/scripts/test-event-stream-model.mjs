import assert from "node:assert/strict";
import { mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import * as esbuild from "esbuild";

const outDir = path.join(tmpdir(), `event-stream-model-${Date.now()}`);
const outFile = path.join(outDir, "eventStreamModel.mjs");

await mkdir(outDir, { recursive: true });

try {
  await esbuild.build({
    entryPoints: [fileURLToPath(new URL("../src/features/events/eventStreamModel.ts", import.meta.url))],
    outfile: outFile,
    bundle: true,
    platform: "node",
    format: "esm",
    logLevel: "silent",
  });

  const {
    createInitialEventStreamState,
    eventStreamReducer,
    normalizeEventPayload,
    terminalEventFromStatePayload,
  } = await import(pathToFileURL(outFile).href);

  {
    const initial = createInitialEventStreamState();
    const { state } = eventStreamReducer(initial, {
      type: "progress",
      payload: {
        event_type: "progress",
        job_id: "job-1",
        stage: "chunking",
        overall_progress: 0.42,
        stage_progress: 0.75,
        chapter_current: 2,
        chapter_total: 4,
        chunk_current: 6,
        chunk_total: 8,
        eta: 125,
        message: "splitting chapters",
      },
    });

    assert.equal(state.uiState, "running");
    assert.equal(state.progressPct, 42);
    assert.equal(state.etaText, "eta ~2m 5s");
    assert.equal(state.statusText, "splitting chapters");
    assert.equal(state.currentStage, "chunking");
    assert.equal(state.currentMessage, "splitting chapters");
    assert.deepEqual(
      state.stages.map((stage) => [stage.name, stage.state, stage.pct]),
      [
        ["ingestion", "done", 100],
        ["chunking", "active", 75],
        ["cleaning", "pending", 0],
        ["synthesis", "pending", 0],
        ["encoding", "pending", 0],
        ["packaging", "pending", 0],
      ],
    );
    assert.deepEqual(state.stages[1].detail, [
      { label: "chapter", value: "2/4", pct: 50 },
      { label: "chunk", value: "6/8", pct: 75 },
    ]);
  }

  {
    const initial = createInitialEventStreamState();
    const accepted = eventStreamReducer(initial, {
      type: "cancelAccepted",
      jobId: "job-2",
      logId: 1,
    }).state;
    const ignored = eventStreamReducer(accepted, {
      type: "state",
      payload: {
        event_type: "state",
        job_id: "job-2",
        state: "running",
        message: "late running event",
      },
      logId: 2,
    }).state;

    assert.equal(ignored.uiState, "running");
    assert.equal(ignored.statusText, "cancellation requested; waiting for backend to stop");
    assert.equal(ignored.logs.length, 1);

    const newJobProgress = eventStreamReducer(ignored, {
      type: "progress",
      payload: {
        event_type: "progress",
        stage: "cleaning",
        progress: 0.25,
        message: "new job progress",
      },
    }).state;

    assert.equal(newJobProgress.progressPct, 25);
    assert.equal(newJobProgress.statusText, "new job progress");

    const cancelled = eventStreamReducer(ignored, {
      type: "state",
      payload: {
        event_type: "state",
        job_id: "job-2",
        state: "cancelled",
        message: "stopped",
        timestamp: "2026-05-24T10:30:00Z",
      },
      logId: 3,
    }).state;

    assert.equal(cancelled.uiState, "cancelled");
    assert.equal(cancelled.activeJobId, null);
    assert.equal(cancelled.isCancelling, false);
    assert.equal(cancelled.currentMessage, "job canceled");
    assert.equal(cancelled.logs[0].msg, "state cancelled (job-2)");
  }

  {
    const payload = normalizeEventPayload({
      event_type: "STATE",
      job_id: " job-3 ",
      state: "COMPLETED",
      message: "done",
    });
    assert.deepEqual(payload, {
      event_type: "state",
      job_id: "job-3",
      state: "completed",
      message: "done",
    });
    assert.deepEqual(terminalEventFromStatePayload(payload), {
      jobId: "job-3",
      status: "completed",
      message: "done",
    });
  }
} finally {
  await rm(outDir, { force: true, recursive: true });
}
