// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UseLibraryResult } from "../library/useLibrary";
import type { AudioAnalyserHandle } from "../player/useAudioAnalyser";
import { DashboardPanel } from "./DashboardPanel";

/**
 * Qualification measured ~4% main-process CPU on a settled empty dashboard.
 * Canvas RAF must not be scheduled in that state: no now-playing surface and
 * no conversion job means neither canvas mounts, so requestAnimationFrame
 * stays untouched.
 */

describe("DashboardPanel idle canvas scheduling", () => {
  let root: Root;
  let container: HTMLDivElement;
  let rafSpy: ReturnType<typeof vi.spyOn>;
  let originalMatchMedia: typeof window.matchMedia;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    originalMatchMedia = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => true,
    })) as unknown as typeof window.matchMedia;
    rafSpy = vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    window.matchMedia = originalMatchMedia;
    vi.restoreAllMocks();
  });

  it("schedules no animation frames on an empty settled dashboard", async () => {
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
          audioVisualizer={{ analyserRef: { current: null }, status: "idle" } as AudioAnalyserHandle}
          elapsedSec={0}
          durationSec={0}
        />,
      );
    });

    expect(container.querySelector(".now-playing-canvas")).toBeNull();
    expect(container.querySelector(".conversion-flow-canvas")).toBeNull();
    expect(rafSpy).not.toHaveBeenCalled();
  });
});
