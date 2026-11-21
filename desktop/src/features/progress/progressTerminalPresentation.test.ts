import { describe, expect, it } from "vitest";

import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";
import { createProgressStages, doneStages } from "../events/progressStageModel";
import {
  buildProgressTerminalPresentation,
  dashboardNowStageLabel,
  formatStageDisplayName,
  parallelLimitNote,
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
      performance: livePerformance,
    });

    expect(presentation.operationTitle).toBe("complete");
    expect(presentation.showLiveActiveTask).toBe(false);
    const values = Object.fromEntries(presentation.metrics.map((metric) => [metric.label, metric.value]));
    // A finished job reports its pool size, never a live "0 of 4".
    expect(values["Parallel chapters"]).toBe("4");
    expect(values.Mode).toBe("Parallel");
    expect(presentation.metrics.some((metric) => metric.value.toLowerCase().includes("waiting"))).toBe(false);
  });

  it("preserves last meaningful throughput and elapsed when available on completed", () => {
    const presentation = buildProgressTerminalPresentation({
      uiState: "completed",
      stages: doneStages(),
      currentStage: "packaging",
      performance: livePerformance,
    });

    expect(presentation.metrics.map((metric) => [metric.label, metric.value])).toEqual([
      ["Parallel chapters", "4"],
      ["Chapters done", "3"],
      ["Cleaning", "1.20 chunks/s"],
      ["Narration", "40.0 characters/s"],
      ["Encoding", "0.50 chapters/s"],
      ["Elapsed", "12s"],
      ["Mode", "Parallel"],
    ]);
    expect(presentation.elapsed).toBe("12s");
  });

  it("renders a neutral completed summary when performance is unavailable", () => {
    const presentation = buildProgressTerminalPresentation({
      uiState: "completed",
      stages: doneStages(),
      currentStage: "",
      performance: null,
    });

    expect(presentation.operationTitle).toBe("complete");
    expect(presentation.showLiveActiveTask).toBe(false);
    expect(presentation.metrics).toEqual([]);
    expect(presentation.elapsed).toBe("");
  });

  it("avoids terminal-style placeholders while running before performance data arrives", () => {
    const stages = createProgressStages().map((item, index) =>
      index === 0 ? stage("ingestion", "active", 5) : item,
    );
    const presentation = buildProgressTerminalPresentation({
      uiState: "running",
      stages,
      currentStage: "ingestion",
      performance: null,
    });

    // Nothing measured yet means nothing listed: no "—" or "Not reported" rows.
    expect(presentation.metrics).toEqual([]);
    expect(presentation.elapsed).toBe("");
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
      performance: livePerformance,
    });

    expect(presentation.operationTitle).toBe("cleaning");
    expect(presentation.showLiveActiveTask).toBe(true);
    const values = Object.fromEntries(presentation.metrics.map((metric) => [metric.label, metric.value]));
    expect(values["Parallel chapters"]).toBe("0 of 4");
    expect(values.Mode).toBe("Manual, parallel");
    expect(values["Chapters done"]).toBe("3");
    expect(buildProgressTerminalPresentation({
      uiState: "running", stages, currentStage: "cleaning",
      performance: { ...livePerformance, failed_chapters: 1 },
    }).metrics.find((metric) => metric.label === "Chapters done")?.value).toBe("3 · 1 failed");
  });

  it("treats cancelled and failed as terminal, not live", () => {
    for (const uiState of ["cancelled", "failed"] as const) {
      const presentation = buildProgressTerminalPresentation({
        uiState,
        stages: createProgressStages(),
        currentStage: "synthesis",
        performance: livePerformance,
      });
      expect(presentation.showLiveActiveTask).toBe(false);
    }
  });
});

describe("formatStageDisplayName", () => {
  it.each([
    ["audio_encoding", "Audio encoding"],
    ["Text Cleaning", "Text cleaning"],
    ["tts_synthesis", "TTS synthesis"],
    ["loading_CUDA", "Loading CUDA"],
    ["API_request", "API request"],
    ["m4b_packaging", "M4B packaging"],
    ["rocm_setup", "ROCm setup"],
    ["pdf_ingestion", "PDF ingestion"],
    ["epub_ingestion", "EPUB ingestion"],
    ["fp16", "FP16"],
  ])("displays %s as %s", (name, expected) => {
    expect(formatStageDisplayName(name)).toBe(expected);
  });
});

describe("parallelLimitNote", () => {
  const log = (msg: string) => ({ msg });

  it("says why fewer chapters run at once than were requested, from the newest matching event", () => {
    expect(parallelLimitNote([
      log("Chapter 2 cleaned"),
      log("[PARALLEL] Using 1 worker of 2 requested: the TTS model and the CUDA cleaner share one GPU"),
      log("[PARALLEL] Using 2 workers of 4 requested: memory headroom limits parallel workers"),
    ], null)).toBe("Using 1 of 2 requested parallel chapters: the TTS model and the CUDA cleaner share one GPU.");
  });

  it("falls back to the measured counts when the event is not in the log", () => {
    const performance: PerformanceSnapshot = { requested_parallel_chapters: 2, effective_parallel_chapters: 1 };
    expect(parallelLimitNote([log("[PARALLEL] Using shared worker pool manager")], performance))
      .toBe("Using 1 of 2 requested parallel chapters.");
  });

  it("stays silent when the request was honoured", () => {
    expect(parallelLimitNote([], { requested_parallel_chapters: 2, effective_parallel_chapters: 2 })).toBe("");
    expect(parallelLimitNote([log("[PARALLEL] Using 2 concurrent workers (manual)")], null)).toBe("");
  });
});
