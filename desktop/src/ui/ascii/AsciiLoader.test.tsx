// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AsciiLoader, scanFrame } from "./AsciiLoader";

let container: HTMLDivElement;
let root: Root;
let reducedMotion: boolean;
let hidden: boolean;
let mediaChange: () => void;
let intersectionChange: (entries: { isIntersecting: boolean }[]) => void;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  reducedMotion = false;
  hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  vi.stubGlobal("matchMedia", () => ({
    get matches() { return reducedMotion; },
    addEventListener: (_: string, listener: () => void) => { mediaChange = listener; },
    removeEventListener: vi.fn(),
  }));
  vi.stubGlobal("IntersectionObserver", class {
    constructor(callback: typeof intersectionChange) { intersectionChange = callback; }
    observe() {}
    disconnect() {}
  });
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockImplementation(function (this: HTMLElement) {
    return (this.closest("[hidden]") ? [] : [{}]) as unknown as DOMRectList;
  });
  container = document.createElement("div");
  container.style.visibility = "visible";
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("scanFrame", () => {
  it("sweeps a bright head across a fixed-width track and back", () => {
    expect(scanFrame(0, 5)).toBe("[·░▒▓█]");
    expect(scanFrame(4, 5)).toBe("[█▓▒░·]");
    expect(scanFrame(8, 5)).toBe("[·░▒▓█]");
    for (let tick = 0; tick < 20; tick += 1) expect(scanFrame(tick, 5)).toHaveLength(7);
  });
});

describe("AsciiLoader motion lifecycle", () => {
  it("only advances frames while active and cleans up its timer", () => {
    act(() => root.render(<AsciiLoader active />));
    const firstFrame = container.textContent;
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(70));
    expect(container.textContent).not.toBe(firstFrame);
    act(() => root.render(<AsciiLoader active={false} />));
    expect(vi.getTimerCount()).toBe(0);
    expect(container.textContent).toBe(firstFrame);
    act(() => root.render(<AsciiLoader active />));
    expect(vi.getTimerCount()).toBe(1);
    act(() => root.render(null));
    expect(vi.getTimerCount()).toBe(0);
  });

  it("renders the ring variant with its own frames", () => {
    act(() => root.render(<AsciiLoader active variant="ring" />));
    expect(container.textContent).toBe("◜");
    act(() => vi.advanceTimersByTime(120));
    expect(container.textContent).toBe("◠");
  });

  it("stops and restarts for document and panel visibility", async () => {
    act(() => root.render(<AsciiLoader active />));
    hidden = true;
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(vi.getTimerCount()).toBe(0);
    hidden = false;
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(vi.getTimerCount()).toBe(1);
    await act(async () => { container.hidden = true; });
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { container.hidden = false; });
    expect(vi.getTimerCount()).toBe(1);
    act(() => intersectionChange([{ isIntersecting: false }]));
    expect(vi.getTimerCount()).toBe(0);
    act(() => intersectionChange([{ isIntersecting: true }]));
    expect(vi.getTimerCount()).toBe(1);
  });

  it("keeps stepping, more slowly, under reduced motion", () => {
    reducedMotion = true;
    act(() => root.render(<AsciiLoader active />));
    const firstFrame = container.textContent;
    act(() => vi.advanceTimersByTime(70));
    expect(container.textContent).toBe(firstFrame);
    act(() => vi.advanceTimersByTime(350));
    expect(container.textContent).not.toBe(firstFrame);
    reducedMotion = false;
    act(() => mediaChange());
    expect(vi.getTimerCount()).toBe(1);
  });
});
