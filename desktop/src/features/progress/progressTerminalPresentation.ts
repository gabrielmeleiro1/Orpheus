import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";

/** One measured value for the performance list; unmeasured values are left out, never shown as placeholders. */
interface ProgressMetric {
  label: string;
  value: string;
  /** A short secondary line under the row, such as the chapters being narrated. */
  note?: string;
  tip?: string;
}

interface ProgressTerminalPresentation {
  operationTitle: string;
  showLiveActiveTask: boolean;
  /** Elapsed time ("2m 3s"), or "" before it is reported. */
  elapsed: string;
  metrics: ProgressMetric[];
}

const STAGE_ACRONYMS = ["TTS", "API", "CPU", "GPU", "CUDA", "ETA", "M4A", "M4B", "MLX", "ROCm", "PDF", "EPUB", "FP16", "OK"];

export function formatStageDisplayName(name: string): string {
  const label = String(name || "").replace(/_/g, " ").replace(/\b[A-Za-z][A-Za-z0-9]*\b/g, (word) => {
    const acronym = STAGE_ACRONYMS.find((value) => value.toLowerCase() === word.toLowerCase());
    return acronym ?? (/^[A-Z][A-Z0-9]+$/.test(word) ? word : word.toLowerCase());
  });
  return label.charAt(0).toUpperCase() + label.slice(1);
}

export function stageLabel(name: string): string {
  return String(name || "").replace(/_/g, " ");
}

function isTerminal(uiState: ConversionJobStatus): boolean {
  return uiState === "completed" || uiState === "failed" || uiState === "cancelled";
}

function isFiniteNumber(value: number | undefined | null): value is number {
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

/** Plain-language rows: what ran in parallel, what finished, how fast, how long. */
function buildMetrics(performance: PerformanceSnapshot | null, live: boolean): ProgressMetric[] {
  if (!performance) return [];
  const metrics: ProgressMetric[] = [];
  const maxWorkers = performance.max_workers;
  if (isFiniteNumber(maxWorkers)) {
    const titles = performance.active_chapter_titles ?? [];
    metrics.push({
      label: "Parallel chapters",
      value: live ? `${performance.active_workers ?? 0} of ${maxWorkers}` : String(maxWorkers),
      note: live && titles.length > 0 ? titles.slice(0, 3).join(", ") : undefined,
      tip: live
        ? `Requested ${performance.requested_parallel_chapters ?? "—"}, using ${performance.effective_parallel_chapters ?? "—"} after warm-up and memory limits`
        : undefined,
    });
  }
  if (isFiniteNumber(performance.completed_chapters)) {
    const failed = performance.failed_chapters ?? 0;
    metrics.push({
      label: "Chapters done",
      value: failed > 0 ? `${performance.completed_chapters} · ${failed} failed` : String(performance.completed_chapters),
    });
  }
  if (isFiniteNumber(performance.cleaner_chunks_per_second)) {
    metrics.push({ label: "Cleaning", value: `${formatMetric(performance.cleaner_chunks_per_second)} chunks/s` });
  }
  if (isFiniteNumber(performance.tts_chars_per_second)) {
    metrics.push({ label: "Narration", value: `${formatMetric(performance.tts_chars_per_second)} characters/s` });
  }
  if (isFiniteNumber(performance.encoder_chapters_per_second)) {
    metrics.push({ label: "Encoding", value: `${formatMetric(performance.encoder_chapters_per_second)} chapters/s` });
  }
  if (isFiniteNumber(performance.elapsed_seconds)) {
    metrics.push({ label: "Elapsed", value: formatSeconds(performance.elapsed_seconds) });
  }
  const modes = (live ? [performance.parallel_worker_mode, performance.pipeline_mode] : [performance.pipeline_mode])
    .filter((mode): mode is string => Boolean(mode?.trim()));
  if (modes.length > 0) {
    metrics.push({
      label: "Mode",
      value: modes.map((mode, index) => (index === 0 ? formatStageDisplayName(mode) : stageLabel(mode).toLowerCase())).join(", "),
      note: performance.fallback_reason ? `Fallback: ${performance.fallback_reason}` : undefined,
    });
  }
  return metrics;
}

// The pipeline's own log line when it runs fewer chapters at once than were asked for.
const PARALLEL_LIMIT_LOG = /^\[PARALLEL\] Using (\d+) workers? of (\d+) requested: (.+)$/;

/**
 * One sentence on why fewer chapters run at once than were requested; "" when
 * nothing was reduced. The reason comes from the event log (newest first), the
 * counts alone from the performance snapshot when that line is not there.
 */
export function parallelLimitNote(
  logs: ReadonlyArray<{ msg: string }>,
  performance: PerformanceSnapshot | null,
): string {
  for (const entry of logs) {
    const match = PARALLEL_LIMIT_LOG.exec(entry.msg);
    if (match) {
      const reason = match[3].trim().replace(/[.\s]+$/, "");
      return `Using ${match[1]} of ${match[2]} requested parallel chapters: ${reason}.`;
    }
  }
  const requested = performance?.requested_parallel_chapters;
  const effective = performance?.effective_parallel_chapters;
  return isFiniteNumber(requested) && isFiniteNumber(effective) && effective >= 1 && effective < requested
    ? `Using ${effective} of ${requested} requested parallel chapters.`
    : "";
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
  performance: PerformanceSnapshot | null;
}): ProgressTerminalPresentation {
  const { uiState, stages, currentStage, performance } = input;
  const terminal = isTerminal(uiState);
  const activeStage = stages.find((stage) => stage.state === "active") ?? null;
  const elapsed = isFiniteNumber(performance?.elapsed_seconds) ? formatSeconds(performance.elapsed_seconds) : "";

  if (!terminal) {
    return {
      operationTitle: stageLabel(activeStage?.name || currentStage || "waiting"),
      showLiveActiveTask: true,
      elapsed,
      metrics: buildMetrics(performance, true),
    };
  }

  const operationTitle =
    uiState === "completed"
      ? "complete"
      : uiState === "cancelled"
        ? "cancelled"
        : "failed";

  return {
    operationTitle,
    showLiveActiveTask: false,
    elapsed,
    metrics: hasMeaningfulSnapshot(performance) ? buildMetrics(performance, false) : [],
  };
}
