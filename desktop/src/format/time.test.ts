import { describe, expect, it } from "vitest";

import {
  formatClock,
  formatClockWithHours,
  formatDurationClock,
  formatListeningLength,
  formatRemainingClock,
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

describe("formatListeningLength", () => {
  it("reads as words at every scale", () => {
    expect(formatListeningLength(0)).toBe("0 sec");
    expect(formatListeningLength(45.4)).toBe("45 sec");
    expect(formatListeningLength(89)).toBe("1 min");
    expect(formatListeningLength(12 * 60 + 20)).toBe("12 min");
    expect(formatListeningLength(3600)).toBe("1 hr");
    expect(formatListeningLength(3 * 3600 + 5 * 60)).toBe("3 hr 5 min");
    // Truncated like every clock in the app: 59:40 is not yet an hour.
    expect(formatListeningLength(59 * 60 + 40)).toBe("59 min");
    expect(formatListeningLength(43 * 60 + 30)).toBe("43 min");
    expect(formatListeningLength(24.6)).toBe("24 sec");
  });

  it("keeps elapsed plus remaining equal to the total", () => {
    expect(formatRemainingClock(2610, 40.6)).toBe("42:50");
    expect(formatRemainingClock(2610.9, 40.6)).toBe("42:50");
    expect(formatRemainingClock(10, 12)).toBe("00:00");
    expect(formatListeningLength(Number.NaN)).toBe("0 sec");
  });
});
