// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import { useEventStream } from "../events/useEventStream";
import { ProgressPanel } from "./ProgressPanel";

it("leads with one stage-and-percent headline and lists measured performance in plain words", () => {
  const container = document.createElement("div");
  const props = {
    uiState: "running" as const,
    statusText: "Cleaning chunk 1/1",
    progressPct: 37,
    etaText: "ETA ~12s",
    stages: [{ name: "cleaning" as const, state: "active" as const, pct: 50, detail: [
      { label: "Chapters", value: "5/12", pct: 42 },
      { label: "Chunks", value: "1/1", pct: 100 },
    ] }],
    currentStage: "cleaning",
    currentMessage: "Cleaning chunk 1/1",
    performance: { active_workers: 2, max_workers: 4, completed_chapters: 5, tts_chars_per_second: 40, elapsed_seconds: 12, pipeline_mode: "parallel" },
    activeJobId: "job",
    isCancelling: false,
    onCancel: async () => {},
  };
  container.innerHTML = renderToString(<ProgressPanel {...props} />);
  const hero = container.querySelector(".progress-hero")!;
  const headline = hero.querySelector(".progress-headline")!;
  expect(headline.querySelector(".progress-title")?.textContent).toBe("Cleaning");
  expect(headline.querySelector("#progress-pct")?.textContent).toBe("37% overall");
  expect(hero.querySelector("#eta-text")?.textContent).toBe("~12s left");
  expect(hero.querySelector("#status-text")?.getAttribute("aria-live")).toBe("polite");
  // The active stage's own detail sits under it, on the same grid as the stages.
  const stageList = container.querySelector("#stage-list")!;
  expect(stageList.querySelector('.stage-active + [aria-label="Active stage detail"] [aria-label="Chapters progress"]')?.getAttribute("aria-valuenow")).toBe("42");
  expect(stageList.querySelector('[aria-label="Chunks progress"]')?.getAttribute("role")).toBe("progressbar");
  const performance = container.querySelector<HTMLDetailsElement>("details.performance-section")!;
  expect(performance.open).toBe(false);
  expect(performance.querySelector("summary")?.textContent).toBe("Performance12s");
  const metrics = [...container.querySelectorAll(".performance-item")];
  expect(metrics.map(row => row.querySelector("dt")?.textContent)).toEqual(["Parallel chapters", "Chapters done", "Narration", "Elapsed", "Mode"]);
  expect(metrics[0].querySelector("dd")?.textContent).toBe("2 of 4");
  expect(metrics[2].querySelector("dd")?.textContent).toBe("40.0 characters/s");

  container.innerHTML = renderToString(<ProgressPanel {...props} uiState="completed" activeJobId={null} progressPct={100} />);
  expect(container.querySelector('[aria-label="Active stage detail"]')).toBeNull();
  expect(container.querySelector("#progress-cancel-btn")).toBeNull();
  expect(container.querySelector(".performance-value")?.textContent).toBe("4");
  expect(container.querySelector("#progress-pct")).toBeNull();
  expect(container.querySelector(".progress-title")?.textContent).toBe("Complete");
});

it("keeps metrics empty states beside stages and shows the idle state", async () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  const props = {
    statusText: "", progressPct: 0, etaText: "", stages: [], currentStage: "", currentMessage: "",
    performance: null, activeJobId: null, isCancelling: false, onCancel: async () => {},
  };
  try {
    for (const uiState of ["running", "completed"] as const) {
      await act(async () => root.render(<ProgressPanel {...props} uiState={uiState} />));
      const summary = container.querySelector(".progress-summary")!;
      expect(summary.querySelector("#stage-list")).not.toBeNull();
      expect(summary.querySelector('[aria-label="Performance snapshot"]')?.textContent).toContain("No measurements yet");
      expect(summary.querySelector(".performance-grid")).toBeNull();
    }
    await act(async () => root.render(<ProgressPanel {...props} uiState="idle" />));
    expect(container.querySelector(".progress-empty")?.textContent).toContain("No conversion running");
    expect(container.querySelector("[role=progressbar]")).toBeNull();
  } finally {
    await act(async () => root.unmount());
  }
});

it("animates an active stage only while the conversion is running", async () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  const props = {
    statusText: "Synthesizing",
    progressPct: 50,
    etaText: "",
    stages: [{ name: "packaging" as const, state: "active" as const, pct: 50, detail: [] }],
    currentStage: "packaging",
    currentMessage: "",
    performance: null,
    activeJobId: null,
    isCancelling: false,
    onCancel: async () => {},
  };
  try {
    await act(async () => root.render(<ProgressPanel {...props} uiState="running" />));
    expect(container.querySelector(".stage-name")?.textContent).toBe("Packaging");
    expect(container.querySelector(".progress-title")?.textContent).toBe("Packaging");
    expect(container.querySelector(".stage-spinner.is-busy")).not.toBeNull();
    expect(container.querySelector('[aria-label="packaging progress"]')).not.toBeNull();
    await act(async () => root.render(<ProgressPanel {...props} uiState="failed" />));
    expect(container.querySelector(".stage-spinner.is-busy")).toBeNull();
  } finally {
    await act(async () => root.unmount());
    vi.unstubAllGlobals();
  }
});

vi.mock("../events/useEventStreamTransport", () => ({ useEventStreamTransport: () => "open" }));

it("does not claim the conversion is idle when its state cannot be verified", () => {
  const container = document.createElement("div");
  container.innerHTML = renderToString(<ProgressPanel uiState="unknown" statusText="Reconnecting to conversion updates"
    progressPct={40} etaText="" stages={[]} currentStage="" currentMessage="" performance={null}
    activeJobId="job" isCancelling={false} onCancel={async () => {}} />);
  expect(container.textContent).toContain("Conversion status unavailable");
  expect(container.querySelector('[role="status"]')?.textContent).toBe("Reconnecting to conversion updates");
  expect(container.textContent).not.toContain("No conversion running");
});

it("announces cancellation failure beside Cancel and keeps the conversion running for retry", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const cancelConversion = vi.fn().mockRejectedValueOnce(new Error("request timed out"))
    .mockResolvedValue({ cancelled: true });
  const apiClient = {
    getActiveConversion: vi.fn(async () => ({ active: true, job: { id: "job", status: "running" } })),
    cancelConversion,
  } as unknown as ApiClient;
  function Probe() {
    const job = useEventStream({ apiClient, baseUrl: "http://desktop.test" });
    return <ProgressPanel {...job} performance={job.performanceSnapshot} onCancel={job.cancelActiveConversion} />;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<Probe />));
    const stopWithConfirmation = async () => {
      await act(async () => container.querySelector<HTMLButtonElement>("#progress-cancel-btn")!.click());
      await act(async () => container.querySelector<HTMLButtonElement>("#progress-cancel-confirm-stop")!.click());
    };
    await stopWithConfirmation();
    expect(container.querySelector('[role="alert"]')?.textContent).toBe("Couldn't stop the conversion. Try again.");
    expect(container.querySelector<HTMLButtonElement>("#progress-cancel-btn")!.disabled).toBe(false);
    await stopWithConfirmation();
    const cancel = container.querySelector<HTMLButtonElement>("#progress-cancel-btn")!;
    expect(cancelConversion).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(cancel.textContent).toContain("Cancelling");
  } finally {
    await act(async () => root.unmount());
    vi.unstubAllGlobals();
  }
});

it("heads a running conversion with the chapter and stage, and drops the chapter once it ends", () => {
  const container = document.createElement("div");
  const props = {
    statusText: "Synthesizing",
    progressPct: 30,
    etaText: "",
    stages: [{ name: "synthesis" as const, state: "active" as const, pct: 30, detail: [
      { label: "Chapter", value: "3 of 12", pct: 25 },
    ] }],
    currentStage: "synthesizing",
    currentMessage: "",
    performance: null,
    activeJobId: "job",
    isCancelling: false,
    onCancel: async () => {},
  };
  container.innerHTML = renderToString(<ProgressPanel {...props} uiState="running" />);
  expect(container.querySelector(".progress-title")?.textContent).toBe("Chapter 3/12 · Synthesis");
  expect(container.querySelector(".stage-name")?.textContent).toBe("Synthesis");

  container.innerHTML = renderToString(<ProgressPanel {...props} uiState="cancelled" activeJobId={null} />);
  expect(container.querySelector(".progress-title")?.textContent).toBe("Cancelled");
});

it("asks before stopping a running book, and Keep converting or Escape leave it running", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const onCancel = vi.fn(async () => {});
  const props = {
    uiState: "running" as const, statusText: "", progressPct: 20, etaText: "", stages: [], currentStage: "",
    currentMessage: "", performance: null, activeJobId: "job-1", isCancelling: false, onCancel,
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const trigger = () => container.querySelector<HTMLButtonElement>("#progress-cancel-btn");
  try {
    await act(async () => root.render(<ProgressPanel {...props} />));
    await act(async () => trigger()!.click());
    const confirm = container.querySelector("#progress-cancel-confirm")!;
    expect(confirm.getAttribute("role")).toBe("group");
    expect(confirm.textContent).toContain("Finished chapters are kept");
    expect(trigger()).toBeNull();
    expect(onCancel).not.toHaveBeenCalled();

    await act(async () => container.querySelector<HTMLButtonElement>("#progress-cancel-confirm-keep")!.click());
    expect(container.querySelector("#progress-cancel-confirm")).toBeNull();
    expect(document.activeElement).toBe(trigger());

    await act(async () => trigger()!.click());
    await act(async () => {
      container.querySelector("#progress-cancel-confirm")!
        .dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(container.querySelector("#progress-cancel-confirm")).toBeNull();

    // A question about one book never confirms stopping the next one.
    await act(async () => trigger()!.click());
    await act(async () => root.render(<ProgressPanel {...props} activeJobId="job-2" />));
    expect(container.querySelector("#progress-cancel-confirm")).toBeNull();

    await act(async () => trigger()!.click());
    await act(async () => container.querySelector<HTMLButtonElement>("#progress-cancel-confirm-stop")!.click());
    expect(onCancel).toHaveBeenCalledTimes(1);
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
