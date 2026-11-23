import { describe, expect, it } from "vitest";

import { equalWhenClosed, equalWhenHidden, equalWhenInvisible } from "./visibilityMemo";

describe("visibility-aware memoization", () => {
  it("suppresses changing props while a panel remains hidden", () => {
    expect(equalWhenHidden(
      { hidden: true, progress: 10 },
      { hidden: true, progress: 90 },
    )).toBe(true);
  });

  it("renders visibility transitions and changes in visible panels", () => {
    const hidden = { hidden: true, progress: 10 } as { hidden: boolean; progress: number };
    expect(equalWhenHidden(
      hidden,
      { hidden: false, progress: 10 },
    )).toBe(false);
    const callback = () => undefined;
    expect(equalWhenHidden(
      { hidden: false, progress: 10, callback },
      { hidden: false, progress: 10, callback },
    )).toBe(true);
    expect(equalWhenHidden(
      { hidden: false, progress: 10 },
      { hidden: false, progress: 90 },
    )).toBe(false);
  });

  it("applies the same rule to an invisible player", () => {
    expect(equalWhenInvisible(
      { visible: false, elapsedSec: 10 },
      { visible: false, elapsedSec: 11 },
    )).toBe(true);
    const invisible = { visible: false, elapsedSec: 10 } as { visible: boolean; elapsedSec: number };
    expect(equalWhenInvisible(
      invisible,
      { visible: true, elapsedSec: 10 },
    )).toBe(false);
  });

  it("skips a closed overlay's changes but renders it when it opens", () => {
    expect(equalWhenClosed({ open: false, elapsedSec: 1 }, { open: false, elapsedSec: 2 })).toBe(true);
    const closed = { open: false, elapsedSec: 2 } as { open: boolean; elapsedSec: number };
    expect(equalWhenClosed(closed, { open: true, elapsedSec: 2 })).toBe(false);
    expect(equalWhenClosed({ open: true, elapsedSec: 2 }, { open: true, elapsedSec: 3 })).toBe(false);
  });
});
