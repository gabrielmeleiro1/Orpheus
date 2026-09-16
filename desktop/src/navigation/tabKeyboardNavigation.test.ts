import { describe, expect, it } from "vitest";

import { DASHBOARD_TAB_CONFIGS } from "./tabs";
import {
  nextTabIndexAfterKeyboard,
  tabButtonTabIndex,
} from "./tabKeyboardNavigation";

describe("tabKeyboardNavigation", () => {
  const tabCount = DASHBOARD_TAB_CONFIGS.length;

  it("keeps exactly one tab focusable via tabIndex", () => {
    expect(tabButtonTabIndex(2, 2)).toBe(0);
    expect(tabButtonTabIndex(2, 3)).toBe(-1);
  });

  it("wraps ArrowRight and ArrowLeft", () => {
    expect(nextTabIndexAfterKeyboard({ key: "ArrowRight", activeIndex: 0, tabCount })).toBe(1);
    expect(nextTabIndexAfterKeyboard({ key: "ArrowRight", activeIndex: tabCount - 1, tabCount })).toBe(0);
    expect(nextTabIndexAfterKeyboard({ key: "ArrowLeft", activeIndex: 0, tabCount })).toBe(tabCount - 1);
  });

  it("supports vertical sidebar navigation and wraps at both ends", () => {
    expect(nextTabIndexAfterKeyboard({ key: "ArrowDown", activeIndex: 0, tabCount })).toBe(1);
    expect(nextTabIndexAfterKeyboard({ key: "ArrowDown", activeIndex: tabCount - 1, tabCount })).toBe(0);
    expect(nextTabIndexAfterKeyboard({ key: "ArrowUp", activeIndex: 0, tabCount })).toBe(tabCount - 1);
  });

  it("maps Home and End to first and last", () => {
    expect(nextTabIndexAfterKeyboard({ key: "Home", activeIndex: 3, tabCount })).toBe(0);
    expect(nextTabIndexAfterKeyboard({ key: "End", activeIndex: 1, tabCount })).toBe(tabCount - 1);
  });

  it("ignores unrelated keys", () => {
    expect(nextTabIndexAfterKeyboard({ key: "Enter", activeIndex: 2, tabCount })).toBeNull();
  });
});
