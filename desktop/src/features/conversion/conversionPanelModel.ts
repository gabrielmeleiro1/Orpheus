import type { ConversionMode, SourcePreflight } from "../../types/api";
import { splitPath } from "./conversionPanelHelpers";

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

  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
    conversionMode,
    sourcePath: normalizedPath,
    label: label?.trim() || splitPath(normalizedPath),
    title: resolvedTitle,
    author: resolvedAuthor,
    status: preflight?.warnings?.length ? "warning" : preflight ? "ready" : "preflighting",
    error: "",
    preflight: preflight ?? null,
    jobId: null,
  };
}
