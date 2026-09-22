// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { PlayerPanel } from "./PlayerPanel";

it("offers chapter navigation and explicit keyboard rings on both sliders", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const style = document.createElement("style");
  style.textContent = readFileSync("src/features/player/player.css", "utf8");
  document.head.append(style);
  const root = createRoot(container);
  const noop = () => {};
  try {
    await act(async () => root.render(<PlayerPanel enabled visible playing={false} title="Paused" elapsedSec={0} durationSec={10} canSeek volume={80} canGoNext canGoPrevious chapterPlaylist onTogglePlay={noop} onSeekBy={noop} onSeekTo={noop} onNext={noop} onPrevious={noop} onVolumeChange={noop} onClose={noop} />));
    expect(container.querySelector('[aria-label="Next chapter"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Previous chapter"]')).not.toBeNull();
    for (const selector of [".player-seek", ".player-volume"]) {
      const slider = container.querySelector<HTMLInputElement>(selector)!;
      slider.focus();
      expect(document.activeElement).toBe(slider);
      const rule = [...style.sheet!.cssRules].find((candidate) => (candidate as CSSStyleRule).selectorText?.includes(`${selector}:focus-visible`)) as CSSStyleRule;
      expect(rule.style.boxShadow).toBe("var(--focus-ring)");
    }
  } finally {
    await act(async () => root.unmount());
    container.remove();
    style.remove();
  }
});
