import { describe, expect, it } from "vitest";

import { resolveConversionPrimaryAction } from "../features/conversion/conversionPrimaryAction";
import {
  createInitialEventStreamState,
  eventStreamReducer,
} from "../features/events/eventStreamModel";
import { doneStages } from "../features/events/progressStageModel";
import { filterVisibleLogs } from "../features/logs/logPresentation";
import { chapterPlaybackStatusLabel } from "../features/player/chapterPlaybackLabel";
import { resolvePlayerDurationSec } from "../features/player/playerDurationModel";
import {
  buildProgressTerminalPresentation,
  dashboardNowStageLabel,
} from "../features/progress/progressTerminalPresentation";
import { DASHBOARD_TAB_CONFIGS } from "../navigation/tabs";
import { nextTabIndexAfterKeyboard } from "../navigation/tabKeyboardNavigation";
import { formatRuntimeIdentity, resolveRuntimeIdentity } from "./runtimeIdentity";

/**
 * Deterministic behavioral journey over production models/reducers.
 * Proves shared presentation/ownership semantics without a browser harness.
 * App shell routing (Progress/Library tab switches) remains a native manual smoke.
 */
describe("UI workflow behavioral journey", () => {
  it("covers source-ready actions, conversion terminals, playback labels, and diagnostics helpers", () => {
    // Runtime identity formatting (header copy)
    expect(
      formatRuntimeIdentity(
        resolveRuntimeIdentity({
          primaryRuntime: "MLX",
          cleanerBackendId: "openai",
          cleanerBackendLabel: "Cloud API",
          ttsEngineLabel: null,
        }),
      ),
    ).toBe("TTS: MLX · Cleaner: Cloud API");

    // Queue vs single-source primary-action ownership
    expect(
      resolveConversionPrimaryAction({
        queueItemCount: 0,
        canStartConversion: false,
        canRunQueue: false,
      }).primary,
    ).toBe("start_conversion");
    const singleReady = resolveConversionPrimaryAction({
      queueItemCount: 1,
      canStartConversion: true,
      canRunQueue: true,
    });
    expect(singleReady).toMatchObject({
      primary: "start_conversion",
      showRunQueue: false,
      showStartConversion: true,
      showPreflightConfirmButton: false,
    });
    expect(
      resolveConversionPrimaryAction({
        queueItemCount: 3,
        canStartConversion: true,
        canRunQueue: true,
      }).primary,
    ).toBe("run_queue");

    // start -> running -> stage progress via production reducer
    let stream = createInitialEventStreamState();
    stream = eventStreamReducer(stream, {
      type: "state",
      payload: { event_type: "state", state: "running", job_id: "job-1", message: "started" },
      logId: 1,
    }).state;
    expect(stream.uiState).toBe("running");

    stream = eventStreamReducer(stream, {
      type: "progress",
      payload: {
        event_type: "progress",
        job_id: "job-1",
        stage: "synthesizing",
        overall_progress: 50,
        stage_progress: 40,
        message: "synth",
        performance: {
          max_workers: 4,
          active_workers: 2,
          pipeline_mode: "parallel",
          parallel_worker_mode: "manual",
          elapsed_seconds: 8,
          tts_chars_per_second: 30,
        },
      },
    }).state;
    expect(stream.progressPct).toBe(50);
    expect(dashboardNowStageLabel(stream.uiState, stream.stages)).toBe("synthesis");

    // Terminal completion semantics (presentation models; not App tab routing)
    stream = eventStreamReducer(stream, {
      type: "state",
      payload: { event_type: "state", state: "completed", job_id: "job-1", message: "done" },
      logId: 2,
    }).state;
    expect(stream.uiState).toBe("completed");
    expect(stream.stages).toEqual(doneStages().map((item) => ({ ...item, detail: [] })));
    expect(dashboardNowStageLabel(stream.uiState, stream.stages)).toBe("complete");

    const completedPresentation = buildProgressTerminalPresentation({
      uiState: stream.uiState,
      stages: stream.stages,
      currentStage: stream.currentStage,
      currentMessage: stream.currentMessage,
      performance: stream.performanceSnapshot,
      progressPct: stream.progressPct,
    });
    expect(completedPresentation.operationTitle).toBe("complete");
    expect(completedPresentation.showLiveWorkers).toBe(false);
    expect(completedPresentation.showLiveActiveTask).toBe(false);
    expect(completedPresentation.modeSummary.toLowerCase()).not.toContain("waiting");

    // selected chapter paused vs playing
    expect(chapterPlaybackStatusLabel({ isCurrent: true, playing: false })).toBe("Selected");
    expect(chapterPlaybackStatusLabel({ isCurrent: true, playing: true })).toBe("Playing");

    // whole-book duration fallback then metadata override
    expect(
      resolvePlayerDurationSec({
        audioDurationSec: 0,
        bookDurationMs: 12_000,
        chapterDurationMs: 4_000,
      }),
    ).toBe(12);
    expect(
      resolvePlayerDurationSec({
        audioDurationSec: 11,
        bookDurationMs: 12_000,
        chapterDurationMs: 4_000,
      }),
    ).toBe(11);

    // failed / cancelled terminals
    let failed = createInitialEventStreamState();
    failed = eventStreamReducer(failed, {
      type: "state",
      payload: { event_type: "state", state: "failed", job_id: "job-2", message: "boom" },
      logId: 3,
    }).state;
    expect(failed.uiState).toBe("failed");
    expect(
      buildProgressTerminalPresentation({
        uiState: failed.uiState,
        stages: failed.stages,
        currentStage: failed.currentStage,
        currentMessage: failed.currentMessage,
        performance: null,
        progressPct: failed.progressPct,
      }).showLiveWorkers,
    ).toBe(false);

    let cancelled = createInitialEventStreamState();
    cancelled = eventStreamReducer(cancelled, {
      type: "state",
      payload: { event_type: "state", state: "cancelled", job_id: "job-3" },
      logId: 4,
    }).state;
    expect(cancelled.uiState).toBe("cancelled");

    // tab keyboard mapping / wrap / Home / End
    const tabCount = DASHBOARD_TAB_CONFIGS.length;
    expect(nextTabIndexAfterKeyboard({ key: "ArrowRight", activeIndex: tabCount - 1, tabCount })).toBe(0);
    expect(nextTabIndexAfterKeyboard({ key: "Home", activeIndex: 3, tabCount })).toBe(0);
    expect(nextTabIndexAfterKeyboard({ key: "End", activeIndex: 1, tabCount })).toBe(tabCount - 1);

    // log category filtering
    const logs = [
      { id: 1, level: "info", ts: "10:00:00", msg: "state completed (job-1)" },
      { id: 2, level: "info", ts: "10:00:01", msg: "sse connected" },
      { id: 3, level: "error", ts: "10:00:02", msg: "conversion failed" },
    ];
    expect(filterVisibleLogs(logs, { level: "all", category: "state" }).map((entry) => entry.id)).toEqual([
      1,
    ]);
    expect(filterVisibleLogs(logs, { level: "error", category: "all" }).map((entry) => entry.id)).toEqual([
      3,
    ]);
  });
});
