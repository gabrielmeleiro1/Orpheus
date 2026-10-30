import { describe, expect, it } from "vitest";

import { createQueueItem } from "./conversionPanelModel";
import { INTERRUPTED_CONVERSION_MESSAGE, readStoredQueue, writeStoredQueue } from "./conversionQueuePersistence";

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
    removeItem: (key: string) => void values.delete(key),
  };
}

describe("conversion queue persistence", () => {
  it("round-trips rows, order and selection", () => {
    const storage = memoryStorage();
    const first = { ...createQueueItem("/a.epub", "audiobook"), title: "A", status: "done" as const, jobId: "j1", bookId: 3 };
    const second = { ...createQueueItem("/b.md", "document_audio"), title: "B", status: "failed" as const, error: "No readable chapters found in this source." };
    writeStoredQueue([first, second], second.id, storage);
    expect(readStoredQueue(storage)).toEqual({ items: [first, second], selectedQueueId: second.id });
  });

  it("marks an interrupted conversion and re-inspects rows that never started", () => {
    const storage = memoryStorage();
    const running = { ...createQueueItem("/run.epub", "audiobook"), status: "converting" as const, jobId: "job" };
    const waiting = { ...createQueueItem("/wait.epub", "audiobook"), status: "warning" as const, error: "", preflight: { title: "W" } as never };
    const stopped = { ...createQueueItem("/stop.epub", "audiobook"), status: "warning" as const, jobId: "job-2", error: "Queue cancelled." };
    writeStoredQueue([running, waiting, stopped], null, storage);
    const { items } = readStoredQueue(storage);
    expect(items[0]).toMatchObject({ status: "warning", jobId: "job", error: INTERRUPTED_CONVERSION_MESSAGE });
    expect(items[1]).toMatchObject({ status: "preflighting", preflight: null, error: "" });
    expect(items[2]).toEqual(stopped);
  });

  it.each([
    ["not JSON", "{"],
    ["another version", JSON.stringify({ version: 2, items: [], selectedQueueId: null })],
    ["items that are not a list", JSON.stringify({ version: 1, items: {}, selectedQueueId: null })],
  ])("ignores a stored record that is %s", (_label, raw) => {
    expect(readStoredQueue(memoryStorage({ "orpheus.conversion.queue.v1": raw }))).toEqual({ items: [], selectedQueueId: null });
  });

  it("drops malformed and duplicate rows and a selection that no longer exists", () => {
    const good = createQueueItem("/ok.epub", "audiobook");
    const raw = JSON.stringify({
      version: 1,
      selectedQueueId: "gone",
      items: [good, good, { ...good, id: "x", status: "exploded" }, { ...good, id: "y", sourcePath: " " }, { ...good, id: "z", conversionMode: "radio" }, null],
    });
    const restored = readStoredQueue(memoryStorage({ "orpheus.conversion.queue.v1": raw }));
    expect(restored.items.map((item) => item.id)).toEqual([good.id]);
    expect(restored.selectedQueueId).toBeNull();
  });

  it("keeps waiting rows over old finished ones when the list is long, and clears an empty queue", () => {
    const storage = memoryStorage();
    const finished = Array.from({ length: 150 }, (_, index) => ({ ...createQueueItem(`/done-${index}.epub`, "audiobook"), status: "done" as const, jobId: `j${index}` }));
    const waiting = Array.from({ length: 100 }, (_, index) => createQueueItem(`/wait-${index}.epub`, "audiobook"));
    writeStoredQueue([...finished, ...waiting], null, storage);
    const { items } = readStoredQueue(storage);
    expect(items).toHaveLength(200);
    expect(items.filter((item) => item.status === "done")).toHaveLength(100);
    expect(items.at(-1)?.sourcePath).toBe("/wait-99.epub");
    writeStoredQueue([], null, storage);
    expect(storage.values.size).toBe(0);
  });

  it("survives storage that refuses writes", () => {
    const storage = { ...memoryStorage(), setItem: () => { throw new DOMException("full", "QuotaExceededError"); } };
    expect(() => writeStoredQueue([createQueueItem("/a.epub", "audiobook")], null, storage)).not.toThrow();
  });
});
