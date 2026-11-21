import type { EventStreamLogEntry } from "../../types/api";

export type LogLevelFilter = "all" | LogLevel;
type LogLevel = "info" | "warn" | "error";
export type LogCategoryFilter = "all" | "state" | "transport" | "conversion" | "other";

export function inferLogCategory(entry: EventStreamLogEntry): Exclude<LogCategoryFilter, "all"> {
  const msg = String(entry.msg || "").toLowerCase();
  if (msg.startsWith("state ") || msg.includes(" state ")) {
    return "state";
  }
  if (
    msg.includes("sse") ||
    msg.includes("event stream") ||
    msg.includes("websocket") ||
    msg.includes("reconnect") ||
    msg.includes("transport")
  ) {
    return "transport";
  }
  if (
    msg.includes("conversion") ||
    msg.includes("cleaner") ||
    msg.includes("tts") ||
    msg.includes("chapter") ||
    msg.includes("job ")
  ) {
    return "conversion";
  }
  return "other";
}

/** The pipeline also emits levels such as "success" and "process"; they read as info. */
export function normalizeLogLevel(level: string): LogLevel {
  const normalized = String(level || "").toLowerCase();
  if (normalized === "warn" || normalized === "warning") {
    return "warn";
  }
  return normalized === "error" ? "error" : "info";
}

export function filterVisibleLogs(
  logs: EventStreamLogEntry[],
  filters: { level: LogLevelFilter; category: LogCategoryFilter; query?: string },
): EventStreamLogEntry[] {
  const query = (filters.query ?? "").trim().toLowerCase();
  return logs.filter((entry) => {
    const level = normalizeLogLevel(entry.level);
    if (filters.level !== "all" && level !== filters.level) {
      return false;
    }
    if (filters.category !== "all" && inferLogCategory(entry) !== filters.category) {
      return false;
    }
    if (query && !String(entry.msg || "").toLowerCase().includes(query)) {
      return false;
    }
    return true;
  });
}

interface LogLevelCounts {
  all: number;
  info: number;
  warn: number;
  error: number;
}

export function countLogLevels(logs: EventStreamLogEntry[]): LogLevelCounts {
  const counts: LogLevelCounts = { all: logs.length, info: 0, warn: 0, error: 0 };
  for (const entry of logs) {
    counts[normalizeLogLevel(entry.level)] += 1;
  }
  return counts;
}

export function formatLogsForClipboard(logs: EventStreamLogEntry[]): string {
  return logs
    .map((entry) => `[${entry.ts}] ${normalizeLogLevel(entry.level)} ${entry.msg}`)
    .join("\n");
}
