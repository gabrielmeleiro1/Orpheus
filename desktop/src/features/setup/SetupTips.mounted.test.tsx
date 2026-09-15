// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InfoTip } from "./SetupTips";

describe("setup information panels", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("escapes clipping containers, keeps model selection unchanged, and stays open while reading", async () => {
    const change = vi.fn();
    await act(async () => root.render(
      <label style={{ overflow: "hidden" }}>
        <input type="checkbox" defaultChecked onChange={change} />
        <InfoTip label="Model details">Complete model information</InfoTip>
      </label>,
    ));
    const trigger = container.querySelector("button")!;
    await act(async () => { trigger.focus(); trigger.click(); });
    const panel = document.querySelector('[role="tooltip"]')!;
    expect(panel.parentElement).toBe(document.body);
    expect(panel.textContent).toBe("Complete model information");
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector("input")!.checked).toBe(true);
    expect(change).not.toHaveBeenCalled();
    await act(async () => trigger.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: document.body })));
    expect(document.querySelector('[role="tooltip"]')).toBe(panel);
    await act(async () => panel.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })));
    expect(document.querySelector('[role="tooltip"]')).toBe(panel);
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    await act(async () => trigger.click());
    await act(async () => document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })));
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
  });

  it("opens on hover without a click and stays readable while the pointer enters the panel", async () => {
    vi.useFakeTimers();
    await act(async () => root.render(<InfoTip label="Details">Model information</InfoTip>));
    const trigger = container.querySelector("button")!;
    await act(async () => trigger.dispatchEvent(new MouseEvent("mouseover", { bubbles: true })));
    const panel = document.querySelector('[role="tooltip"]')!;
    expect(panel.textContent).toBe("Model information");
    await act(async () => {
      trigger.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: document.body }));
      vi.advanceTimersByTime(100);
      panel.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, relatedTarget: document.body }));
      vi.advanceTimersByTime(250);
    });
    expect(document.querySelector('[role="tooltip"]')).toBe(panel);
    await act(async () => {
      panel.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: document.body }));
      vi.advanceTimersByTime(250);
    });
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
  });

  it("keeps information inside the viewport and follows scrolling and resizing", async () => {
    vi.stubGlobal("innerWidth", 600);
    vi.stubGlobal("innerHeight", 400);
    let top = 350;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const isPanel = this.classList.contains("setup-tip-bubble");
      const left = isPanel ? 0 : 550;
      const y = isPanel ? 0 : top;
      const width = isPanel ? 320 : 20;
      const height = isPanel ? 160 : 20;
      return { x: left, y, top: y, left, right: left + width, bottom: y + height, width, height, toJSON: () => ({}) };
    });
    await act(async () => root.render(<InfoTip label="Details">Information</InfoTip>));
    await act(async () => container.querySelector("button")!.click());
    const panel = document.querySelector<HTMLElement>('[role="tooltip"]')!;
    expect(panel.style.left).toBe("264px");
    expect(panel.style.top).toBe("182px");
    top = 50;
    await act(async () => window.dispatchEvent(new Event("scroll")));
    expect(panel.style.top).toBe("78px");
    vi.stubGlobal("innerWidth", 400);
    await act(async () => window.dispatchEvent(new Event("resize")));
    expect(panel.style.left).toBe("64px");
  });
});
