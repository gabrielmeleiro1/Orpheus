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
  it("keeps option presses from focusing an ancestor scrollport before click", () => {
    container.tabIndex = 0;
    const onChange = vi.fn();
    act(() => root.render(<TermSelect id="voice" label="Voice" value="a" options={[{value:"a",label:"First"},{value:"b",label:"Second"}]} placeholder="Select" onChange={onChange} />));
    openMenu();
    const trigger = container.querySelector<HTMLButtonElement>("button")!;
    const option = container.querySelectorAll<HTMLElement>('[role="option"]')[1];
    const mouseDown = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    act(() => {
      // A nonfocusable option inside the app's tabindex=0 scrollport otherwise
      // transfers focus outside the combobox before the browser dispatches click.
      if (option.dispatchEvent(mouseDown)) container.focus();
    });
    expect(document.activeElement).toBe(trigger);
    expect(container.querySelector('[role="listbox"]')).not.toBeNull();
    act(() => option.click());
    expect(onChange).toHaveBeenCalledWith("b");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("reveals the highlighted option while navigating a long scrollable list", () => {
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollIntoView");
    const options = Array.from({ length: 30 }, (_, index) => ({ value: String(index), label: `Voice ${index}` }));
    act(() => root.render(<TermSelect id="voice" label="Voice" value="0" options={options} placeholder="Select" onChange={() => {}} />));
    const menu = openMenu();
    menu.style.maxHeight = "100px";
    menu.style.overflowY = "auto";
    const trigger = container.querySelector<HTMLButtonElement>("button")!;
    for (let index = 0; index < 12; index += 1) {
      act(() => trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
    }
    const highlighted = container.querySelector(".is-highlighted");
    expect(highlighted?.textContent).toContain("Voice 12");
    expect(scroll.mock.instances.at(-1)).toBe(highlighted);
    expect(scroll).toHaveBeenLastCalledWith({ block: "nearest", inline: "nearest" });
  });

  it("closes on Tab without preventing focus navigation", () => {
    openMenu();
    const trigger = container.querySelector<HTMLButtonElement>("button")!;
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    act(() => trigger.dispatchEvent(tab));
    expect(tab.defaultPrevented).toBe(false);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("closes when focus leaves the combobox", () => {
    openMenu();
    const next = document.createElement("button");
    container.append(next);
    act(() => next.focus());
    expect(container.querySelector<HTMLButtonElement>(".term-select-btn")!.getAttribute("aria-expanded")).toBe("false");
  });

  it("keeps selection distinct from the keyboard highlight without inline appearance", () => {
    openMenu();
    const trigger = container.querySelector<HTMLButtonElement>("button")!;
    const options = container.querySelectorAll<HTMLElement>('[role="option"]');
    expect(options[0].classList.contains("is-active")).toBe(true);
    expect(options[0].classList.contains("is-highlighted")).toBe(true);
    act(() => trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
    expect(options[0].getAttribute("aria-selected")).toBe("true");
    expect(options[0].classList.contains("is-active")).toBe(true);
    expect(options[0].classList.contains("is-highlighted")).toBe(false);
    expect(options[1].getAttribute("aria-selected")).toBe("false");
    expect(options[1].classList.contains("is-highlighted")).toBe(true);
    expect(trigger.getAttribute("aria-activedescendant")).toBe(options[1].id);
    expect(options[1].getAttribute("style")).toBeNull();
    act(() => trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

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

describe("TermSelect keyboard and references", () => {
  it("points aria-controls at the listbox only while it exists", () => {
    const trigger = container.querySelector<HTMLButtonElement>("button")!;
    expect(trigger.hasAttribute("aria-controls")).toBe(false);
    const menu = openMenu();
    expect(trigger.getAttribute("aria-controls")).toBe(menu.id);
    expect(document.getElementById(trigger.getAttribute("aria-controls")!)).toBe(menu);
  });

  it("moves to the first and last option with Home and End", () => {
    const trigger = container.querySelector<HTMLButtonElement>("button")!;
    openMenu();
    act(() => trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true })));
    expect(trigger.getAttribute("aria-activedescendant")).toBe("voice-listbox-option-1");
    act(() => trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true })));
    expect(trigger.getAttribute("aria-activedescendant")).toBe("voice-listbox-option-0");
  });
});

describe("TermSelect across a window resize", () => {
  it("scrolls an open combobox back into view when a reflow carries it out of its pane", () => {
    openMenu();
    const root = container.querySelector<HTMLDivElement>(".term-select")!;
    const scrollIntoView = vi.fn();
    root.scrollIntoView = scrollIntoView;
    triggerTop = 700; // below the pane after the layout went to one column
    act(() => window.dispatchEvent(new Event("resize")));
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });
  });
});
