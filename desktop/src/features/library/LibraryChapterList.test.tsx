// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

import { LibraryChapterList } from "./LibraryChapterList";

it("labels the playing chapter, calls the latest callbacks and disables rows during a transition", async () => {
  const chapters = Array.from({ length: 3 }, (_, index) => ({
    id: index + 1, number: index + 1, title: `Chapter ${index + 1}`, duration_ms: 60_000, mp3_path: null,
  }));
  const container = document.createElement("div");
  const root = createRoot(container);
  const onJumpToChapter = vi.fn();
  const onLoadCleaningPreview = vi.fn(async () => undefined);
  const props = { chapters, currentChapterNumber: 1, playing: false, detailTransitionPending: false,
    cleaningPreviewLoading: false, onJumpToChapter, onLoadCleaningPreview };
  const buttons = () => [...container.querySelectorAll<HTMLButtonElement>("button")];
  try {
    await act(async () => root.render(<LibraryChapterList {...props} currentChapterNumber={2} playing />));
    expect(container.querySelector('[aria-current="true"]')?.getAttribute("aria-label")).toBe("Chapter 2 — playing");
    const replacementJump = vi.fn();
    const replacementPreview = vi.fn(async () => undefined);
    await act(async () => root.render(<LibraryChapterList {...props} onJumpToChapter={replacementJump}
      onLoadCleaningPreview={replacementPreview} />));
    await act(async () => {
      buttons().find((button) => button.textContent?.includes("Chapter 1"))!.click();
      container.querySelector<HTMLButtonElement>('[aria-label="Original and cleaned text: Chapter 1"]')!.click();
    });
    expect(replacementJump).toHaveBeenCalledWith(chapters[0]);
    expect(replacementPreview).toHaveBeenCalledWith(1);
    expect(onJumpToChapter).not.toHaveBeenCalled();
    expect(onLoadCleaningPreview).not.toHaveBeenCalled();
    await act(async () => root.render(<LibraryChapterList {...props} detailTransitionPending />));
    expect(buttons().every((button) => button.disabled)).toBe(true);
  } finally {
    await act(async () => root.unmount());
  }
});
