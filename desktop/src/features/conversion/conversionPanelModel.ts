import type { ConversionMode, SourcePreflight } from "../../types/api";
import { formatListeningLength } from "../../format/time";
import { splitPath } from "./conversionPanelHelpers";

const NO_READABLE_CHAPTERS_ERROR = "No readable chapters found in this source.";
const OCR_REQUIRED_ERROR = "This PDF requires OCR before conversion.";
// Shorter sources finish soon enough that their length is not worth a line.
const LONG_SOURCE_AUDIO_SECONDS = 60 * 60;

export function preflightBlockingReason(preflight: SourcePreflight): string | null {
  // The backend names the exact problem (password, DRM, damage, empty file);
  // the generic reasons below only cover what it did not explain.
  const backendReason = preflight.blocking_error?.message?.trim();
  if (backendReason) {
    return backendReason;
  }
  if (preflight.requires_ocr) {
    return OCR_REQUIRED_ERROR;
  }
  if (Number(preflight.chapter_count) <= 0) {
    return NO_READABLE_CHAPTERS_ERROR;
  }
  return null;
}

/** "About 3 hr 20 min of audio" for a long source; "" when short or not estimated. */
export function estimatedAudioNote(preflight: SourcePreflight | null): string {
  const seconds = Number(preflight?.estimated_audio_seconds);
  if (!Number.isFinite(seconds) || seconds < LONG_SOURCE_AUDIO_SECONDS) {
    return "";
  }
  return `About ${formatListeningLength(seconds)} of audio`;
}

export function isPreflightConvertible(preflight: SourcePreflight): boolean {
  return preflightBlockingReason(preflight) === null;
}

type QueueItemStatus = "ready" | "preflighting" | "warning" | "converting" | "done" | "failed";

export interface ConversionQueueItem {
  id: string;
  conversionMode: ConversionMode;
  sourcePath: string;
  label: string;
  title: string;
  author: string;
  status: QueueItemStatus;
  error: string;
  preflight: SourcePreflight | null;
  jobId: string | null;
  /** The library book this row's job wrote to, once known; links the row to a later repair. */
  bookId?: number | null;
}

/** A row whose conversion ended, in any way; a row that never started is not finished. */
export function isFinishedQueueItem(item: ConversionQueueItem): boolean {
  return item.status === "done" || ((item.status === "failed" || item.status === "warning") && item.jobId !== null);
}

export function createQueueItem(
  sourcePath: string,
  conversionMode: ConversionMode,
  label?: string,
  preflight?: SourcePreflight | null,
): ConversionQueueItem {
  const normalizedPath = sourcePath.trim();
  const resolvedTitle = preflight?.title?.trim() || "";
  const resolvedAuthor = preflight?.author?.trim() || "";
  const preflightError = preflight ? preflightBlockingReason(preflight) : null;

  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
    conversionMode,
    sourcePath: normalizedPath,
    label: label?.trim() || splitPath(normalizedPath),
    title: resolvedTitle,
    author: resolvedAuthor,
    status: preflightError
      ? "failed"
      : preflight?.warnings?.length
        ? "warning"
        : preflight
          ? "ready"
          : "preflighting",
    error: preflightError ?? "",
    preflight: preflight ?? null,
    jobId: null,
  };
}
