// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { ConversionActionsView } from "./ConversionActionsView";

afterEach(() => vi.unstubAllGlobals());

const row = (status: string, finished = false) => ({ statusClassName: `queue-item-status status-${status}`, finished });

it.each([
  [[row("failed"), row("failed")], "2 books could not start"],
  [[row("failed"), row("ready")], "1 book to convert"],
  [[row("done", true)], "Queue finished"],
])("summarizes the queue without counting rows that failed to start as waiting (%#)", async (queueItems, summary) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(
      <ConversionActionsView
        sourceQueue={{ queueItems, queueStatus: "", queueRunning: false, sourceInput: "file", pastedText: "" } as never}
        conversionActions={{ hasActiveJob: false, isStarting: false, startLabel: "Start", blockingReason: "", cancelLabel: "Cancel" } as never}
        capabilities={{ showStartConversion: true, canStartConversion: true } as never}
        actions={{} as never}
      />,
    ));
    expect(container.querySelector(".conversion-queue-summary")?.textContent).toBe(summary);
  } finally {
    await act(async () => root.unmount());
  }
});

async function renderActions(props: { queueRunning: boolean; conversionActions: object; capabilities: object; actions?: object }) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () => root.render(
    <ConversionActionsView
      sourceQueue={{ queueItems: [row("ready")], queueStatus: props.queueRunning ? "Converting Lantern…" : "", queueRunning: props.queueRunning, sourceInput: "file", pastedText: "" } as never}
      conversionActions={{ isStarting: false, startLabel: "Start", blockingReason: "", cancelLabel: "Cancel", ...props.conversionActions } as never}
      capabilities={props.capabilities as never}
      actions={(props.actions ?? {}) as never}
    />,
  ));
  return { container, unmount: () => act(async () => root.unmount()) };
}

it("does not claim to be converting while the running job cannot be observed", async () => {
  const view = await renderActions({
    queueRunning: true,
    conversionActions: { hasActiveJob: true, runningStatus: "Connection lost: reconnecting…" },
    capabilities: { canCancelJob: true },
  });
  try {
    expect(view.container.querySelector(".conversion-queue-summary")?.textContent).toBe("Connection lost: reconnecting…");
    expect(view.container.querySelector("#cancel-btn")).not.toBeNull();
  } finally {
    await view.unmount();
  }
});

it.each([
  [true, "run-queue-btn"],
  [false, "start-btn"],
])("gives the one primary button to the waiting books when they can start (%s)", async (canRunQueue, primaryId) => {
  const view = await renderActions({
    queueRunning: false,
    conversionActions: { hasActiveJob: false, startLabel: "Convert again" },
    capabilities: { showRunQueue: true, canRunQueue, showStartConversion: true, canStartConversion: true },
  });
  try {
    const primary = [...view.container.querySelectorAll(".conversion-action-row .btn-primary")].map((button) => button.id);
    expect(primary).toEqual([primaryId]);
    expect(view.container.querySelector("#start-btn")?.textContent).toBe("Convert again");
    expect(view.container.querySelector("#run-queue-btn")?.textContent).toBe("Start all");
  } finally {
    await view.unmount();
  }
});

it("asks before stopping a converting book and stops a pending start at once", async () => {
  const actions = {
    acceptsCancelClick: vi.fn((clickCount = 0) => clickCount <= 1),
    onConfirmCancelJob: vi.fn(async () => {}),
    onCancelJob: vi.fn(async () => {}),
  };
  const converting = await renderActions({
    queueRunning: false,
    conversionActions: { hasActiveJob: true, cancelJobId: "job-1" },
    capabilities: { canCancelJob: true },
    actions,
  });
  try {
    const click = (selector: string, detail = 1) => act(async () => {
      converting.container.querySelector(selector)!.dispatchEvent(new MouseEvent("click", { bubbles: true, detail }));
    });
    // The second click of a Start double-click never opens the question.
    await click("#cancel-btn", 2);
    expect(converting.container.querySelector("#cancel-confirm")).toBeNull();
    await click("#cancel-btn");
    expect(converting.container.querySelector("#cancel-btn")).toBeNull();
    expect(converting.container.querySelector("#cancel-confirm")?.textContent).toContain("Stop this conversion?");
    expect(actions.onConfirmCancelJob).not.toHaveBeenCalled();
    await click("#cancel-confirm-stop");
    expect(actions.onConfirmCancelJob).toHaveBeenCalledTimes(1);
    expect(actions.onCancelJob).not.toHaveBeenCalled();
  } finally {
    await converting.unmount();
  }

  const pending = await renderActions({
    queueRunning: true,
    conversionActions: { hasActiveJob: true, cancelJobId: null },
    capabilities: { canCancelJob: true },
    actions,
  });
  try {
    await act(async () => pending.container.querySelector<HTMLButtonElement>("#cancel-btn")!.click());
    expect(pending.container.querySelector("#cancel-confirm")).toBeNull();
    expect(actions.onCancelJob).toHaveBeenCalledTimes(1);
  } finally {
    await pending.unmount();
  }
});
