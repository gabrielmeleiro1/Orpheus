import { describe, expect, it } from "vitest";

import { chapterDisplayLabel, chapterPlaybackStatusLabel } from "./chapterPlaybackLabel";
import {
  resolvePlayerCanSeek,
  resolvePlayerDurationSec,
  seekAudioIfReady,
} from "./playerDurationModel";

describe("chapterPlaybackStatusLabel", () => {
  it("says Playing only while audio is playing", () => {
    expect(chapterPlaybackStatusLabel({ isCurrent: true, playing: true })).toBe("Playing");
    expect(chapterPlaybackStatusLabel({ isCurrent: true, playing: false })).toBe("Selected");
    expect(chapterPlaybackStatusLabel({ isCurrent: false, playing: false })).toBeNull();
  });
});

describe("resolvePlayerDurationSec", () => {
  it("uses whole-book chapter sum as fallback before audio metadata for multi-chapter books", () => {
    // Packaged playback is a whole-book/document stream; three 4s chapters => 12s.
    expect(
      resolvePlayerDurationSec({
        audioDurationSec: 0,
        bookDurationMs: 12_000,
        chapterDurationMs: 4_000,
      }),
    ).toBe(12);
  });

  it("keeps single-chapter document fallback when only one chapter duration is known", () => {
    expect(
      resolvePlayerDurationSec({
        audioDurationSec: 0,
        bookDurationMs: 4_000,
        chapterDurationMs: 4_000,
      }),
    ).toBe(4);
    expect(
      resolvePlayerDurationSec({
        audioDurationSec: 0,
        bookDurationMs: null,
        chapterDurationMs: 4_000,
      }),
    ).toBe(4);
  });

  it("lets audio metadata override the book duration fallback", () => {
    expect(
      resolvePlayerDurationSec({
        audioDurationSec: 9.7,
        bookDurationMs: 12_000,
        chapterDurationMs: 4_000,
      }),
    ).toBe(9);
  });

  it("does not fabricate duration when nothing is known", () => {
    expect(
      resolvePlayerDurationSec({
        audioDurationSec: 0,
        chapterDurationMs: 0,
        bookDurationMs: null,
      }),
    ).toBe(0);
  });
});

describe("resolvePlayerCanSeek", () => {
  it("disables seek until a book's audio is loaded", () => {
    expect(resolvePlayerCanSeek({ audioSrc: null, loadedBookId: null })).toBe(false);
    expect(resolvePlayerCanSeek({ audioSrc: "http://127.0.0.1/audio?book=7", loadedBookId: null })).toBe(false);
    expect(resolvePlayerCanSeek({ audioSrc: "http://127.0.0.1/audio?book=7", loadedBookId: 7 })).toBe(true);
  });
});

describe("seekAudioIfReady", () => {
  it("does not mutate currentTime before audio is loaded", () => {
    const audio = { src: "", currentTime: 42, duration: 100 };
    expect(seekAudioIfReady({ audio, loadedBookId: 7, positionSeconds: 10 })).toBe(false);
    expect(audio.currentTime).toBe(42);
  });

  it("seeks loaded audio, clamped to its duration", () => {
    const audio = { src: "http://127.0.0.1/audio?book=7", currentTime: 42, duration: 100 };
    expect(seekAudioIfReady({ audio, loadedBookId: 7, positionSeconds: 10 })).toBe(true);
    expect(audio.currentTime).toBe(10);
    expect(seekAudioIfReady({ audio, loadedBookId: 7, positionSeconds: 500 })).toBe(true);
    expect(audio.currentTime).toBe(100);
  });
});

describe("chapterDisplayLabel", () => {
  it("does not repeat a chapter number the title already carries", () => {
    expect(chapterDisplayLabel({ number: 1, title: "Chapter 1: The Keeper's Daughter" })).toBe("Chapter 1: The Keeper's Daughter");
    expect(chapterDisplayLabel({ number: 3, title: "CHAPTER 3: Fog" })).toBe("CHAPTER 3: Fog");
    expect(chapterDisplayLabel({ number: 4, title: "chapter 4" })).toBe("chapter 4");
    expect(chapterDisplayLabel({ number: 2, title: "2. Arrival" })).toBe("2. Arrival");
  });

  it("prefixes titles that do not name the chapter", () => {
    expect(chapterDisplayLabel({ number: 1, title: "The Keeper's Daughter" })).toBe("Chapter 1: The Keeper's Daughter");
    // Another chapter's number in the title is still prefixed.
    expect(chapterDisplayLabel({ number: 12, title: "Chapter 1 revisited" })).toBe("Chapter 12: Chapter 1 revisited");
    expect(chapterDisplayLabel({ number: 1, title: "10 Rules" })).toBe("Chapter 1: 10 Rules");
    expect(chapterDisplayLabel({ number: 5, title: "" })).toBe("Chapter 5");
    expect(chapterDisplayLabel(null)).toBe("Chapter unknown");
  });
});
