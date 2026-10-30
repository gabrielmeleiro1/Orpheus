// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { resolveTipPosition } from "./TooltipLayer";

const rect = (left: number, top: number, width: number, height: number): DOMRect =>
  ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) });

describe("resolveTipPosition", () => {
  const original = { width: window.innerWidth, height: window.innerHeight };
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1000 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 700 });
  });
  afterEach(() => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: original.width });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: original.height });
  });

  it("uses the preferred side when the bubble fits there", () => {
    expect(resolveTipPosition("top", rect(480, 300, 40, 40), rect(0, 0, 200, 60))).toEqual({ left: 400, top: 232 });
  });

  it("flips to the opposite side instead of leaving the window", () => {
    // An info button near the right edge that asks for a tip on its right.
    const position = resolveTipPosition("right", rect(900, 300, 32, 32), rect(0, 0, 280, 80));
    expect(position.left + 280).toBeLessThanOrEqual(900);
    expect(position.top).toBe(276);
  });

  it("slides along the edge rather than covering a control in a window corner", () => {
    // A Copy button in the bottom-right corner that asks for a tip below: no
    // side fits centred, but above fits once the bubble slides left.
    const anchor = rect(930, 660, 60, 30);
    const position = resolveTipPosition("bottom", anchor, rect(0, 0, 280, 54));
    expect(position.top + 54).toBeLessThanOrEqual(anchor.top);
    expect(position.left + 280).toBeLessThanOrEqual(1000 - 8);
  });

  it("keeps the whole bubble inside the window when no side fits", () => {
    const position = resolveTipPosition("top", rect(980, 2, 16, 16), rect(0, 0, 990, 690));
    expect(position.left).toBeGreaterThanOrEqual(0);
    expect(position.top).toBeGreaterThanOrEqual(0);
    expect(position.left + 990).toBeLessThanOrEqual(1000);
    expect(position.top + 690).toBeLessThanOrEqual(700);
  });
});
