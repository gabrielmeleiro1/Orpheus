import { describe, expect, it } from "vitest";

import type { SourcePreflight } from "../../types/api";
import { createQueueItem, isStartUncertain } from "./conversionPanelModel";
import {
  conversionQueueReducer,
  createConversionQueueInitialState,
  type PreflightStamp,
} from "./conversionQueueReducer";

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

function stamp(itemId: string, sourcePath: string, revision: number): PreflightStamp {
  return { itemId, sourcePath, revision };
}

describe("conversionQueueReducer", () => {
  it("adds queue items and selects the first item when no source is selected", () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    const state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [item],
      selectFirst: true,
    });

    expect(state.queueItems).toEqual([item]);
    expect(state.selectedQueueId).toBe(item.id);
    expect(state.sourcePath).toBe("/tmp/book.epub");
    // The new row is the confirmation; no status sentence.
    expect(state.queueStatus).toBe("");
  });

  it("removes selected items and applies the next item selection", () => {
    const first = createQueueItem("/tmp/first.epub", "audiobook");
    const second = createQueueItem("/tmp/second.epub", "audiobook");
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [first, second],
      selectFirst: true,
    });

    state = conversionQueueReducer(state, { type: "itemRemoved", itemId: first.id });

    expect(state.queueItems.map((item) => item.id)).toEqual([second.id]);
    expect(state.selectedQueueId).toBe(second.id);
    expect(state.sourcePath).toBe("/tmp/second.epub");
  });

  it("reorders queue items without changing selection", () => {
    const first = createQueueItem("/tmp/first.epub", "audiobook");
    const second = createQueueItem("/tmp/second.epub", "audiobook");
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [first, second],
      selectFirst: true,
    });

    state = conversionQueueReducer(state, {
      type: "itemMoved",
      itemId: second.id,
      delta: -1,
    });

    expect(state.queueItems.map((item) => item.id)).toEqual([second.id, first.id]);
    expect(state.selectedQueueId).toBe(first.id);
  });

  it("edits selected items and drops the old preflight when the source path changes", () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook", undefined, preflight());
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [item],
      selectFirst: true,
    });

    state = conversionQueueReducer(state, {
      type: "selectedQueueItemEdited",
      patch: { sourcePath: "/tmp/changed.epub", title: "Changed" },
    });

    expect(state.queueItems[0]).toMatchObject({
      sourcePath: "/tmp/changed.epub",
      label: "changed.epub",
      title: "Changed",
      preflight: null,
      status: "ready",
      error: "",
    });
    expect(state.sourcePath).toBe("/tmp/changed.epub");
  });

  it("clears stale preflight state when the source path is emptied", () => {
    const inspected = createQueueItem(
      "/tmp/book.epub",
      "audiobook",
      undefined,
      preflight({ title: "Inspected" }),
    );
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [inspected],
      selectFirst: true,
    });
    state = {
      ...state,
      preflightStatus: "Preflight completed.",
      previewText: "stale sample",
    };

    state = conversionQueueReducer(state, {
      type: "selectedQueueItemEdited",
      patch: { sourcePath: "" },
    });

    expect(state.queueItems[0]).toMatchObject({
      sourcePath: "",
      preflight: null,
      status: "ready",
      error: "",
    });
    expect(state.sourcePath).toBe("");
    expect(state.preflightStatus).toBe("");
    expect(state.previewText).toBeNull();
  });

  it("detaches an immutable history row when a different manual source is entered", () => {
    const done = {
      ...createQueueItem("/tmp/done.epub", "audiobook"),
      status: "done" as const,
    };
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [done],
      selectFirst: true,
    });

    state = conversionQueueReducer(state, {
      type: "selectedQueueItemEdited",
      patch: { sourcePath: "/tmp/next.epub" },
    });

    expect(state.queueItems[0]).toMatchObject({
      sourcePath: "/tmp/done.epub",
      status: "done",
    });
    expect(state.selectedQueueId).toBeNull();
    expect(state.sourcePath).toBe("/tmp/next.epub");
  });

  it("does not rename queue rows that already converted or are converting", () => {
    // Typing a title for the NEXT paste must not retitle the row of a job
    // that already ran — the finished book keeps its original title, so a
    // renamed row would lie about what was converted.
    const done = { ...createQueueItem("/tmp/done.epub", "audiobook"), title: "Original", status: "done" as const };
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [done],
      selectFirst: true,
    });

    state = conversionQueueReducer(state, {
      type: "selectedQueueItemEdited",
      patch: { title: "Next Paste Title" },
    });

    expect(state.queueItems[0].title).toBe("Original");
    // The shared field state still tracks what the user typed for reuse.
    expect(state.titleOverride).toBe("Next Paste Title");
  });

  it("starts a row over as a new source when its uncertain start's path changes", () => {
    const item = { ...createQueueItem("/tmp/book.epub", "audiobook"), status: "warning" as const,
      error: "The app restarted. Check Library before retrying this item.", startUncertain: true };
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [item],
      selectFirst: true,
    });
    state = conversionQueueReducer(state, {
      type: "selectedQueueItemEdited",
      patch: { sourcePath: "/tmp/other.epub" },
    });
    state = conversionQueueReducer(state, {
      type: "preflightSucceeded",
      stamp: stamp(item.id, "/tmp/other.epub", 1),
      preflight: preflight({ warnings: ["Large file"] }),
      updateSelected: true,
    });

    expect(state.queueItems[0]).toMatchObject({ status: "warning", error: "" });
    expect(isStartUncertain(state.queueItems[0])).toBe(false);
  });

  it("rejects stale preflight results when source revision changed", () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [item],
      selectFirst: true,
    });
    const staleStamp = stamp(item.id, item.sourcePath, 0);

    state = conversionQueueReducer(state, {
      type: "selectedQueueItemEdited",
      patch: { sourcePath: "/tmp/changed.epub" },
    });
    state = conversionQueueReducer(state, {
      type: "preflightSucceeded",
      stamp: staleStamp,
      preflight: preflight({ title: "Old title" }),
      updateSelected: true,
    });

    expect(state.queueItems[0].preflight).toBeNull();
    expect(state.titleOverride).toBe("");
  });

  it("applies fresh preflight metadata and preview text to the selected item", () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [item],
      selectFirst: true,
    });

    state = conversionQueueReducer(state, {
      type: "preflightSucceeded",
      stamp: stamp(item.id, item.sourcePath, 0),
      preflight: preflight({
        title: "Detected",
        author: "Author",
        sample: "This is a long enough sample to become preview text.",
      }),
      updateSelected: true,
    });

    expect(state.queueItems[0]).toMatchObject({
      title: "Detected",
      author: "Author",
      status: "ready",
      error: "",
    });
    expect(state.titleOverride).toBe("Detected");
    expect(state.authorOverride).toBe("Author");
    expect(state.previewText).toBe("This is a long enough sample to become preview text.");
    expect(state.preflightStatus).toBe("");
  });

  it("marks a completed zero-chapter preflight as non-runnable", () => {
    const item = createQueueItem("/tmp/invalid.pdf", "audiobook");
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [item],
      selectFirst: true,
    });

    state = conversionQueueReducer(state, {
      type: "preflightSucceeded",
      stamp: stamp(item.id, item.sourcePath, 0),
      preflight: preflight({
        chapter_count: 0,
        warnings: ["Source may be malformed or unreadable"],
      }),
      updateSelected: true,
    });

    expect(state.queueItems[0]).toMatchObject({
      status: "failed",
      error: "No readable chapters found in this source.",
    });
    // The row carries the error, so the intake does not repeat it.
    expect(state.preflightStatus).toBe("");
  });

  it("blocks an OCR-required PDF with an actionable preflight error", () => {
    const item = createQueueItem("/tmp/scanned.pdf", "audiobook");
    let state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded",
      items: [item],
      selectFirst: true,
    });

    state = conversionQueueReducer(state, {
      type: "preflightSucceeded",
      stamp: stamp(item.id, item.sourcePath, 0),
      preflight: preflight({ chapter_count: 0, requires_ocr: true }),
      updateSelected: true,
    });

    expect(state.queueItems[0]).toMatchObject({
      status: "failed",
      error: "This PDF requires OCR before conversion.",
    });
    expect(state.preflightStatus).toBe("");
  });
});

describe("a row whose book another job finishes", () => {
  function queued(...overrides: Array<Partial<ReturnType<typeof createQueueItem>>>) {
    const items = overrides.map((override, index) => ({
      ...createQueueItem(`/tmp/book-${index}.epub`, "audiobook", undefined, preflight()),
      ...override,
    }));
    const state = conversionQueueReducer(createConversionQueueInitialState(), {
      type: "itemsAdded", items, selectFirst: true,
    });
    return { items, state };
  }

  it("remembers which book a row's own job wrote to", () => {
    const { state } = queued({ status: "converting", jobId: "job-1" });
    const next = conversionQueueReducer(state, { type: "jobBookObserved", jobId: "job-1", bookId: 7, completed: false });

    expect(next.queueItems[0]).toMatchObject({ status: "converting", jobId: "job-1", bookId: 7 });
    // Repeated snapshots of the same job change nothing.
    expect(conversionQueueReducer(next, { type: "jobBookObserved", jobId: "job-1", bookId: 7, completed: false })).toBe(next);
  });

  it("marks a cancelled row done once a repair completes its book", () => {
    const { state } = queued(
      { status: "failed", error: "conversion cancelled", jobId: "job-1", bookId: 7 },
      { status: "failed", error: "conversion cancelled", jobId: "job-2", bookId: 8 },
      { status: "ready" },
    );

    const running = conversionQueueReducer(state, { type: "jobBookObserved", jobId: "repair_7_1", bookId: 7, completed: false });
    expect(running).toBe(state);

    const repaired = conversionQueueReducer(state, { type: "jobBookObserved", jobId: "repair_7_1", bookId: 7, completed: true });
    expect(repaired.queueItems.map((item) => [item.status, item.error])).toEqual([
      ["done", ""],
      ["failed", "conversion cancelled"],
      ["ready", ""],
    ]);
    // The row keeps the job it ran; only its outcome is superseded.
    expect(repaired.queueItems[0].jobId).toBe("job-1");
  });

  it("leaves a row alone when its own job is the one that completed", () => {
    const { state } = queued({ status: "converting", jobId: "job-1", bookId: 7 });

    expect(conversionQueueReducer(state, { type: "jobBookObserved", jobId: "job-1", bookId: 7, completed: true })).toBe(state);
  });

  it("marks a row done when the job it lost sight of is seen to complete", () => {
    const unconfirmed = "Could not confirm the conversion. Check Library before retrying.";
    const { state } = queued(
      { status: "warning", error: unconfirmed, jobId: "job-1" },
      { status: "failed", error: "synthesis failed", jobId: "job-2", bookId: 8 },
    );

    const stillRunning = conversionQueueReducer(state, { type: "jobBookObserved", jobId: "job-1", bookId: 7, completed: false });
    expect(stillRunning.queueItems[0]).toMatchObject({ status: "warning", error: unconfirmed, bookId: 7 });

    const completed = conversionQueueReducer(stillRunning, { type: "jobBookObserved", jobId: "job-1", bookId: 7, completed: true });
    expect(completed.queueItems.map((item) => [item.status, item.error])).toEqual([
      ["done", ""],
      ["failed", "synthesis failed"],
    ]);
  });
});
