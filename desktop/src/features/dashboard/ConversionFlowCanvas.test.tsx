// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConversionFlowCanvas } from "./ConversionFlowCanvas";

/**
 * The dashboard canvases run a 30fps RAF loop that is pure idle CPU when
 * nothing is happening. These tests pin the scheduling contract: the loop
 * exists only when the conversion is active AND the panel is visible AND the
 * document is visible AND reduced motion is off. Every other combination must
 * draw a single static frame and schedule no animation frame.
 *
 * happy-dom's canvas has no 2D context, so `draw` early-returns and dataset
 * writes are unobservable — the RAF spy is the reliable signal, and it is the
 * behavior we actually care about for CPU.
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

describe("ConversionFlowCanvas RAF scheduling", () => {
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

  async function renderCanvas(props: { active: boolean; visible?: boolean }): Promise<void> {
    await act(async () => {
      root = createRoot(container);
      root.render(<ConversionFlowCanvas active={props.active} visible={props.visible ?? true} />);
    });
  }

  it("schedules animation when active, visible, document visible, motion on", async () => {
    await renderCanvas({ active: true, visible: true });
    expect(rafSpy).toHaveBeenCalled();
  });

  it("schedules no animation when the conversion is inactive", async () => {
    await renderCanvas({ active: false, visible: true });
    expect(rafSpy).not.toHaveBeenCalled();
  });

  it("schedules no animation when the panel is hidden", async () => {
    await renderCanvas({ active: true, visible: false });
    expect(rafSpy).not.toHaveBeenCalled();
  });

  it("schedules no animation when the document is hidden", async () => {
    setDocumentHidden(true);
    await renderCanvas({ active: true, visible: true });
    expect(rafSpy).not.toHaveBeenCalled();
  });

  it("schedules no animation when reduced motion is requested", async () => {
    stubReducedMotion(true);
    await renderCanvas({ active: true, visible: true });
    expect(rafSpy).not.toHaveBeenCalled();
  });

  it("cancels the loop when the conversion goes idle", async () => {
    await renderCanvas({ active: true, visible: true });
    expect(rafSpy).toHaveBeenCalled();

    await act(async () => {
      root.render(<ConversionFlowCanvas active={false} visible={true} />);
    });
    expect(cancelSpy).toHaveBeenCalled();
  });
});
