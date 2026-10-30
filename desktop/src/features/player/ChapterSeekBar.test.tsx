// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { ChapterSeekBar } from "./ChapterSeekBar";

afterEach(() => vi.unstubAllGlobals());

it("steps keyboard seeks from the live position rather than the last rendered value", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const onStep = vi.fn();
  const onChange = vi.fn();
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    // The rendered value lags the audio clock by up to a second while playing.
    await act(async () => root.render(
      <ChapterSeekBar valueSec={769.6} maxSec={2610} disabled={false} label="Seek" valueText="" marks={[]}
        onChange={onChange} onStep={onStep} />,
    ));
    const input = container.querySelector("input")!;
    const press = (key: string) => {
      const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      input.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(press("ArrowRight")).toBe(true);
    expect(press("ArrowLeft")).toBe(true);
    expect(press("PageUp")).toBe(true);
    expect(onStep.mock.calls).toEqual([[5], [-5], [60]]);
    expect(onChange).not.toHaveBeenCalled();
    // Home/End keep their native meaning.
    expect(press("Home")).toBe(false);
  } finally {
    await act(async () => root.unmount());
  }
});
