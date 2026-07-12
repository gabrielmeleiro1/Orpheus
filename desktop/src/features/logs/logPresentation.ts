import type { EventStreamLogEntry } from "../../types/api";

export type LogLevelFilter = "all" | "info" | "warn" | "error";
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

function normalizeLevel(level: string): string {
  const normalized = String(level || "").toLowerCase();
  if (normalized === "warning") {
    return "warn";
  }
  return normalized;
}

export function filterVisibleLogs(
  logs: EventStreamLogEntry[],
  filters: { level: LogLevelFilter; category: LogCategoryFilter },
): EventStreamLogEntry[] {
  return logs.filter((entry) => {
    const level = normalizeLevel(entry.level);
    if (filters.level !== "all" && level !== filters.level) {
      return false;
    }
    if (filters.category !== "all" && inferLogCategory(entry) !== filters.category) {
      return false;
    }
    return true;
  });
}

export function formatLogsForClipboard(logs: EventStreamLogEntry[]): string {
  return logs
    .map((entry) => `[${entry.ts}] ${normalizeLevel(entry.level)} ${entry.msg}`)
    .join("\n");
}
