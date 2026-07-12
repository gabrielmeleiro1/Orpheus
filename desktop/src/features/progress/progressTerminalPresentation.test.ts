import { describe, expect, it } from "vitest";

import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";
import { createProgressStages, doneStages } from "../events/progressStageModel";
import {
  buildProgressTerminalPresentation,
  dashboardNowStageLabel,
} from "./progressTerminalPresentation";

function stage(
  name: ProgressStageView["name"],
  state: ProgressStageView["state"],
  pct: number,
): ProgressStageView {
  return { name, state, pct, detail: [] };
}

const livePerformance: PerformanceSnapshot = {
  pipeline_mode: "parallel",
  parallel_worker_mode: "manual",
  active_workers: 0,
  max_workers: 4,
  requested_parallel_chapters: undefined,
  effective_parallel_chapters: undefined,
  queued_chapters: 0,
  running_chapters: 0,
  completed_chapters: 3,
  failed_chapters: 0,
  active_chapter_titles: [],
  cleaner_chunks_per_second: 1.2,
  tts_chars_per_second: 40,
  encoder_chapters_per_second: 0.5,
  elapsed_seconds: 12,
  eta_seconds: 0,
};

describe("dashboardNowStageLabel", () => {
  it("does not fall back to ingestion when all stages are done on a completed job", () => {
    const stages = doneStages();
    expect(dashboardNowStageLabel("completed", stages)).toBe("complete");
  });

  it("uses the active stage while running", () => {
    const stages = createProgressStages().map((item, index) =>
      index === 2 ? stage("cleaning", "active", 40) : item,
    );
    expect(dashboardNowStageLabel("running", stages)).toBe("cleaning");
  });
});

describe("buildProgressTerminalPresentation", () => {
  it("never presents live worker or active-task claims when completed", () => {
    const presentation = buildProgressTerminalPresentation({
      uiState: "completed" as ConversionJobStatus,
      stages: doneStages(),
      currentStage: "ingestion",
      currentMessage: "waiting for updates",
      performance: livePerformance,
      progressPct: 100,
    });

    expect(presentation.operationTitle).toBe("complete");
    expect(presentation.showLiveActiveTask).toBe(false);
    expect(presentation.showLiveWorkers).toBe(false);
    expect(presentation.workerSummary).not.toMatch(/0\/4/);
    expect(presentation.modeSummary.toLowerCase()).not.toContain("waiting");
    expect(presentation.activeTaskSummary).toMatch(/complete|finished|done/i);
  });

  it("preserves last meaningful throughput and elapsed when available on completed", () => {
    const presentation = buildProgressTerminalPresentation({
      uiState: "completed",
      stages: doneStages(),
      currentStage: "packaging",
      currentMessage: "done",
      performance: livePerformance,
      progressPct: 100,
    });

    expect(presentation.throughputSummary).toContain("chunk/s");
    expect(presentation.elapsedSummary).toContain("12");
    expect(presentation.snapshotKind).toBe("final");
  });

  it("renders a neutral completed summary when performance is unavailable", () => {
    const presentation = buildProgressTerminalPresentation({
      uiState: "completed",
      stages: doneStages(),
      currentStage: "",
      currentMessage: "",
      performance: null,
      progressPct: 100,
    });

    expect(presentation.operationTitle).toBe("complete");
    expect(presentation.showLiveWorkers).toBe(false);
    expect(presentation.showLiveActiveTask).toBe(false);
    expect(presentation.workerSummary).toMatch(/unavailable|not available|--/i);
    expect(presentation.throughputSummary).toMatch(/unavailable|not available|--/i);
    expect(presentation.snapshotKind).toBe("neutral");
  });

  it("keeps live metrics while running", () => {
    const stages = [
      stage("ingestion", "done", 100),
      stage("chunking", "done", 100),
      stage("cleaning", "active", 55),
      stage("synthesis", "pending", 0),
      stage("encoding", "pending", 0),
      stage("packaging", "pending", 0),
    ];
    const presentation = buildProgressTerminalPresentation({
      uiState: "running",
      stages,
      currentStage: "cleaning",
      currentMessage: "cleaning chapter 2",
      performance: livePerformance,
      progressPct: 40,
    });

    expect(presentation.operationTitle).toBe("cleaning");
    expect(presentation.showLiveWorkers).toBe(true);
    expect(presentation.showLiveActiveTask).toBe(true);
    expect(presentation.workerSummary).toContain("0/4");
    expect(presentation.snapshotKind).toBe("live");
  });

  it("treats cancelled and failed as terminal, not live", () => {
    for (const uiState of ["cancelled", "failed"] as const) {
      const presentation = buildProgressTerminalPresentation({
        uiState,
        stages: createProgressStages(),
        currentStage: "synthesis",
        currentMessage: uiState,
        performance: livePerformance,
        progressPct: 30,
      });
      expect(presentation.showLiveWorkers).toBe(false);
      expect(presentation.showLiveActiveTask).toBe(false);
      expect(presentation.snapshotKind).not.toBe("live");
    }
  });
});
