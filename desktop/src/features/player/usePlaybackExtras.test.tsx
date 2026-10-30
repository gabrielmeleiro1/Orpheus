// @vitest-environment happy-dom

import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { usePlaybackRate, useSleepTimer } from "./usePlaybackExtras";

let container: HTMLDivElement;
let root: Root;
let audio: HTMLAudioElement;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  window.localStorage.clear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  audio = document.createElement("audio");
  let paused = false;
  Object.defineProperty(audio, "paused", { configurable: true, get: () => paused });
  audio.pause = vi.fn(() => { paused = true; });
  audio.play = vi.fn(async () => { paused = false; audio.dispatchEvent(new Event("play")); });
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function RateHarness({ src, onReady }: { src: string; onReady: (api: ReturnType<typeof usePlaybackRate>) => void }) {
  const ref = useRef<HTMLAudioElement | null>(audio);
  onReady(usePlaybackRate(ref, src));
  return null;
}

function SleepHarness({ chapter, bookKey = "book-1", onReady }: { chapter: number | null; bookKey?: string; onReady: (api: ReturnType<typeof useSleepTimer>) => void }) {
  const ref = useRef<HTMLAudioElement | null>(audio);
  onReady(useSleepTimer(ref, { bookKey, chapterAt: () => ({ number: chapter, endSec: chapter === null ? null : chapter * 60 }) }));
  return null;
}

it("applies the speed to each new source and remembers it", async () => {
  let api!: ReturnType<typeof usePlaybackRate>;
  await act(async () => root.render(<RateHarness src="a.mp3" onReady={(next) => { api = next; }} />));
  expect(api.playbackRate).toBe(1);

  await act(async () => api.setPlaybackRate(1.5));
  expect(audio.playbackRate).toBe(1.5);
  expect(audio.defaultPlaybackRate).toBe(1.5);
  expect(window.localStorage.getItem("orpheus.playbackRate")).toBe("1.5");

  audio.playbackRate = 1;
  await act(async () => root.render(<RateHarness src="b.mp3" onReady={(next) => { api = next; }} />));
  expect(audio.playbackRate).toBe(1.5);
});

it("ignores a stored speed that is not one of the offered rates", async () => {
  window.localStorage.setItem("orpheus.playbackRate", "9");
  let api!: ReturnType<typeof usePlaybackRate>;
  await act(async () => root.render(<RateHarness src="a.mp3" onReady={(next) => { api = next; }} />));
  expect(api.playbackRate).toBe(1);
});

it("pauses when a minutes timer runs out and counts down meanwhile", async () => {
  vi.useFakeTimers();
  let api!: ReturnType<typeof useSleepTimer>;
  await act(async () => root.render(<SleepHarness chapter={1} onReady={(next) => { api = next; }} />));
  await act(async () => { await audio.play(); });

  await act(async () => api.setSleepTimer(15));
  expect(api.sleepRemainingSec).toBe(15 * 60);
  await act(async () => vi.advanceTimersByTime(60_000));
  expect(api.sleepRemainingSec).toBe(14 * 60);
  expect(audio.pause).not.toHaveBeenCalled();

  await act(async () => vi.advanceTimersByTime(14 * 60_000));
  expect(audio.pause).toHaveBeenCalled();
  expect(api.sleepTimer.kind).toBe("off");
});

it("holds a minutes timer while playback is paused and resumes it with playback", async () => {
  vi.useFakeTimers();
  let api!: ReturnType<typeof useSleepTimer>;
  await act(async () => root.render(<SleepHarness chapter={1} onReady={(next) => { api = next; }} />));
  await act(async () => { await audio.play(); });
  await act(async () => api.setSleepTimer(15));
  await act(async () => vi.advanceTimersByTime(60_000));

  await act(async () => { audio.pause(); audio.dispatchEvent(new Event("pause")); });
  vi.mocked(audio.pause).mockClear();
  await act(async () => vi.advanceTimersByTime(30 * 60_000));
  expect(api.sleepRemainingSec).toBe(14 * 60);
  expect(api.sleepTimer.kind).toBe("minutes");
  expect(audio.pause).not.toHaveBeenCalled();

  await act(async () => { await audio.play(); });
  await act(async () => vi.advanceTimersByTime(13 * 60_000));
  expect(api.sleepRemainingSec).toBe(60);
  await act(async () => vi.advanceTimersByTime(60_000));
  expect(audio.pause).toHaveBeenCalled();
  expect(api.sleepTimer.kind).toBe("off");
});

it("starts a minutes timer set while paused only when playback starts", async () => {
  vi.useFakeTimers();
  let api!: ReturnType<typeof useSleepTimer>;
  await act(async () => root.render(<SleepHarness chapter={1} onReady={(next) => { api = next; }} />));
  audio.pause();
  await act(async () => api.setSleepTimer(15));
  await act(async () => vi.advanceTimersByTime(20 * 60_000));
  expect(api.sleepRemainingSec).toBe(15 * 60);

  await act(async () => { await audio.play(); });
  await act(async () => vi.advanceTimersByTime(60_000));
  expect(api.sleepRemainingSec).toBe(14 * 60);
});

it("stops at a natural boundary and allows immediate manual Play", async () => {
  vi.useFakeTimers();
  let api!: ReturnType<typeof useSleepTimer>;
  await act(async () => root.render(<SleepHarness chapter={3} onReady={(next) => { api = next; }} />));
  await act(async () => { await audio.play(); });
  await act(async () => api.setSleepTimer("chapter"));
  expect(api.sleepTimer).toEqual({ kind: "chapter", chapterNumber: 3 });

  audio.currentTime = 180;
  await act(async () => { expect(api.stopAtChapterEnd()).toBe(true); });
  expect(audio.paused).toBe(true);
  expect(api.sleepTimer.kind).toBe("off");

  // The transport suppresses auto-advance synchronously; no play listener
  // remains to swallow the listener's next manual Play.
  await act(async () => { await audio.play(); });
  expect(audio.paused).toBe(false);
});

it("re-arms after a manual chapter jump instead of treating it as a chapter ending", async () => {
  let api!: ReturnType<typeof useSleepTimer>;
  await act(async () => root.render(<SleepHarness chapter={1} onReady={(next) => { api = next; }} />));
  await act(async () => api.setSleepTimer("chapter"));
  api.onSeeking();
  audio.currentTime = 75;
  expect(api.stopAtChapterEnd()).toBe(false);
  await act(async () => root.render(<SleepHarness chapter={2} onReady={(next) => { api = next; }} />));
  await act(async () => api.onSeeked());
  expect(api.sleepTimer).toEqual({ kind: "chapter", chapterNumber: 2 });
  expect(api.stopAtChapterEnd()).toBe(false);
  expect(audio.pause).not.toHaveBeenCalled();
});

it("cancels a chapter timer on a same-number chapter in another book", async () => {
  let api!: ReturnType<typeof useSleepTimer>;
  await act(async () => root.render(<SleepHarness chapter={1} onReady={(next) => { api = next; }} />));
  await act(async () => api.setSleepTimer("chapter"));
  await act(async () => root.render(<SleepHarness chapter={1} bookKey="book-2" onReady={(next) => { api = next; }} />));
  expect(api.sleepTimer.kind).toBe("off");
  expect(api.stopAtChapterEnd(true)).toBe(false);
  expect(audio.pause).not.toHaveBeenCalled();
});

it("handles the final ended event once, including chapters without a known duration", async () => {
  let api!: ReturnType<typeof useSleepTimer>;
  await act(async () => root.render(<SleepHarness chapter={null} onReady={(next) => { api = next; }} />));
  await act(async () => api.setSleepTimer("chapter"));
  await act(async () => { expect(api.stopAtChapterEnd(true)).toBe(true); });
  expect(api.sleepTimer.kind).toBe("off");
  expect(api.stopAtChapterEnd(true)).toBe(false);
});

it("turns the timer off without touching playback", async () => {
  let api!: ReturnType<typeof useSleepTimer>;
  await act(async () => root.render(<SleepHarness chapter={1} onReady={(next) => { api = next; }} />));
  await act(async () => { await audio.play(); });
  await act(async () => api.setSleepTimer(30));
  await act(async () => api.setSleepTimer("off"));
  expect(api.sleepTimer.kind).toBe("off");
  expect(api.sleepRemainingSec).toBeNull();
  expect(audio.paused).toBe(false);
});
