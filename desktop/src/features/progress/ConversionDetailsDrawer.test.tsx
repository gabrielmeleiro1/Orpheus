// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import type { ConversionJobController } from "../conversion/useConversionJob";
import { ConversionDetailsDrawer } from "./ConversionDetailsDrawer";

const job = {
  uiState: "running",
  statusText: "Synthesizing",
  progressPct: 40,
  etaText: "ETA 3m",
  stages: [{ name: "synthesis", state: "active", pct: 40, detail: [] }],
  currentStage: "synthesis",
  currentMessage: "",
  performanceSnapshot: null,
  activeJobId: "job-1",
  isCancelling: false,
  cancellationError: "",
  cancelActiveConversion: vi.fn(async () => undefined),
  logs: [],
} as unknown as ConversionJobController;

describe("ConversionDetailsDrawer", () => {
  it("keeps keyboard focus inside the drawer and makes covered controls inert until close", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const render = (open: boolean, notice = false) => root.render(<>
      <button data-testid="behind">Covered action</button>
      {notice ? <button data-testid="notice">New completion notice</button> : null}
      <ConversionDetailsDrawer open={open} conversionJob={job} onClose={vi.fn()} />
    </>);
    try {
      await act(async () => render(false));
      const background = container.querySelector<HTMLButtonElement>('[data-testid="behind"]')!;
      background.focus();
      await act(async () => render(true));
      const drawer = container.querySelector<HTMLElement>('[role="dialog"]')!;
      expect(drawer.getAttribute("aria-modal")).toBe("true");
      expect(background.hasAttribute("inert")).toBe(true);
      const first = drawer.querySelector<HTMLButtonElement>("button")!;
      // The noisy log starts folded; its summary is then the last stop.
      const logDisclosure = drawer.querySelector<HTMLDetailsElement>(".activity-log-disclosure")!;
      expect(logDisclosure.open).toBe(false);
      const summary = logDisclosure.querySelector<HTMLElement>("summary")!;
      summary.focus();
      const pastSummary = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
      summary.dispatchEvent(pastSummary);
      expect(pastSummary.defaultPrevented).toBe(true);
      expect(document.activeElement).toBe(first);
      await act(async () => { logDisclosure.open = true; logDisclosure.dispatchEvent(new Event("toggle")); });
      const last = drawer.querySelector<HTMLElement>('[role="log"]')!;
      expect(last.tabIndex).toBe(0);
      expect(last.getAttribute("aria-label")).toBe("Event log entries");
      first.focus();
      const backwards = new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true });
      first.dispatchEvent(backwards);
      expect(backwards.defaultPrevented).toBe(true);
      expect(document.activeElement).toBe(last);
      const forwards = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
      last.dispatchEvent(forwards);
      expect(forwards.defaultPrevented).toBe(true);
      expect(document.activeElement).toBe(first);
      await act(async () => render(true, true));
      expect(container.querySelector('[data-testid="notice"]')?.hasAttribute("inert")).toBe(true);
      await act(async () => render(false, true));
      expect(background.hasAttribute("inert")).toBe(false);
      expect(container.querySelector('[data-testid="notice"]')?.hasAttribute("inert")).toBe(false);
      expect(document.activeElement).toBe(background);
    } finally {
      await act(async () => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });

  it("shows stages and the log over the page, closes on Escape and restores focus", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    document.body.append(container);
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();
    const root = createRoot(container);
    const onClose = vi.fn();
    try {
      await act(async () => root.render(<ConversionDetailsDrawer open conversionJob={job} onClose={onClose} />));
      const drawer = container.querySelector<HTMLElement>('[role="dialog"][aria-label="Conversion details"]')!;
      expect(drawer.querySelector("#stage-list")?.textContent).toContain("Synthesis");
      // The region is named by the heading people can see, not by a hidden label.
      const logRegion = drawer.querySelector<HTMLElement>("section.activity-log")!;
      expect(logRegion.hasAttribute("aria-label")).toBe(false);
      const logHeading = document.getElementById(logRegion.getAttribute("aria-labelledby") ?? "");
      expect(logRegion.contains(logHeading)).toBe(true);
      expect(logHeading?.textContent).toBe("Event log");
      expect(drawer.querySelector<HTMLDetailsElement>(".activity-log-disclosure")?.open).toBe(false);
      expect(document.activeElement?.getAttribute("aria-label")).toBe("Close conversion details");
      await act(async () => drawer.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
      expect(onClose).toHaveBeenCalledOnce();
      await act(async () => root.render(<ConversionDetailsDrawer open={false} conversionJob={job} onClose={onClose} />));
      expect(container.querySelector('[role="dialog"]')).toBeNull();
      expect(document.activeElement).toBe(trigger);
    } finally {
      await act(async () => root.unmount());
      container.remove();
      trigger.remove();
      vi.unstubAllGlobals();
    }
  });

  it("explains a reduced parallel request beside the progress, without opening the event log", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const limited = { ...job, logs: [
      { id: 2, level: "info", ts: "", msg: "Chapter 1 cleaned" },
      { id: 1, level: "warning", ts: "", msg: "[PARALLEL] Using 1 worker of 2 requested: memory headroom limits parallel workers" },
    ] } as unknown as ConversionJobController;
    try {
      await act(async () => root.render(<ConversionDetailsDrawer open conversionJob={limited} onClose={vi.fn()} />));
      const drawer = container.querySelector<HTMLElement>('[role="dialog"]')!;
      expect(drawer.querySelector<HTMLDetailsElement>(".activity-log-disclosure")?.open).toBe(false);
      const note = drawer.querySelector("#parallel-limit-note");
      expect(note?.textContent).toBe("Using 1 of 2 requested parallel chapters: memory headroom limits parallel workers.");
      expect(note?.closest("details")).toBeNull();

      await act(async () => root.render(<ConversionDetailsDrawer open conversionJob={job} onClose={vi.fn()} />));
      expect(drawer.querySelector("#parallel-limit-note")).toBeNull();
    } finally {
      await act(async () => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });
});
