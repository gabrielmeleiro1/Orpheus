import { describe, expect, it } from "vitest";

import { mergePerformanceSnapshots, normalizePerformanceSnapshot } from "./performanceSnapshotModel";

describe("normalizePerformanceSnapshot", () => {
  it("treats null and blank numeric fields as absent rather than zero", () => {
    expect(
      normalizePerformanceSnapshot({
        tts_chars_per_second: null,
        eta_seconds: "",
        elapsed_seconds: "  ",
        active_workers: undefined,
      }),
    ).toBeNull();
  });

  it("still accepts real numbers and numeric strings, including zero", () => {
    expect(
      normalizePerformanceSnapshot({ tts_chars_per_second: "12.345", eta_seconds: 0, active_workers: 2 }),
    ).toEqual({ tts_chars_per_second: 12.35, eta_seconds: 0, active_workers: 2 });
  });

  it("keeps the previous reading when a later snapshot reports null", () => {
    const previous = normalizePerformanceSnapshot({ tts_chars_per_second: 40, eta_seconds: 90 });
    const next = normalizePerformanceSnapshot({ tts_chars_per_second: null, eta_seconds: 80 });
    expect(mergePerformanceSnapshots(previous, next ?? undefined)).toEqual({
      tts_chars_per_second: 40,
      eta_seconds: 80,
    });
  });
});
