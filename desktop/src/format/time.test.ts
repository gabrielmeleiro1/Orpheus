import { describe, expect, it } from "vitest";

import {
  formatClock,
  formatClockWithHours,
  formatDurationClock,
  formatDurationClockWithHours,
} from "./time";

describe("formatClock", () => {
  it("renders MM:SS with zero padding", () => {
    expect(formatClock(0)).toBe("00:00");
    expect(formatClock(5)).toBe("00:05");
    expect(formatClock(65)).toBe("01:05");
    expect(formatClock(600)).toBe("10:00");
  });

  it("never renders an hours segment even past an hour", () => {
    expect(formatClock(3661)).toBe("61:01");
  });

  it("floors fractional seconds and clamps negatives to zero", () => {
    expect(formatClock(59.9)).toBe("00:59");
    expect(formatClock(-10)).toBe("00:00");
  });

  it("coerces non-finite input to zero", () => {
    expect(formatClock(Number.NaN)).toBe("00:00");
  });
});

describe("formatClockWithHours", () => {
  it("renders MM:SS below an hour", () => {
    expect(formatClockWithHours(0)).toBe("00:00");
    expect(formatClockWithHours(65)).toBe("01:05");
    expect(formatClockWithHours(3599)).toBe("59:59");
  });

  it("adds an hours segment at or above an hour", () => {
    expect(formatClockWithHours(3600)).toBe("1:00:00");
    expect(formatClockWithHours(3661)).toBe("1:01:01");
    expect(formatClockWithHours(7325)).toBe("2:02:05");
  });

  it("clamps negatives and non-finite input to zero", () => {
    expect(formatClockWithHours(-5)).toBe("00:00");
    expect(formatClockWithHours(Number.POSITIVE_INFINITY)).toBe("00:00");
  });
});

describe("formatDurationClock", () => {
  it("returns a placeholder for empty or non-positive durations", () => {
    expect(formatDurationClock(null)).toBe("--:--");
    expect(formatDurationClock(0)).toBe("--:--");
    expect(formatDurationClock(-100)).toBe("--:--");
  });

  it("renders MM:SS from milliseconds without hours", () => {
    expect(formatDurationClock(65_000)).toBe("01:05");
    expect(formatDurationClock(3_661_000)).toBe("61:01");
  });
});

describe("formatDurationClockWithHours", () => {
  it("returns a placeholder for empty or non-positive durations", () => {
    expect(formatDurationClockWithHours(null)).toBe("--:--");
    expect(formatDurationClockWithHours(0)).toBe("--:--");
  });

  it("renders an hours segment from milliseconds when needed", () => {
    expect(formatDurationClockWithHours(65_000)).toBe("01:05");
    expect(formatDurationClockWithHours(3_661_000)).toBe("1:01:01");
  });
});
