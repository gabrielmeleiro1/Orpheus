// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AudioAnalyserHandle } from "../player/useAudioAnalyser";
import { NowPlayingCanvas } from "./NowPlayingCanvas";

/**
 * The now-playing lyre must run its RAF loop only while audio is actually
 * playing and the panel/document are visible with reduced motion off. When
 * paused, hidden, or reduced-motion, it draws one static frame and schedules
 * no animation. happy-dom canvases have no 2D context, so the RAF spy is the
 * observable — and the CPU signal we care about.
 */

let rafSpy: ReturnType<typeof vi.spyOn>;
let cancelSpy: ReturnType<typeof vi.spyOn>;
let originalMatchMedia: typeof window.matchMedia;
let visibilityOverridden = false;

function stubReducedMotion(matches: boolean): void {
  window.matchMedia = ((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => true,
  })) as unknown as typeof window.matchMedia;
}

function setDocumentHidden(hidden: boolean): void {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
  visibilityOverridden = true;
}

function idleVisualizer(): AudioAnalyserHandle {
  return { analyserRef: { current: null }, status: "idle" } as AudioAnalyserHandle;
}

describe("NowPlayingCanvas RAF scheduling", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    originalMatchMedia = window.matchMedia;
    stubReducedMotion(false);
    let id = 0;
    rafSpy = vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => {
      id += 1;
      return id;
    });
    cancelSpy = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    window.matchMedia = originalMatchMedia;
    if (visibilityOverridden) {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "visible",
      });
      visibilityOverridden = false;
    }
    vi.restoreAllMocks();
  });

  async function renderCanvas(props: { playing: boolean; active: boolean }): Promise<void> {
    await act(async () => {
      root = createRoot(container);
      root.render(
        <NowPlayingCanvas visualizer={idleVisualizer()} playing={props.playing} active={props.active} />,
      );
    });
  }

  it("schedules animation when playing, panel active, document visible, motion on", async () => {
    await renderCanvas({ playing: true, active: true });
    expect(rafSpy).toHaveBeenCalled();
  });

  it("schedules no animation when playback is paused", async () => {
    await renderCanvas({ playing: false, active: true });
    expect(rafSpy).not.toHaveBeenCalled();
  });

  it("schedules no animation when the panel is hidden", async () => {
    await renderCanvas({ playing: true, active: false });
    expect(rafSpy).not.toHaveBeenCalled();
  });

  it("schedules no animation when the document is hidden", async () => {
    setDocumentHidden(true);
    await renderCanvas({ playing: true, active: true });
    expect(rafSpy).not.toHaveBeenCalled();
  });

  it("schedules no animation when reduced motion is requested", async () => {
    stubReducedMotion(true);
    await renderCanvas({ playing: true, active: true });
    expect(rafSpy).not.toHaveBeenCalled();
  });

  it("cancels the loop when playback pauses", async () => {
    await renderCanvas({ playing: true, active: true });
    expect(rafSpy).toHaveBeenCalled();

    await act(async () => {
      root.render(<NowPlayingCanvas visualizer={idleVisualizer()} playing={false} active={true} />);
    });
    expect(cancelSpy).toHaveBeenCalled();
  });
});
