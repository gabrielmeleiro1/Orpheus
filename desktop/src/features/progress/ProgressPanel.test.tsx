// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { ProgressPanel } from "./ProgressPanel";

it("uses completed styling for the finished bar and stage check", async () => {
  const container = document.createElement("div");
  const style = document.createElement("style");
  style.textContent = readFileSync("src/features/progress/progress.css", "utf8");
  document.head.append(style);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<ProgressPanel uiState="completed" statusText="Complete" progressPct={100} etaText="" stages={[{ name: "packaging", state: "done", pct: 100, detail: [] }]} currentStage="packaging" currentMessage="" performance={null} activeJobId={null} isCancelling={false} onCancel={async () => {}} />));
    expect(container.querySelector('[aria-label="Overall conversion progress"]')?.classList.contains("pbar-done")).toBe(true);
    expect(container.querySelector(".stage-node.is-done")).not.toBeNull();
    const rule = ([...style.sheet!.cssRules] as CSSStyleRule[]).find(candidate => candidate.selectorText === ".stage-node.is-done");
    expect(rule?.style.color).toBe("var(--positive)");
  } finally {
    await act(async () => root.unmount());
    style.remove();
  }
});
