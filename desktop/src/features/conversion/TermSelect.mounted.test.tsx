// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TermSelect } from "./TermSelect";

let container: HTMLDivElement;
let root: Root;
let triggerTop: number;
let clipBottom: number;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  triggerTop = 315;
  clipBottom = 518;
  vi.stubGlobal("innerHeight", 640);
  container = document.createElement("div");
  container.style.overflowY = "auto";
  document.body.appendChild(container);
  vi.spyOn(container, "getBoundingClientRect").mockImplementation(() => ({ top: 100, bottom: clipBottom, left: 0, right: 600, width: 600, height: clipBottom - 100, x: 0, y: 100, toJSON() {} }));
  vi.spyOn(container, "clientHeight", "get").mockImplementation(() => clipBottom - 100);
  root = createRoot(container);
  act(() => root.render(<TermSelect id="voice" label="Voice" value="a" options={[{value:"a",label:"First"},{value:"b",label:"Second"}]} placeholder="Select" onChange={() => {}} />));
  const trigger = container.querySelector<HTMLDivElement>(".term-select")!;
  vi.spyOn(trigger, "getBoundingClientRect").mockImplementation(() => ({ top: triggerTop, bottom: triggerTop + 38, left: 0, right: 200, width: 200, height: 38, x: 0, y: triggerTop, toJSON() {} }));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function openMenu() {
  act(() => container.querySelector<HTMLButtonElement>("button")!.click());
  return container.querySelector<HTMLDivElement>('[role="listbox"]')!;
}

describe("TermSelect menu clipping", () => {
  it("opens above and caps its height inside the form scrollport", () => {
    const menu = openMenu();
    expect(menu.style.bottom).toBe("calc(100% + 4px)");
    expect(menu.style.top).toBe("auto");
    expect(menu.style.maxHeight).toBe("211px");
  });

  it("intersects the scroll ancestor with the viewport", () => {
    clipBottom = 1000;
    triggerTop = 480;
    const menu = openMenu();
    expect(menu.style.bottom).toBe("calc(100% + 4px)");
    expect(menu.style.maxHeight).toBe("220px");
  });

  it("repositions on ancestor scrolling and caps available height on resize", () => {
    const menu = openMenu();
    triggerTop = 120;
    act(() => container.dispatchEvent(new Event("scroll")));
    expect(menu.style.top).toBe("calc(100% + 4px)");
    expect(menu.style.bottom).toBe("auto");
    expect(menu.style.maxHeight).toBe("220px");
    vi.stubGlobal("innerHeight", 300);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(menu.style.maxHeight).toBe("134px");
  });
});

describe("TermSelect pointer and keyboard interaction", () => {
  it("focuses the opened trigger so a pointer user can choose with arrow keys", () => {
    const onChange = vi.fn();
    act(() => root.render(<TermSelect id="voice" label="Voice" value="a" options={[{value:"a",label:"First"},{value:"b",label:"Second"}]} placeholder="Select" onChange={onChange} />));
    const trigger = container.querySelector<HTMLButtonElement>("button")!;
    act(() => trigger.click());
    expect(document.activeElement).toBe(trigger);
    act(() => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true })));
    act(() => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    expect(onChange).toHaveBeenCalledWith("b");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
  });
});
