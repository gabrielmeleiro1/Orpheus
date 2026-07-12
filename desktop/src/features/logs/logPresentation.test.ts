import { describe, expect, it } from "vitest";

import type { EventStreamLogEntry } from "../../types/api";
import {
  filterVisibleLogs,
  formatLogsForClipboard,
  inferLogCategory,
} from "./logPresentation";

const logs: EventStreamLogEntry[] = [
  { id: 1, level: "info", ts: "10:00:01", msg: "state running (job-1)" },
  { id: 2, level: "warn", ts: "10:00:02", msg: "cleaner retry" },
  { id: 3, level: "error", ts: "10:00:03", msg: "conversion failed" },
  { id: 4, level: "info", ts: "10:00:04", msg: "sse connected" },
];

describe("logPresentation", () => {
  it("filters by level", () => {
    expect(filterVisibleLogs(logs, { level: "error", category: "all" }).map((entry) => entry.id)).toEqual([
      3,
    ]);
  });

  it("filters by inferred category", () => {
    expect(inferLogCategory(logs[0]!)).toBe("state");
    expect(inferLogCategory(logs[3]!)).toBe("transport");
    expect(
      filterVisibleLogs(logs, { level: "all", category: "state" }).map((entry) => entry.id),
    ).toEqual([1]);
  });

  it("formats visible logs for clipboard copy", () => {
    const visible = filterVisibleLogs(logs, { level: "warn", category: "all" });
    expect(formatLogsForClipboard(visible)).toBe("[10:00:02] warn cleaner retry");
  });
});
