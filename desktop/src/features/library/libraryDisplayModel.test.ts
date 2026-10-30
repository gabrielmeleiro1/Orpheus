import { describe, expect, it } from "vitest";

import type { LibraryBookDetail } from "../../types/api";
import { bookAvailabilityAdvice, bookAvailabilityNote, buildBookSummary } from "./libraryDisplayModel";

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

describe("bookAvailabilityNote", () => {
  it("names what is wrong with a book that is not normally playable", () => {
    expect(bookAvailabilityNote({ total_chapters: 5, completed_chapters: 0, audio_available: false })).toBe("Not converted");
    expect(bookAvailabilityNote({ total_chapters: 5, completed_chapters: 0 })).toBe("Not converted");
    expect(bookAvailabilityNote({ total_chapters: 5, completed_chapters: 5, audio_available: false })).toBe("Audio missing");
    expect(bookAvailabilityNote({ total_chapters: 5, completed_chapters: 3, audio_available: true }))
      .toBe("Unfinished · 3/5 chapters");
    expect(bookAvailabilityNote({ total_chapters: 5, completed_chapters: 5, audio_available: true })).toBeNull();
    // Older backends report no audio flag: chapter counts alone decide.
    expect(bookAvailabilityNote({ total_chapters: 5, completed_chapters: 5 })).toBeNull();
  });

  it("advises Repair audio only while the source file exists", () => {
    const unfinished = { total_chapters: 5, completed_chapters: 3, audio_available: true };
    const missing = { total_chapters: 5, completed_chapters: 5, audio_available: false };
    expect(bookAvailabilityAdvice({ ...unfinished, source_available: true })).toBe("Repair audio finishes it.");
    expect(bookAvailabilityAdvice({ ...missing, source_available: true })).toBe("Repair audio rebuilds it.");
    expect(bookAvailabilityAdvice({ total_chapters: 5, completed_chapters: 0, audio_available: false })).toBe("Repair audio creates the audio.");
    expect(bookAvailabilityAdvice({ ...missing, source_available: false }))
      .toBe("The source file is missing, so the audio cannot be rebuilt. Delete the book, or create it again from the original file.");
    expect(bookAvailabilityAdvice({ ...unfinished, source_available: false })).not.toContain("Repair");
    expect(bookAvailabilityAdvice({ total_chapters: 5, completed_chapters: 5, audio_available: true, source_available: false })).toBeNull();
  });
});
