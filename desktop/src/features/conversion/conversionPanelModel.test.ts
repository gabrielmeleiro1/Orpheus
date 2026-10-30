import { describe, expect, it } from "vitest";

import type { SourcePreflight } from "../../types/api";
import { pastedTextEstimate } from "./conversionPanelHelpers";
import {
  createQueueItem,
  estimatedAudioNote,
  isPreflightConvertible,
  preflightBlockingReason,
} from "./conversionPanelModel";

function preflight(overrides: Partial<SourcePreflight> = {}): SourcePreflight {
  return {
    title: null,
    author: null,
    chapter_count: 1,
    source_type: "epub",
    sample: "",
    warnings: [],
    size_bytes: 10,
    requires_ocr: false,
    pdf_text_layer: null,
    ...overrides,
  };
}

describe("conversion queue item creation", () => {
  it("creates a preflighting item from a source path", () => {
    expect(createQueueItem(" /tmp/source.epub ", "audiobook")).toEqual({
      id: expect.any(String),
      conversionMode: "audiobook",
      sourcePath: "/tmp/source.epub",
      label: "source.epub",
      title: "",
      author: "",
      status: "preflighting",
      error: "",
      preflight: null,
      jobId: null,
    });
  });

  it("gives every created item its own id", () => {
    expect(createQueueItem("/tmp/source.epub", "audiobook").id)
      .not.toBe(createQueueItem("/tmp/source.epub", "audiobook").id);
  });

  it("uses label and preflight metadata when present", () => {
    expect(
      createQueueItem(
        " /tmp/source.pdf ",
        "document_audio",
        " Source Label ",
        preflight({ title: " Title ", author: " Author " }),
      ),
    ).toMatchObject({
      conversionMode: "document_audio",
      sourcePath: "/tmp/source.pdf",
      label: "Source Label",
      title: "Title",
      author: "Author",
      status: "ready",
      preflight: preflight({ title: " Title ", author: " Author " }),
    });
  });

  it("marks items with preflight warnings as warning", () => {
    expect(createQueueItem("/tmp/source.pdf", "audiobook", undefined, preflight({ warnings: ["low text"] }))).toMatchObject({
      status: "warning",
    });
  });

  it("blocks a completed preflight with no readable chapters", () => {
    expect(
      createQueueItem(
        "/tmp/invalid.pdf",
        "audiobook",
        undefined,
        preflight({
          chapter_count: 0,
          warnings: ["Source may be malformed or unreadable"],
        }),
      ),
    ).toMatchObject({
      status: "failed",
      error: "No readable chapters found in this source.",
    });
  });

  it("blocks OCR-required PDFs until OCR is available", () => {
    expect(
      createQueueItem(
        "/tmp/scanned.pdf",
        "audiobook",
        undefined,
        preflight({ chapter_count: 0, requires_ocr: true }),
      ),
    ).toMatchObject({
      status: "failed",
      error: "This PDF requires OCR before conversion.",
    });
  });

  it.each([
    ["source_empty", "This file is empty, so there is nothing to convert."],
    ["source_unreadable", "This file could not be read."],
    ["pdf_password_protected", "This PDF is password-protected. Remove the password, then add it again."],
    ["pdf_damaged", "This PDF is damaged and cannot be opened."],
    ["epub_damaged", "This EPUB is damaged and cannot be opened."],
    ["epub_drm_protected", "This EPUB is copy-protected (DRM) and cannot be converted."],
  ])("explains a %s source in the backend's own words", (code, message) => {
    const blocked = preflight({ chapter_count: 0, warnings: [message], blocking_error: { code, message } });

    expect(preflightBlockingReason(blocked)).toBe(message);
    expect(isPreflightConvertible(blocked)).toBe(false);
    expect(createQueueItem("/tmp/blocked", "audiobook", undefined, blocked)).toMatchObject({
      status: "failed",
      error: message,
    });
  });

  it("falls back to the generic reason when the backend gives no message", () => {
    expect(preflightBlockingReason(preflight({ chapter_count: 0, blocking_error: null })))
      .toBe("No readable chapters found in this source.");
    expect(preflightBlockingReason(preflight({ chapter_count: 0, blocking_error: { code: "source_empty", message: " " } })))
      .toBe("No readable chapters found in this source.");
    expect(preflightBlockingReason(preflight({ blocking_error: null }))).toBeNull();
  });
});

describe("estimated audio length", () => {
  it("states the length of a long source", () => {
    expect(estimatedAudioNote(preflight({ estimated_audio_seconds: 12_000 }))).toBe("About 3 hr 20 min of audio");
    expect(estimatedAudioNote(preflight({ estimated_audio_seconds: 3_600 }))).toBe("About 1 hr of audio");
  });

  it("gives pasted text the same length before and after it is queued", () => {
    const characters = 6_327_424;
    // What the backend reports for this many characters at its 18.5 characters a second.
    const queuedRow = estimatedAudioNote(preflight({ estimated_audio_seconds: Math.round(characters / 18.5) }));
    expect(queuedRow).toBe("About 95 hr of audio");
    expect(pastedTextEstimate("x".repeat(characters))).toBe(`6,327,424 characters · ${queuedRow}`);
  });

  it("stays silent for short or unestimated sources", () => {
    expect(estimatedAudioNote(preflight({ estimated_audio_seconds: 3_599 }))).toBe("");
    expect(estimatedAudioNote(preflight({ estimated_audio_seconds: null }))).toBe("");
    expect(estimatedAudioNote(preflight())).toBe("");
    expect(estimatedAudioNote(null)).toBe("");
  });
});
