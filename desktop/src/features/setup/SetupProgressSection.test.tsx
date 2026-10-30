// @vitest-environment happy-dom

import { act, createRef } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";

import type { RuntimeModelDownloadStatus } from "../../types/api";
import { applyProgressEvent, modelDownloadProgressEvent } from "./setupModel";
import { SetupProgressSection } from "./SetupProgressSection";

it.each([
  { known: true, lifecycle: "running" },
  { known: false, lifecycle: "running" },
  { known: true, lifecycle: "stopping" },
  { known: false, lifecycle: "stopping" },
] as const)("renders model download progress with known bytes=$known while $lifecycle", async ({ known, lifecycle }) => {
  const snapshot: RuntimeModelDownloadStatus = {
    lifecycle,
    profile: "mlx",
    model_groups: ["cleaner-mlx"],
    total_models: 1,
    completed_models: 0,
    current_model_group: "cleaner-mlx",
    bytes_total: known ? 1_800_000_000 : null,
    bytes_done: known ? 1_200_000_000 : null,
    error: null,
  };
  const progress = applyProgressEvent(undefined, modelDownloadProgressEvent(snapshot));
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<SetupProgressSection
      progress={progress}
      progressMessage={progress.message}
      progressValue={progress.percent ?? undefined}
      headingRef={createRef<HTMLDivElement>()}
    />));
    const bar = container.querySelector('[role="progressbar"]');
    expect(bar?.getAttribute("aria-valuenow")).toBe(known ? "67" : null);
    expect(container.textContent?.includes("1.2 GB of 1.8 GB")).toBe(known);
  } finally {
    await act(async () => root.unmount());
  }
});
