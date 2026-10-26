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
  const instanceId = normalizeString(source.backend_instance_id);
  if (instanceId) {
    normalized.backend_instance_id = instanceId;
  }
  if (typeof source.event_sequence === "number" && Number.isSafeInteger(source.event_sequence) && source.event_sequence > 0) {
    normalized.event_sequence = source.event_sequence;
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
    const numeric = finiteNumber(source[key]);
    if (numeric !== undefined) {
      normalized[key] = numeric;
    }
  }

  return normalized;
}

// `Number(null)` and `Number("")` are 0, which would turn an absent value into
// a real reading and defeat `overall_progress ?? progress` fallbacks.
function finiteNumber(value: unknown): number | undefined {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : undefined;
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : undefined;
  }
  return undefined;
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
