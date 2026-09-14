import type { ConversionMode, SourcePreflight } from "../../types/api";
import { splitPath } from "./conversionPanelHelpers";

export const NO_READABLE_CHAPTERS_ERROR = "No readable chapters found in this source.";
export const OCR_REQUIRED_ERROR = "This PDF requires OCR before conversion.";

export function preflightBlockingReason(preflight: SourcePreflight): string | null {
  if (preflight.requires_ocr) {
    return OCR_REQUIRED_ERROR;
  }
  if (Number(preflight.chapter_count) <= 0) {
    return NO_READABLE_CHAPTERS_ERROR;
  }
  return null;
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
