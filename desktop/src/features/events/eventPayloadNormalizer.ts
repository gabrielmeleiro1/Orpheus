import type {
  ConversionJobStatus,
  ConversionJobTerminalEvent,
  ConversionTerminalJobStatus,
  DesktopEventPayload,
} from "../../types/api";
import { normalizePerformanceSnapshot } from "./performanceSnapshotModel";

export function normalizeString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function normalizeState(state: string): ConversionJobStatus {
  if (state === "idle") {
    return "idle";
  }
  if (state === "running") {
    return "running";
  }
  if (state === "completed") {
    return "completed";
  }
  if (state === "failed") {
    return "failed";
  }
  if (state === "cancelled") {
    return "cancelled";
  }
  return "unknown";
}

export function normalizeTerminalStatus(value: unknown): ConversionTerminalJobStatus | null {
  const normalized = normalizeString(value).toLowerCase();
  if (normalized === "completed" || normalized === "failed" || normalized === "cancelled") {
    return normalized;
  }
  return null;
}

export function normalizeEventPayload(payload: unknown): DesktopEventPayload | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const source = payload as Record<string, unknown>;
  const eventType = normalizeString(source.event_type).toLowerCase();
  if (!eventType) {
    return null;
  }

  const eventId = normalizeString(source.event_id);
  const timestamp = normalizeString(source.timestamp);
  const state = normalizeString(source.state).toLowerCase();
  const stage = normalizeString(source.stage);
  const message = normalizeString(source.message);
  const level = normalizeString(source.level).toLowerCase();
  const jobId = normalizeString(source.job_id);
  const performance = normalizePerformanceSnapshot(source.performance);

  const normalized: DesktopEventPayload = {
    event_type: eventType,
  };

  if (eventId) {
    normalized.event_id = eventId;
  }
  if (timestamp) {
    normalized.timestamp = timestamp;
  }
  if (state) {
    normalized.state = state;
  }
  if (stage) {
    normalized.stage = stage;
  }
  if (message) {
    normalized.message = message;
  }
  if (level) {
    normalized.level = level;
  }
  if (jobId) {
    normalized.job_id = jobId;
  }
  if (performance) {
    normalized.performance = performance;
  }

  for (const key of [
    "progress",
    "overall_progress",
    "stage_progress",
    "eta",
    "chapter_current",
    "chapter_total",
    "chunk_current",
    "chunk_total",
  ] as const) {
    const numeric = Number(source[key]);
    if (Number.isFinite(numeric)) {
      normalized[key] = numeric;
    }
  }

  return normalized;
}

export function terminalEventFromStatePayload(
  payload: DesktopEventPayload | null,
): ConversionJobTerminalEvent | undefined {
  if (!payload) {
    return undefined;
  }
  const jobId = normalizeString(payload.job_id);
  const status = normalizeTerminalStatus(payload.state);
  if (!jobId || !status) {
    return undefined;
  }
  return {
    jobId,
    status,
    message: normalizeString(payload.message) || `conversion ${status}`,
  };
}
