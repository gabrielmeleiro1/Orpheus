// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api/client";
import { BookCover } from "./BookCover";
import { CoverClientContext, CoverGenerationContext, CoverRefreshContext } from "./coverArt";

describe("BookCover", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function render(client: ApiClient | null, bookId: number, size: "sm" | "tile" = "tile") {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => root.render(
      <CoverClientContext.Provider value={client}>
        <CoverRefreshContext.Provider value={false}>
        <BookCover bookId={bookId} title="The Odyssey" author="Homer" size={size} />
        </CoverRefreshContext.Provider>
      </CoverClientContext.Provider>,
    ));
    return { container, root };
  }

  it("shows the source's own cover art once it loads", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:cover");
    const requestBlob = vi.fn(async () => new Blob(["png"], { type: "image/png" }));
    const { container, root } = await render({ requestBlob } as unknown as ApiClient, 41);
    try {
      expect(requestBlob).toHaveBeenCalledWith("/api/library/41/cover", { timeoutMs: 20_000 });
      expect(container.querySelector(".book-cover")?.classList.contains("has-art")).toBe(true);
      expect(container.querySelector("img")?.getAttribute("src")).toBe("blob:cover");
      expect(container.querySelector(".book-cover-bars")).toBeNull();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("falls back to the generated cover when the book has none, and asks only once", async () => {
    const requestBlob = vi.fn(async () => new Blob([]));
    const client = { requestBlob } as unknown as ApiClient;
    const first = await render(client, 42);
    const second = await render(client, 42);
    try {
      expect(first.container.querySelector("img")).toBeNull();
      // The title is shown beside every cover, so the generated one carries only the waveform.
      expect(first.container.querySelector(".book-cover-bars")).not.toBeNull();
      expect(first.container.textContent).toBe("");
      expect(requestBlob).toHaveBeenCalledTimes(1);
    } finally {
      await act(async () => first.root.unmount());
      await act(async () => second.root.unmount());
    }
  });

  it("draws small fallback covers as a waveform instead of placeholder initials", async () => {
    const { container, root } = await render(null, 43, "sm");
    try {
      expect(container.querySelector(".book-cover-bars")).not.toBeNull();
      expect(container.textContent).toBe("");
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("retries transient errors on refresh and remount, and releases shared URLs after the last cover leaves", async () => {
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:retried");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const requestBlob = vi.fn().mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValue(new Blob(["png"], { type: "image/png" }));
    const client = { requestBlob } as unknown as ApiClient;
    const first = await render(client, 51);
    await act(async () => first.root.render(<CoverClientContext.Provider value={client}><CoverRefreshContext.Provider value={true}>
      <BookCover bookId={51} title="The Odyssey" />
    </CoverRefreshContext.Provider></CoverClientContext.Provider>));
    expect(first.container.querySelector("img")?.getAttribute("src")).toBe("blob:retried");
    const second = await render(client, 51);
    expect(requestBlob).toHaveBeenCalledTimes(2);
    expect(createObjectURL).toHaveBeenCalledOnce();
    await act(async () => first.root.unmount());
    expect(revokeObjectURL).not.toHaveBeenCalled();
    await act(async () => second.root.unmount());
    expect(revokeObjectURL).toHaveBeenCalledExactlyOnceWith("blob:retried");
    createObjectURL.mockRestore();
    revokeObjectURL.mockRestore();
  });

  it("refetches a same-ID cover after library replacement and ignores abandoned responses", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:new-library");
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    let resolveOld!: (blob: Blob) => void;
    const requestBlob = vi.fn().mockImplementationOnce(() => new Promise<Blob>((resolve) => { resolveOld = resolve; }))
      .mockResolvedValue(new Blob(["new"], { type: "image/png" }));
    const client = { requestBlob } as unknown as ApiClient;
    const container = document.createElement("div");
    const root = createRoot(container);
    const draw = (generation: number) => root.render(<CoverClientContext.Provider value={client}><CoverGenerationContext.Provider value={generation}>
      <BookCover bookId={52} title="Book" />
    </CoverGenerationContext.Provider></CoverClientContext.Provider>);
    await act(async () => draw(0));
    await act(async () => draw(1));
    await act(async () => resolveOld(new Blob(["old"], { type: "image/png" })));
    expect(requestBlob).toHaveBeenCalledTimes(2);
    expect(URL.createObjectURL).toHaveBeenCalledOnce();
    expect(container.querySelector("img")?.getAttribute("src")).toBe("blob:new-library");
    await act(async () => root.unmount());
    expect(revoke).toHaveBeenCalledOnce();
    vi.restoreAllMocks();
  });
});
