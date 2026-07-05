import { describe, expect, it } from "vitest";

import { comboboxReducer, initialComboboxState, type ComboboxState } from "./useComboboxState";

function state(overrides: Partial<ComboboxState> = {}): ComboboxState {
  return { ...initialComboboxState, ...overrides };
}

describe("comboboxReducer", () => {
  it("toggles open without touching the highlight", () => {
    expect(comboboxReducer(state({ highlightIndex: 2 }), { type: "toggle" })).toEqual({
      open: true,
      highlightIndex: 2,
    });
    expect(comboboxReducer(state({ open: true, highlightIndex: 2 }), { type: "toggle" })).toEqual({
      open: false,
      highlightIndex: 2,
    });
  });

  it("open/close are idempotent on identical state", () => {
    const open = state({ open: true });
    expect(comboboxReducer(open, { type: "open" })).toBe(open);
    const closed = state({ open: false });
    expect(comboboxReducer(closed, { type: "close" })).toBe(closed);
  });

  it("sets an explicit highlight index", () => {
    expect(comboboxReducer(state({ open: true }), { type: "setHighlight", index: 3 })).toEqual({
      open: true,
      highlightIndex: 3,
    });
  });

  it("advances the highlight clamped to the option count", () => {
    expect(comboboxReducer(state({ open: true, highlightIndex: -1 }), { type: "highlightNext", optionCount: 3 }))
      .toEqual({ open: true, highlightIndex: 0 });
    expect(comboboxReducer(state({ open: true, highlightIndex: 1 }), { type: "highlightNext", optionCount: 3 }))
      .toEqual({ open: true, highlightIndex: 2 });
    expect(comboboxReducer(state({ open: true, highlightIndex: 2 }), { type: "highlightNext", optionCount: 3 }))
      .toEqual({ open: true, highlightIndex: 2 });
  });

  it("retreats the highlight without going below zero", () => {
    expect(comboboxReducer(state({ open: true, highlightIndex: 2 }), { type: "highlightPrev" }))
      .toEqual({ open: true, highlightIndex: 1 });
    expect(comboboxReducer(state({ open: true, highlightIndex: 0 }), { type: "highlightPrev" }))
      .toEqual({ open: true, highlightIndex: 0 });
    expect(comboboxReducer(state({ open: true, highlightIndex: -1 }), { type: "highlightPrev" }))
      .toEqual({ open: true, highlightIndex: 0 });
  });
});
