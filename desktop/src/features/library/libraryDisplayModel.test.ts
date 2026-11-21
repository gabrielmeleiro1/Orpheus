import { describe, expect, it } from "vitest";

import { bookAvailabilityAdvice, bookAvailabilityNote } from "./libraryDisplayModel";

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
