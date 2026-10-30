// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { LibraryChapterDetail } from "../../types/api";
import { NowPlayingView } from "./NowPlayingView";

const chapters: LibraryChapterDetail[] = [
  { id: 1, number: 1, title: "Futures", duration_ms: 33_000, mp3_path: "1.mp3" },
  { id: 2, number: 2, title: "Executors", duration_ms: 34_000, mp3_path: "2.mp3" },
  { id: 3, number: 3, title: "Pinning", duration_ms: 32_000, mp3_path: "3.mp3" },
];

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function render(overrides: Partial<Parameters<typeof NowPlayingView>[0]> = {}) {
  const props = {
    open: true,
    bookTitle: "Notes on Async Rust",
    bookAuthor: "Ada Marlowe",
    chapters,
    currentChapterNumber: 2,
    playing: true,
    playbackRate: 1,
    sleepTimer: { kind: "off" as const },
    sleepRemainingSec: null,
    onJumpToChapter: vi.fn(),
    onPlaybackRateChange: vi.fn(),
    onSleepTimerChange: vi.fn(),
    onClose: vi.fn(),
    ...overrides,
  };
  await act(async () => root.render(<NowPlayingView {...props} />));
  return props;
}

it("renders nothing while closed", async () => {
  await render({ open: false });
  expect(container.innerHTML).toBe("");
});

it("marks the current chapter and jumps to the one clicked", async () => {
  const props = await render();
  const current = container.querySelector('[aria-current="true"]');
  expect(current?.textContent).toContain("Executors");
  expect(container.querySelector(".np-view-chapter")?.textContent).toBe("Chapter 2 of 3 · Executors");
  await act(async () => (container.querySelectorAll<HTMLButtonElement>(".np-chapter")[2]).click());
  expect(props.onJumpToChapter).toHaveBeenCalledWith(chapters[2]);
});

it("shows the live playback position as it advances", async () => {
  const seek = { elapsedSec: 1, durationSec: 99, canSeek: true, onSeekTo: vi.fn() };
  await render(seek);
  await render({ ...seek, elapsedSec: 30 });
  expect(container.querySelector(".np-scrubber-times")?.textContent).toContain("00:30");
});

it.each([false, true])("gates missing chapter files only in chapter-playlist mode (%s)", async (chapterPlaylist) => {
  const missingChapter = { ...chapters[1], mp3_path: null };
  const props = await render({ chapterPlaylist, chapters: [chapters[0], missingChapter] });
  const missingButton = container.querySelectorAll<HTMLButtonElement>(".np-chapter")[1];
  expect(missingButton.disabled).toBe(chapterPlaylist);
  await act(async () => missingButton.click());
  if (chapterPlaylist) expect(props.onJumpToChapter).not.toHaveBeenCalled();
  else expect(props.onJumpToChapter).toHaveBeenCalledWith(missingChapter);
});

const combobox = (name: string) => container.querySelector<HTMLButtonElement>(`[role="combobox"][aria-label="${name}"]`)!;
const option = (label: string) =>
  [...container.querySelectorAll<HTMLElement>("[role=\"option\"]")].find((candidate) => candidate.textContent === label)!;

it("changes speed and sleep timer from their menus", async () => {
  const props = await render({ playbackRate: 1.25 });
  expect(combobox("Playback speed").textContent).toBe("1.25×");
  await act(async () => combobox("Playback speed").click());
  await act(async () => option("1.5×").click());
  expect(props.onPlaybackRateChange).toHaveBeenCalledWith(1.5);
  await act(async () => combobox("Sleep timer").click());
  await act(async () => option("30 min").click());
  expect(props.onSleepTimerChange).toHaveBeenCalledWith(30);
  await act(async () => combobox("Sleep timer").click());
  await act(async () => option("End of chapter").click());
  expect(props.onSleepTimerChange).toHaveBeenCalledWith("chapter");
});

it("keeps Escape for an open menu before closing the view", async () => {
  const props = await render();
  await act(async () => combobox("Sleep timer").click());
  await act(async () => {
    combobox("Sleep timer").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  expect(combobox("Sleep timer").getAttribute("aria-expanded")).toBe("false");
  expect(props.onClose).not.toHaveBeenCalled();
});

it("shows the sleep countdown once and announces only the choice", async () => {
  await render({ sleepTimer: { kind: "minutes", minutes: 15, endsAt: 0 }, sleepRemainingSec: 754 });
  const control = combobox("Sleep timer").closest(".np-control")!;
  expect(combobox("Sleep timer").textContent).toBe("12:34 left");
  // One visible statement of the running timer: no second countdown or duration beside the menu button.
  expect(control.textContent).toBe("Sleep timer12:34 left");
  expect(container.querySelectorAll("[aria-label=\"Sleep timer\"]")).toHaveLength(1);
  expect(container.querySelector("[role=\"status\"]")?.textContent).toBe("Pausing in 15 minutes");
  await act(async () => combobox("Sleep timer").click());
  expect([...container.querySelectorAll("#np-sleep [role=\"option\"]")].map((item) => item.textContent))
    .toEqual(["Off", "12:34 left", "30 min", "60 min", "End of chapter"]);
  expect(container.querySelector("#np-sleep [role=\"option\"][aria-selected=\"true\"]")?.textContent).toBe("12:34 left");
});

it("omits a missing author instead of naming it", async () => {
  await render({ bookAuthor: null });
  expect(container.querySelector(".np-view-author")).toBeNull();
  expect(container.textContent).not.toContain("Unknown author");
});

it("focuses its close button on open and closes on Escape", async () => {
  const props = await render();
  expect(document.activeElement?.getAttribute("aria-label")).toBe("Close now playing");
  await act(async () => {
    container.querySelector(".np-view")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  expect(props.onClose).toHaveBeenCalledOnce();
});

it("restores focus to the opener when closed", async () => {
  const opener = document.createElement("button");
  document.body.append(opener);
  opener.focus();
  try {
    await render();
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Close now playing");
    await render({ open: false });
    expect(document.activeElement).toBe(opener);
  } finally {
    opener.remove();
  }
});

it("preserves focus when the listener closes it from another transport control", async () => {
  const opener = document.createElement("button");
  const transport = document.createElement("button");
  document.body.append(opener, transport);
  opener.focus();
  try {
    await render();
    transport.focus();
    await render({ open: false });
    expect(document.activeElement).toBe(transport);
  } finally {
    opener.remove();
    transport.remove();
  }
});
