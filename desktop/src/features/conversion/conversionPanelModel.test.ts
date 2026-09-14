import { describe, expect, it, vi } from "vitest";

import type { SourcePreflight } from "../../types/api";
import { createQueueItem } from "./conversionPanelModel";

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
    vi.spyOn(Date, "now").mockReturnValue(123);
    vi.spyOn(Math, "random").mockReturnValue(0.5);

    expect(createQueueItem(" /tmp/source.epub ", "audiobook")).toEqual({
      id: "123-i",
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

    vi.restoreAllMocks();
  });

  it("uses label and preflight metadata when present", () => {
    vi.spyOn(Date, "now").mockReturnValue(456);
    vi.spyOn(Math, "random").mockReturnValue(0.25);

    expect(
      createQueueItem(
        " /tmp/source.pdf ",
        "document_audio",
        " Source Label ",
        preflight({ title: " Title ", author: " Author " }),
      ),
    ).toMatchObject({
      id: "456-9",
      conversionMode: "document_audio",
      sourcePath: "/tmp/source.pdf",
      label: "Source Label",
      title: "Title",
      author: "Author",
      status: "ready",
      preflight: preflight({ title: " Title ", author: " Author " }),
    });

    vi.restoreAllMocks();
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
});
