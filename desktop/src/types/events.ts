import type { ConversionJobStatus } from "./conversion";

export type ProgressStageName =
  | "ingestion"
  | "chunking"
  | "cleaning"
  | "synthesis"
  | "encoding"
  | "packaging";

type ProgressStageState = "pending" | "active" | "done";

export interface ProgressStageDetail {
  label: string;
  value: string;
  pct: number;
}

export interface ProgressStageView {
  name: ProgressStageName;
  state: ProgressStageState;
  pct: number;
  detail: ProgressStageDetail[];
}

export interface PerformanceSnapshot {
  pipeline_mode?: string;
  parallel_worker_mode?: string;
  requested_parallel_chapters?: number;
  effective_parallel_chapters?: number;
  active_workers?: number;
  max_workers?: number;
  queued_chapters?: number;
  running_chapters?: number;
  completed_chapters?: number;
  failed_chapters?: number;
  active_chapter_titles?: string[];
  cleaner_chunks_per_second?: number;
  tts_chars_per_second?: number;
  encoder_chapters_per_second?: number;
  elapsed_seconds?: number;
  eta_seconds?: number;
  fallback_reason?: string;
}

export interface EventStreamLogEntry {
  id: number;
  level: string;
  ts: string;
  msg: string;
}

export type EventStreamConnectionState = "idle" | "connecting" | "open" | "error";

export interface DesktopEventPayload {
  event_id?: string;
  timestamp?: string;
  event_type?: string;
  state?: ConversionJobStatus | string;
  stage?: string;
  progress?: number;
  overall_progress?: number;
  stage_progress?: number;
  chapter_current?: number;
  chapter_total?: number;
  chunk_current?: number;
  chunk_total?: number;
  message?: string;
  level?: string;
  job_id?: string;
  eta?: number | null;
  performance?: PerformanceSnapshot;
}
