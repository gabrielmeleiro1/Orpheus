import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";

export type ProgressSnapshotKind = "live" | "final" | "neutral";

export interface ProgressTerminalPresentation {
  operationTitle: string;
  showLiveWorkers: boolean;
  showLiveActiveTask: boolean;
  workerSummary: string;
  modeSummary: string;
  throughputSummary: string;
  elapsedSummary: string;
  activeTaskSummary: string;
  queueSummary: string;
  chaptersSummary: string;
  snapshotKind: ProgressSnapshotKind;
}

function stageLabel(name: string): string {
  return String(name || "").replace(/_/g, " ");
}

function isTerminal(uiState: ConversionJobStatus): boolean {
  return uiState === "completed" || uiState === "failed" || uiState === "cancelled";
}

function formatMetric(value: number | undefined, suffix = ""): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  const formatted = value >= 100 ? Math.round(value).toString() : value.toFixed(value >= 10 ? 1 : 2);
  return suffix ? `${formatted} ${suffix}` : formatted;
}

function formatSeconds(value: number | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  const total = Math.max(0, Math.round(value));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function hasMeaningfulSnapshot(performance: PerformanceSnapshot | null): boolean {
  if (!performance) {
    return false;
  }
  return (
    typeof performance.elapsed_seconds === "number" ||
    typeof performance.completed_chapters === "number" ||
    typeof performance.cleaner_chunks_per_second === "number" ||
    typeof performance.tts_chars_per_second === "number" ||
    typeof performance.encoder_chapters_per_second === "number" ||
    typeof performance.max_workers === "number"
  );
}

export function dashboardNowStageLabel(
  uiState: ConversionJobStatus,
  stages: ProgressStageView[],
): string {
  if (uiState === "completed" || stages.every((stage) => stage.state === "done")) {
    return "complete";
  }
  if (uiState === "cancelled") {
    return "cancelled";
  }
  if (uiState === "failed") {
    return "failed";
  }
  const active = stages.find((stage) => stage.state === "active");
  if (active) {
    return stageLabel(active.name);
  }
  return "waiting";
}

export function buildProgressTerminalPresentation(input: {
  uiState: ConversionJobStatus;
  stages: ProgressStageView[];
  currentStage: string;
  currentMessage: string;
  performance: PerformanceSnapshot | null;
  progressPct: number;
}): ProgressTerminalPresentation {
  const { uiState, stages, currentStage, currentMessage, performance } = input;
  const terminal = isTerminal(uiState);
  const activeStage = stages.find((stage) => stage.state === "active") ?? null;
  const meaningful = hasMeaningfulSnapshot(performance);

  if (!terminal) {
    const workerText =
      typeof performance?.max_workers === "number"
        ? `${performance.active_workers ?? 0}/${performance.max_workers}`
        : "--";
    const selectedWorkerMode = performance?.parallel_worker_mode || "waiting";
    return {
      operationTitle: stageLabel(activeStage?.name || currentStage || "waiting"),
      showLiveWorkers: true,
      showLiveActiveTask: true,
      workerSummary: workerText,
      modeSummary: `${selectedWorkerMode} / ${performance?.pipeline_mode || "waiting"}`,
      throughputSummary: [
        `${formatMetric(performance?.cleaner_chunks_per_second, "chunk/s")} clean`,
        `${formatMetric(performance?.tts_chars_per_second, "char/s")} tts`,
        `${formatMetric(performance?.encoder_chapters_per_second, "chapter/s")} encode`,
      ].join(" · "),
      elapsedSummary: `elapsed ${formatSeconds(performance?.elapsed_seconds)}`,
      activeTaskSummary: currentMessage || "waiting for updates",
      queueSummary: `${performance?.running_chapters ?? 0} running / ${performance?.queued_chapters ?? 0} queued`,
      chaptersSummary: `${performance?.completed_chapters ?? 0} done / ${performance?.failed_chapters ?? 0} failed`,
      snapshotKind: "live",
    };
  }

  const operationTitle =
    uiState === "completed"
      ? "complete"
      : uiState === "cancelled"
        ? "cancelled"
        : uiState === "failed"
          ? "failed"
          : stageLabel(currentStage || "done");

  if (!meaningful) {
    return {
      operationTitle,
      showLiveWorkers: false,
      showLiveActiveTask: false,
      workerSummary: "unavailable",
      modeSummary: `${uiState} · final`,
      throughputSummary: "unavailable",
      elapsedSummary: "elapsed unavailable",
      activeTaskSummary: uiState === "completed" ? "conversion complete" : currentMessage || uiState,
      queueSummary: "queue idle",
      chaptersSummary: "—",
      snapshotKind: "neutral",
    };
  }

  const finalWorkers =
    typeof performance?.max_workers === "number"
      ? `final ${performance.max_workers} workers`
      : "final workers unavailable";

  return {
    operationTitle,
    showLiveWorkers: false,
    showLiveActiveTask: false,
    workerSummary: finalWorkers,
    modeSummary: `final · ${performance?.pipeline_mode || uiState}`,
    throughputSummary: [
      `${formatMetric(performance?.cleaner_chunks_per_second, "chunk/s")} clean`,
      `${formatMetric(performance?.tts_chars_per_second, "char/s")} tts`,
      `${formatMetric(performance?.encoder_chapters_per_second, "chapter/s")} encode`,
    ].join(" · "),
    elapsedSummary: `elapsed ${formatSeconds(performance?.elapsed_seconds)}`,
    activeTaskSummary: uiState === "completed" ? "conversion complete" : currentMessage || uiState,
    queueSummary: "queue idle",
    chaptersSummary: `${performance?.completed_chapters ?? 0} done / ${performance?.failed_chapters ?? 0} failed`,
    snapshotKind: "final",
  };
}
