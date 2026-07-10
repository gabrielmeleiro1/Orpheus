import type { EventStreamLogEntry } from "../../types/api";
import { normalizeString } from "./eventPayloadNormalizer";
import type { EventStreamModelState } from "./eventStreamReducer";

const MAX_LOG_LINES = 300;

export type LogActionFields = {
  logId: number;
  fallbackTimestamp?: string;
};

function formatTimestamp(value: string | undefined, fallbackTimestamp = ""): string {
  if (!value) {
    return fallbackTimestamp;
  }

  const parsed = new Date(value);
  if (!Number.isNaN(parsed.getTime())) {
    return parsed.toLocaleTimeString("en-US", { hour12: false });
  }

  return fallbackTimestamp;
}

export function addLog(
  state: EventStreamModelState,
  level: string,
  message: string,
  timestamp: string | undefined,
  logId: number,
  fallbackTimestamp?: string,
): EventStreamModelState {
  const normalizedMessage = normalizeString(message);
  if (!normalizedMessage) {
    return state;
  }

  const next: EventStreamLogEntry[] = [
    {
      id: logId,
      level: normalizeString(level) || "info",
      ts: formatTimestamp(timestamp, fallbackTimestamp),
      msg: normalizedMessage,
    },
    ...state.logs,
  ];
  if (next.length > MAX_LOG_LINES) {
    next.length = MAX_LOG_LINES;
  }
  return { ...state, logs: next };
}
