import { describe, expect, it } from "vitest";

import { computeVirtualWindow, scrollOffsetForIndex } from "./virtualWindow";

describe("computeVirtualWindow", () => {
  it("keeps the list bounded when a hidden viewport cannot be measured", () => {
    const zeroViewport = computeVirtualWindow(1000, 50, 0, 0, 5);
    expect(zeroViewport.endIndex - zeroViewport.startIndex).toBeLessThan(30);
    expect(zeroViewport.paddingBottom).toBeGreaterThan(0);

    const zeroStride = computeVirtualWindow(1000, 0, 0, 400, 5);
    expect(zeroStride.endIndex - zeroStride.startIndex).toBeLessThan(30);
  });

  it("returns an empty window for an empty list", () => {
    expect(computeVirtualWindow(0, 50, 0, 400, 5)).toEqual({
      startIndex: 0,
      endIndex: 0,
      paddingTop: 0,
      paddingBottom: 0,
    });
  });

  it("bounds the rendered slice and keeps total height via spacers", () => {
    // 1000 rows of 50px, a 400px viewport scrolled to the middle.
    const window = computeVirtualWindow(1000, 50, 5000, 400, 4);
    // Rendered count stays a small constant regardless of list size.
    expect(window.endIndex - window.startIndex).toBeLessThan(30);
    expect(window.startIndex).toBeGreaterThan(0);
    expect(window.endIndex).toBeLessThan(1000);
    // Spacers reproduce the full scroll height so the scrollbar is faithful.
    const renderedHeight = (window.endIndex - window.startIndex) * 50;
    expect(window.paddingTop + renderedHeight + window.paddingBottom).toBe(1000 * 50);
  });

  it("subtracts the grid gap contributed around spacer elements", () => {
    const window = computeVirtualWindow(1000, 57, 5700, 400, 4, 7);
    const renderedCount = window.endIndex - window.startIndex;
    const spacerCount = Number(window.paddingTop > 0) + Number(window.paddingBottom > 0);
    const gridChildCount = renderedCount + spacerCount;
    const renderedRowsHeight = renderedCount * 50;
    const actualGridHeight = window.paddingTop
      + renderedRowsHeight
      + window.paddingBottom
      + Math.max(0, gridChildCount - 1) * 7;

    expect(actualGridHeight).toBe(1000 * 50 + 999 * 7);
  });

  it("includes overscan rows above and below the visible band", () => {
    const overscan = 3;
    const window = computeVirtualWindow(1000, 50, 5000, 400, overscan);
    const firstVisible = Math.floor(5000 / 50); // 100
    expect(window.startIndex).toBe(firstVisible - overscan);
  });

  it("clamps the window at the list bounds when scrolled to the end", () => {
    const window = computeVirtualWindow(100, 50, 100000, 400, 4);
    expect(window.endIndex).toBe(100);
    expect(window.paddingBottom).toBe(0);
  });
});

describe("scrollOffsetForIndex", () => {
  it("scrolls up to reveal a row above the viewport", () => {
    expect(scrollOffsetForIndex(2, 50, 500, 400)).toBe(100);
  });

  it("scrolls down to reveal a row below the viewport", () => {
    // Row 20 spans 1000-1050; viewport 500-900 -> need scrollTop 650.
    expect(scrollOffsetForIndex(20, 50, 500, 400)).toBe(650);
  });

  it("returns null when the row is already visible", () => {
    expect(scrollOffsetForIndex(11, 50, 500, 400)).toBeNull();
  });

  it("returns null when the viewport cannot be measured", () => {
    expect(scrollOffsetForIndex(11, 50, 500, 0)).toBeNull();
    expect(scrollOffsetForIndex(-1, 50, 500, 400)).toBeNull();
  });
});
