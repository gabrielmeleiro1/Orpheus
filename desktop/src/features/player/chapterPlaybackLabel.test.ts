import { describe, expect, it } from "vitest";

import { chapterPlaybackStatusLabel } from "./chapterPlaybackLabel";
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
  it("keeps seeded duration visible but disables seek until the selected book's audio is loaded", () => {
    const seededDurationSec = resolvePlayerDurationSec({
      audioDurationSec: 0,
      bookDurationMs: 12_000,
      chapterDurationMs: 4_000,
    });
    expect(seededDurationSec).toBe(12);

    expect(
      resolvePlayerCanSeek({
        audioSrc: null,
        loadedBookId: null,
        selectedBookId: 7,
      }),
    ).toBe(false);

    expect(
      resolvePlayerCanSeek({
        audioSrc: "http://127.0.0.1/audio?book=7",
        loadedBookId: 7,
        selectedBookId: 7,
      }),
    ).toBe(true);

    expect(
      resolvePlayerCanSeek({
        audioSrc: "http://127.0.0.1/audio?book=3",
        loadedBookId: 3,
        selectedBookId: 7,
      }),
    ).toBe(false);
  });
});

describe("seekAudioIfReady", () => {
  it("does not mutate currentTime when selected and loaded book ids mismatch", () => {
    const audio = {
      src: "http://127.0.0.1/audio?book=3",
      currentTime: 42,
      duration: 100,
    };

    const applied = seekAudioIfReady({
      audio,
      loadedBookId: 3,
      selectedBookId: 7,
      positionSeconds: 10,
    });

    expect(applied).toBe(false);
    expect(audio.currentTime).toBe(42);
  });

  it("applies seek once selected and loaded book ids match", () => {
    const audio = {
      src: "http://127.0.0.1/audio?book=7",
      currentTime: 42,
      duration: 100,
    };

    const applied = seekAudioIfReady({
      audio,
      loadedBookId: 7,
      selectedBookId: 7,
      positionSeconds: 10,
    });

    expect(applied).toBe(true);
    expect(audio.currentTime).toBe(10);
  });
});
