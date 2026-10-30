// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";
import { PlayerPanel } from "./PlayerPanel";

it("offers chapter navigation in chapter-playlist mode", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const noop = () => {};
  try {
    await act(async () => root.render(<PlayerPanel enabled visible playing={false} title="Paused" elapsedSec={0} durationSec={10} canSeek volume={80} canGoNext canGoPrevious chapterPlaylist onTogglePlay={noop} onSeekBy={noop} onSeekTo={noop} onNext={noop} onPrevious={noop} onVolumeChange={noop} onClose={noop} />));
    expect(container.querySelector('[aria-label="Next chapter"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Previous chapter"]')).not.toBeNull();
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it.each([
  { elapsedSec: 65, durationSec: 3599, elapsed: "01:05", duration: "59:59" },
  { elapsedSec: 3600, durationSec: 5305, elapsed: "1:00:00", duration: "1:28:25" },
  { elapsedSec: 5305, durationSec: 37000, elapsed: "1:28:25", duration: "10:16:40" },
])("formats playback times and seek value text for $duration", async ({ elapsedSec, durationSec, elapsed, duration }) => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const noop = () => {};
  try {
    await act(async () => root.render(<PlayerPanel enabled visible playing={false} title="Paused" elapsedSec={elapsedSec} durationSec={durationSec} canSeek volume={80} onTogglePlay={noop} onSeekBy={noop} onSeekTo={noop} onNext={noop} onPrevious={noop} onVolumeChange={noop} onClose={noop} />));
    expect(container.querySelector("#player-elapsed")?.textContent).toBe(elapsed);
    expect(container.querySelector("#player-duration")?.textContent).toBe(duration);
    expect(container.querySelector(".player-seek")?.getAttribute("aria-valuetext")).toBe(`${elapsed} of ${duration}`);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it("shows speed and sleep chips only while Now Playing is closed", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const noop = () => {};
  const draw = (nowPlayingOpen: boolean) => root.render(<PlayerPanel enabled visible playing title="Playing" bookTitle="Book" elapsedSec={0} durationSec={10} canSeek volume={80} playbackRate={1.5} sleepLabel="Sleep 14:59" nowPlayingOpen={nowPlayingOpen} onToggleNowPlaying={noop} onTogglePlay={noop} onSeekBy={noop} onSeekTo={noop} onNext={noop} onPrevious={noop} onVolumeChange={noop} onClose={noop} />);
  try {
    await act(async () => draw(false));
    expect(container.querySelector(".player-rate-chip")?.textContent).toBe("1.5×");
    expect(container.querySelector(".player-sleep-chip")?.textContent).toBe("Sleep 14:59");
    await act(async () => draw(true));
    expect(container.querySelector(".player-rate-chip")).toBeNull();
    expect(container.querySelector(".player-sleep-chip")).toBeNull();
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
