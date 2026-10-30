import { afterEach, describe, expect, it, vi } from "vitest";
import type { RequestJson } from "../http";
import { createLibraryApi } from "./library";

afterEach(() => vi.unstubAllGlobals());

describe("library epoch requests", () => {
  it("sends the selected epoch with library mutations", async () => {
    const requestJson = vi.fn().mockResolvedValue({});
    const api = createLibraryApi({ requestJson: requestJson as RequestJson, resolvedBaseUrl: "http://localhost", usesDesktopAuth: false });
    await api.updateBookPlaybackPosition(1, 4, { expectedEpoch: "selected" });
    expect(JSON.parse(requestJson.mock.calls[0][1].body)).toEqual({ position_sec: 4, expected_epoch: "selected" });
    await api.deleteBook(1, { expectedEpoch: "selected" });
    expect(requestJson.mock.calls[1][0]).toBe("/api/library/1?expected_epoch=selected");
    await api.updateLibraryBook(1, { title: "Edited", expected_epoch: "selected" });
    expect(JSON.parse(requestJson.mock.calls[2][1].body)).toEqual({ title: "Edited", expected_epoch: "selected" });
    // The epoch is wire payload, never a fetch option.
    for (const [, init] of requestJson.mock.calls) {
      expect(init).not.toHaveProperty("expectedEpoch");
    }
  });

  it("mints audio credentials from the selected epoch without refreshing identity", async () => {
    vi.stubGlobal("window", { location: { origin: "http://localhost" } });
    const requestJson = vi.fn().mockResolvedValue({ token: "scoped", expires_at: 1 });
    const api = createLibraryApi({ requestJson: requestJson as RequestJson, resolvedBaseUrl: "http://localhost", usesDesktopAuth: true });
    await api.getLibraryAudioUrl(1, { expectedEpoch: "selected" });
    expect(requestJson).toHaveBeenCalledTimes(1);
    expect(JSON.parse(requestJson.mock.calls[0][1].body)).toEqual({ scope: "audio", book_id: 1, expected_epoch: "selected" });
    expect(requestJson.mock.calls[0][1]).not.toHaveProperty("expectedEpoch");
    await expect(api.getLibraryAudioUrl(1)).rejects.toThrow("expectedEpoch");
    expect(requestJson).toHaveBeenCalledTimes(1);
  });
});
