// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UseLibraryResult } from "../library/useLibrary";
import type { AudioAnalyserHandle } from "../player/useAudioAnalyser";
import { DashboardPanel } from "./DashboardPanel";

vi.mock("./AudioWaveformCanvas", () => ({ AudioWaveformCanvas: () => null }));

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
      items: [{ id: 1, title: "Visible match" }],
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

    const libraryCard = [...container.querySelectorAll(".status-card")].find(
      (card) => card.querySelector(".status-label")?.textContent === "Library",
    );
    expect(libraryCard?.querySelector(".status-number")?.textContent).toBe("4");
    expect(libraryCard?.querySelector(".status-detail")?.textContent).toBe("books indexed");
  });
});
