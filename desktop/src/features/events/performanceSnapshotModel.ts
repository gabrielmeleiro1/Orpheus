import type { PerformanceSnapshot } from "../../types/api";

const MONOTONIC_COUNTERS = ["completed_chapters", "failed_chapters"] as const;

function normalizeString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function normalizeOptionalNumber(value: unknown): number | undefined {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return undefined;
  }
  return Math.max(0, Math.round(numeric * 100) / 100);
}

export function normalizePerformanceSnapshot(value: unknown): PerformanceSnapshot | null {
  if (!value || typeof value !== "object") {
    return null;
  }

  const source = value as Record<string, unknown>;
  const snapshot: PerformanceSnapshot = {};
  const pipelineMode = normalizeString(source.pipeline_mode).toLowerCase();
  const fallbackReason = normalizeString(source.fallback_reason);
  const activeTitles = Array.isArray(source.active_chapter_titles)
    ? source.active_chapter_titles
        .map((item) => normalizeString(item))
        .filter((item) => item.length > 0)
    : [];

  if (pipelineMode) {
    snapshot.pipeline_mode = pipelineMode;
  }
  if (fallbackReason) {
    snapshot.fallback_reason = fallbackReason;
  }
  const parallelWorkerMode = normalizeString(source.parallel_worker_mode).toLowerCase();
  if (parallelWorkerMode) {
    snapshot.parallel_worker_mode = parallelWorkerMode;
  }
  if (activeTitles.length > 0) {
    snapshot.active_chapter_titles = activeTitles;
  }

  for (const key of [
    "requested_parallel_chapters",
    "effective_parallel_chapters",
    "active_workers",
    "max_workers",
    "queued_chapters",
    "running_chapters",
    "completed_chapters",
    "failed_chapters",
    "cleaner_chunks_per_second",
    "tts_chars_per_second",
    "encoder_chapters_per_second",
    "elapsed_seconds",
    "eta_seconds",
  ] as const) {
    const normalized = normalizeOptionalNumber(source[key]);
    if (normalized !== undefined) {
      snapshot[key] = normalized;
    }
  }

  return Object.keys(snapshot).length > 0 ? snapshot : null;
}

/**
 * Merge live snapshots without allowing a later idle/cleanup snapshot to
 * erase chapter counts already reported for the active job. The backend can
 * emit a final packaging update from the parent process after a worker-pool
 * update; that parent snapshot has zero local worker counters.
 */
export function mergePerformanceSnapshots(
  previous: PerformanceSnapshot | null,
  next: PerformanceSnapshot | undefined,
): PerformanceSnapshot | null {
  if (!next) {
    return previous;
  }
  if (!previous) {
    return next;
  }

  const merged: PerformanceSnapshot = { ...previous, ...next };
  for (const key of MONOTONIC_COUNTERS) {
    const previousValue = previous[key];
    const nextValue = next[key];
    if (typeof previousValue === "number" && typeof nextValue === "number") {
      merged[key] = Math.max(previousValue, nextValue);
    }
  }
  return merged;
}
