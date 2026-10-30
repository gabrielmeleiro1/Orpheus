import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import {
  LIBRARY_PAGE_SIZE,
  createLibraryRepository,
} from "./libraryRepository";

function makeBookSummary(id: number): { id: number; title: string } {
  return { id, title: `Book ${id}` };
}

describe("libraryRepository listPage", () => {
  it("requests exactly one fixed-size page at the given offset", async () => {
    const items = [makeBookSummary(1), makeBookSummary(2)];
    const getLibrary = vi.fn().mockResolvedValue({ items, count: items.length });
    const apiClient = { getLibrary } as unknown as ApiClient;

    const repository = createLibraryRepository(apiClient);
    const page = await repository.listPage("dune", "audiobook", 0);

    expect(getLibrary).toHaveBeenCalledTimes(1);
    expect(getLibrary).toHaveBeenCalledWith(
      "dune",
      LIBRARY_PAGE_SIZE,
      { conversionMode: "audiobook", offset: 0 },
      undefined,
    );
    expect(page.items).toEqual(items);
    expect(page.offset).toBe(0);
    expect(page.hasMore).toBe(false);
  });

  it("reports hasMore when the page comes back full and pages from an offset", async () => {
    const fullPage = Array.from({ length: LIBRARY_PAGE_SIZE }, (_, index) =>
      makeBookSummary(index),
    );
    const getLibrary = vi.fn().mockResolvedValue({ items: fullPage, count: fullPage.length });
    const apiClient = { getLibrary } as unknown as ApiClient;

    const repository = createLibraryRepository(apiClient);
    const page = await repository.listPage("", "all", LIBRARY_PAGE_SIZE);

    expect(getLibrary).toHaveBeenCalledTimes(1);
    expect(getLibrary).toHaveBeenCalledWith(
      undefined,
      LIBRARY_PAGE_SIZE,
      { conversionMode: "", offset: LIBRARY_PAGE_SIZE },
      undefined,
    );
    expect(page.items).toHaveLength(LIBRARY_PAGE_SIZE);
    expect(page.offset).toBe(LIBRARY_PAGE_SIZE);
    expect(page.hasMore).toBe(true);
  });

  it("counts the unfiltered collection independently of a filtered page", async () => {
    const filteredItems = [makeBookSummary(1)];
    const getLibrary = vi.fn().mockResolvedValue({ items: filteredItems, count: filteredItems.length });
    const getLibraryCount = vi.fn().mockResolvedValue({ count: 2 });
    const apiClient = { getLibrary, getLibraryCount } as unknown as ApiClient;

    const repository = createLibraryRepository(apiClient);
    const filteredPage = await repository.listPage("missing", "all", 0);
    const indexedCount = await repository.countBooks();

    expect(filteredPage.items).toEqual(filteredItems);
    expect(indexedCount).toBe(2);
    expect(getLibrary).toHaveBeenCalledTimes(1);
    expect(getLibraryCount).toHaveBeenCalledWith(undefined);
  });
});

describe("libraryRepository repair payloads", () => {
  it("sends an empty repair payload so the backend chooses the defaults", async () => {
    const repairConversion = vi.fn().mockResolvedValue({
      job_id: "repair-1",
      status: "running",
      book_id: 42,
      error: null,
    });
    const apiClient = {
      repairConversion,
    } as unknown as ApiClient;

    const repository = createLibraryRepository(apiClient);
    await repository.repairBook(42);

    expect(repairConversion).toHaveBeenCalledTimes(1);
    expect(repairConversion).toHaveBeenCalledWith(42, {}, undefined);
  });
});
