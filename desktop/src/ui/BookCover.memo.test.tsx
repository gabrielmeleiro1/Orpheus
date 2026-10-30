// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

const coverArt = vi.hoisted(() => ({ calls: 0 }));
vi.mock("./coverArt", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./coverArt")>()),
  useCoverArt: () => {
    coverArt.calls += 1;
    return null;
  },
}));

import { BookCover } from "./BookCover";

describe("BookCover rendering", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    coverArt.calls = 0;
  });

  it("does not re-render when its parent re-renders with the same book", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    const root = createRoot(container);
    // Sidebars and shelves re-render once per playback tick or progress event.
    const draw = (tick: number) => root.render(
      <div data-tick={tick}>
        <BookCover bookId={7} sourceKey="/books/7.epub" title="The Odyssey" author="Homer" size="sm" />
      </div>,
    );
    try {
      await act(async () => draw(0));
      await act(async () => draw(1));
      await act(async () => draw(2));
      expect(coverArt.calls).toBe(1);

      await act(async () => root.render(
        <div data-tick={3}>
          <BookCover bookId={7} sourceKey="/books/7.epub" title="The Iliad" author="Homer" size="sm" />
        </div>,
      ));
      expect(coverArt.calls).toBe(2);
    } finally {
      await act(async () => root.unmount());
    }
  });
});
