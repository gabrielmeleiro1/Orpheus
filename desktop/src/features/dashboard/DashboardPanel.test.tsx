// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UseLibraryResult } from "../library/useLibrary";
import type { AudioAnalyserHandle } from "../player/useAudioAnalyser";
import { DashboardPanel } from "./DashboardPanel";

vi.mock("./NowPlayingCanvas", () => ({ NowPlayingCanvas: () => null }));
vi.mock("./ConversionFlowCanvas", () => ({ ConversionFlowCanvas: () => null }));

describe("DashboardPanel library count", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("uses indexedCount when visible library items are filtered", async () => {
    const library = {
      items: [
        { id: 1, title: "Visible match", total_chapters: 4, completed_chapters: 4 },
        { id: 2, title: "Still converting", total_chapters: 4, completed_chapters: 2 },
      ],
      indexedCount: 4,
      selectedBookDetail: null,
    } as unknown as UseLibraryResult;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <DashboardPanel
          hidden={false}
          library={library}
          connectionState="connected"
          health={null}
          statusMessage="ready"
          uiState="idle"
          progressPct={0}
          etaText=""
          stages={[]}
          performance={null}
          currentChapterLabel=""
          playerTitle=""
          playing={false}
          audioVisualizer={{} as AudioAnalyserHandle}
          elapsedSec={0}
          durationSec={0}
        />,
      );
    });

    expect(container.querySelector(".recent-section .section-title")?.textContent).toContain("4 books");
    expect(container.querySelector('[aria-label="Visible match completion"]')).toBeNull();
    expect(container.querySelector('[aria-label="Still converting completion"]')?.getAttribute("aria-valuenow")).toBe("50");
  });

  it("shows a single Status tile and no ETA placeholder when ETA is empty", async () => {
    const library = {
      items: [],
      indexedCount: 0,
      selectedBookDetail: null,
    } as unknown as UseLibraryResult;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <DashboardPanel
          hidden={false}
          library={library}
          connectionState="connected"
          health={null}
          statusMessage="ready"
          uiState="running"
          progressPct={50}
          etaText=""
          stages={[]}
          performance={null}
          currentChapterLabel=""
          playerTitle=""
          playing={false}
          audioVisualizer={{} as AudioAnalyserHandle}
          elapsedSec={0}
          durationSec={0}
        />,
      );
    });

    const metricRow = container.querySelector(".metric-row-flow");
    expect(metricRow?.querySelectorAll(".metric").length).toBe(1);
    expect(container.textContent).not.toContain("ETA");
    expect(metricRow?.querySelector(".metric-label")?.textContent).toBe("Status");
  });

  it("keeps conversion progress exposed to assistive technology", async () => {
    const library = {
      items: [],
      indexedCount: 0,
      selectedBookDetail: null,
    } as unknown as UseLibraryResult;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <DashboardPanel
          hidden={false}
          library={library}
          connectionState="connected"
          health={null}
          statusMessage="running"
          uiState="running"
          progressPct={50}
          etaText=""
          stages={[]}
          performance={null}
          currentChapterLabel=""
          playerTitle=""
          playing={false}
          audioVisualizer={{} as AudioAnalyserHandle}
          elapsedSec={0}
          durationSec={0}
        />,
      );
    });

    const progress = container.querySelector('[aria-label="Conversion progress"]');
    expect(progress).not.toBeNull();
    expect(progress?.closest('[aria-hidden="true"]')).toBeNull();
  });

  it("renders exactly one primary action on the idle dashboard", async () => {
    const library = {
      items: [],
      indexedCount: 0,
      selectedBookDetail: null,
    } as unknown as UseLibraryResult;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <DashboardPanel
          hidden={false}
          library={library}
          connectionState="connected"
          health={null}
          statusMessage="ready"
          uiState="idle"
          progressPct={0}
          etaText=""
          stages={[]}
          performance={null}
          currentChapterLabel=""
          playerTitle=""
          playing={false}
          audioVisualizer={{} as AudioAnalyserHandle}
          elapsedSec={0}
          durationSec={0}
          onStartConvert={() => undefined}
        />,
      );
    });

    expect(container.querySelectorAll(".btn-primary").length).toBe(1);
  });
});
