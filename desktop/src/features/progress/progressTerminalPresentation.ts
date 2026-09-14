import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";

type ProgressSnapshotKind = "live" | "final" | "neutral";

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

function isFiniteNumber(value: number | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function formatMetric(value: number): string {
  return value >= 100 ? Math.round(value).toString() : value.toFixed(value >= 10 ? 1 : 2);
}

function formatSeconds(value: number): string {
  const total = Math.max(0, Math.round(value));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function throughputSummary(performance: PerformanceSnapshot | null, fallback: string): string {
  const cleaner = performance?.cleaner_chunks_per_second;
  const tts = performance?.tts_chars_per_second;
  const encoder = performance?.encoder_chapters_per_second;
  const parts: string[] = [];
  if (isFiniteNumber(cleaner)) {
    parts.push(`${formatMetric(cleaner)} chunk/s clean`);
  }
  if (isFiniteNumber(tts)) {
    parts.push(`${formatMetric(tts)} char/s tts`);
  }
  if (isFiniteNumber(encoder)) {
    parts.push(`${formatMetric(encoder)} chapter/s encode`);
  }
  return parts.length > 0 ? parts.join(" · ") : fallback;
}

function elapsedSummary(performance: PerformanceSnapshot | null, fallback: string): string {
  const elapsed = performance?.elapsed_seconds;
  return isFiniteNumber(elapsed) ? `Elapsed ${formatSeconds(elapsed)}` : fallback;
}

function workerPoolSummary(performance: PerformanceSnapshot | null): string | null {
  const maxWorkers = performance?.max_workers;
  if (!isFiniteNumber(maxWorkers)) {
    return null;
  }
  return `${performance?.active_workers ?? 0}/${maxWorkers}`;
}

function terminalTaskSummary(uiState: ConversionJobStatus, currentMessage: string): string {
  if (uiState === "completed") {
    return "Conversion complete";
  }
  if (currentMessage) {
    return currentMessage;
  }
  return uiState === "cancelled" ? "Conversion cancelled" : "Conversion failed";
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
    const workerMode = performance?.parallel_worker_mode;
    const pipelineMode = performance?.pipeline_mode;
    return {
      operationTitle: stageLabel(activeStage?.name || currentStage || "waiting"),
      showLiveWorkers: true,
      showLiveActiveTask: true,
      workerSummary: workerPoolSummary(performance) ?? "—",
      modeSummary:
        workerMode || pipelineMode ? `${workerMode || "—"} / ${pipelineMode || "—"}` : "Not reported yet",
      throughputSummary: throughputSummary(performance, "Measuring…"),
      elapsedSummary: elapsedSummary(performance, "Elapsed —"),
      activeTaskSummary: currentMessage || "Waiting for updates",
      queueSummary: `${performance?.running_chapters ?? 0} running · ${performance?.queued_chapters ?? 0} queued`,
      chaptersSummary: `${performance?.completed_chapters ?? 0} done · ${performance?.failed_chapters ?? 0} failed`,
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
      workerSummary: "Not available",
      modeSummary: "Not recorded",
      throughputSummary: "Not available",
      elapsedSummary: "Elapsed not recorded",
      activeTaskSummary: terminalTaskSummary(uiState, currentMessage),
      queueSummary: "Queue idle",
      chaptersSummary: "—",
      snapshotKind: "neutral",
    };
  }

  return {
    operationTitle,
    showLiveWorkers: false,
    showLiveActiveTask: false,
    workerSummary: isFiniteNumber(performance?.max_workers)
      ? `${performance?.max_workers} ${performance?.max_workers === 1 ? "worker" : "workers"}`
      : "Not available",
    modeSummary: performance?.pipeline_mode || "Not recorded",
    throughputSummary: throughputSummary(performance, "Not available"),
    elapsedSummary: elapsedSummary(performance, "Elapsed not recorded"),
    activeTaskSummary: terminalTaskSummary(uiState, currentMessage),
    queueSummary: "Queue idle",
    chaptersSummary: `${performance?.completed_chapters ?? 0} done · ${performance?.failed_chapters ?? 0} failed`,
    snapshotKind: "final",
  };
}
