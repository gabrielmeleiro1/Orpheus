// @vitest-environment happy-dom

import { act, Profiler } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { ProgressBar } from "./ProgressBar";

afterEach(() => vi.unstubAllGlobals());

it("does not re-render a fitted bar when a resize leaves its cell count unchanged", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  let notify: () => void = () => undefined;
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { notify = callback; }
    observe() {}
    disconnect() {}
  });
  let width = 300;
  const clientWidth = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => width);
  const commits: string[] = [];
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(
      <Profiler id="bar" onRender={(_id, phase) => commits.push(phase)}>
        <ProgressBar pct={40} label="Stage" variant="ascii" cells="fit" />
      </Profiler>,
    ));
    const cells = () => container.querySelectorAll(".pbar-cells-filled > span, .pbar-cells-empty > span").length;
    const fitted = cells();
    commits.length = 0;

    // Neighbouring text changes nudge the bar's width on every progress event.
    for (const nudge of [301, 302, 300, 301]) {
      width = nudge;
      await act(async () => notify());
    }
    expect(commits).toEqual([]);
    expect(cells()).toBe(fitted);

    width = 600;
    await act(async () => notify());
    expect(cells()).toBeGreaterThan(fitted);
  } finally {
    await act(async () => root.unmount());
    clientWidth.mockRestore();
  }
});
