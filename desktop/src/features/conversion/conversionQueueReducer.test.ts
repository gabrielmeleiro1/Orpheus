import { describe, expect, it } from "vitest";

import type { SourcePreflight } from "../../types/api";
import { createQueueItem } from "./conversionPanelModel";
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
    expect(state.sourceFileLabel).toBe("book.epub selected");
    expect(state.queueStatus).toBe("1 item added");
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
    expect(state.sourceFileLabel).toBe("second.epub selected");
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

  it("edits selected items and bumps revision when the source path changes", () => {
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
    expect(state.itemRevisions[item.id]).toBe(1);
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
    expect(state.preflightStatus).toBe("Preflight completed.");
  });
});
