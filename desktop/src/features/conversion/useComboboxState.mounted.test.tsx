// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useComboboxState } from "./useComboboxState";

let container: HTMLDivElement;
let root: Root;
let current: ReturnType<typeof useComboboxState> | null = null;

function Probe({ optionCount, selectedIndex, disabled }: {
  optionCount: number;
  selectedIndex: number;
  disabled: boolean;
}) {
  current = useComboboxState({
    optionCount,
    selectedIndex,
    disabled,
    onChoose: () => {},
  });
  return null;
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  current = null;
});

describe("useComboboxState lifecycle", () => {
  it("closes when disabled or when the option list becomes empty", () => {
    act(() => root.render(<Probe optionCount={2} selectedIndex={0} disabled={false} />));
    act(() => current?.toggle());
    expect(current?.open).toBe(true);

    act(() => root.render(<Probe optionCount={2} selectedIndex={0} disabled />));
    expect(current?.open).toBe(false);

    act(() => root.render(<Probe optionCount={2} selectedIndex={0} disabled={false} />));
    act(() => current?.toggle());
    act(() => root.render(<Probe optionCount={0} selectedIndex={0} disabled={false} />));
    expect(current?.open).toBe(false);
  });

  it("keeps the active descendant within the current options", () => {
    act(() => root.render(<Probe optionCount={2} selectedIndex={8} disabled={false} />));
    act(() => current?.toggle());
    expect(current?.highlightIndex).toBe(1);
  });
});
