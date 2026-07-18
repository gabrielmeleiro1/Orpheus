import { describe, expect, it } from "vitest";

import type { LibraryBookDetail, LibraryBookSummary } from "../../types/api";
import { buildBookSummary, formatSummaryMeta } from "./libraryDisplayModel";

function summary(overrides: Partial<LibraryBookSummary> = {}): LibraryBookSummary {
  return {
    id: 1,
    title: "Book",
    author: "Ada",
    conversion_mode: "audiobook",
    tags: "",
    favorite: false,
    source_available: true,
    total_chapters: 10,
    completed_chapters: 4,
    ...overrides,
  };
}

function detail(overrides: Partial<LibraryBookDetail> = {}): LibraryBookDetail {
  return {
    id: 1,
    title: "Book",
    author: "Ada",
    source_path: null,
    source_type: "epub",
    conversion_mode: "audiobook",
    tags: "",
    favorite: false,
    source_available: true,
    total_chapters: 3,
    playback_path: null,
    playback_status: "pending",
    playback_position_sec: 0,
    chapters: [],
    playback_url: null,
    ...overrides,
  };
}

describe("formatSummaryMeta", () => {
  it("formats an audiobook row with completion counts", () => {
    expect(formatSummaryMeta(summary())).toBe("Ada · audiobook · 4/10");
  });

  it("labels document audio and flags missing sources and unknown authors", () => {
    expect(
      formatSummaryMeta(
        summary({ author: null, conversion_mode: "document_audio", source_available: false }),
      ),
    ).toBe("Unknown author · document · 4/10 · source missing");
  });
});

describe("buildBookSummary", () => {
  it("returns placeholder strings when nothing is selected", () => {
    const view = buildBookSummary(null);
    expect(view.title).toBe("No book selected");
    expect(view.meta).toBe("Select a book to inspect details.");
    expect(view.extra).toBe("—");
  });

  it("singularizes the chapter count and marks pending playback", () => {
    const view = buildBookSummary(
      detail({ chapters: [{ id: 1, number: 1, title: "One", duration_ms: 1000, mp3_path: null }] }),
    );
    expect(view.meta).toBe("Ada · 1 chapter");
    expect(view.extra).toBe("Playback output pending");
  });

  it("reports a resume offset once playback output exists", () => {
    const view = buildBookSummary(
      detail({ playback_path: "/out.mp3", playback_url: "http://x/audio", playback_position_sec: 65 }),
    );
    expect(view.extra).toBe("Playback ready · resume 01:05");
  });

  it("reports plain playback-ready when at the start", () => {
    const view = buildBookSummary(
      detail({ playback_path: "/out.mp3", playback_url: "http://x/audio", playback_position_sec: 0 }),
    );
    expect(view.extra).toBe("Playback ready");
  });
});
