import { describe, expect, it } from "vitest";

import type { EventStreamLogEntry } from "../../types/api";
import {
  countLogLevels,
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

  it("files the pipeline's success and process levels under Info everywhere", () => {
    const pipeline: EventStreamLogEntry[] = [
      { id: 5, level: "success", ts: "10:00:05", msg: "model loaded" },
      { id: 6, level: "process", ts: "10:00:06", msg: "warming up" },
      { id: 7, level: "WARNING", ts: "10:00:07", msg: "slow disk" },
    ];
    expect(countLogLevels(pipeline)).toEqual({ all: 3, info: 2, warn: 1, error: 0 });
    expect(filterVisibleLogs(pipeline, { level: "info", category: "all" }).map((entry) => entry.id)).toEqual([5, 6]);
    expect(formatLogsForClipboard(pipeline)).toBe("[10:00:05] info model loaded\n[10:00:06] info warming up\n[10:00:07] warn slow disk");
  });
});
