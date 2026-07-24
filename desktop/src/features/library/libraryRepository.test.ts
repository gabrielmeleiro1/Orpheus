import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import {
  LIBRARY_PAGE_SIZE,
  createLibraryRepository,
} from "./libraryRepository";

function makeBookSummary(id: number): { id: number; title: string } {
  return { id, title: `Book ${id}` };
}

describe("libraryRepository listBooks pagination", () => {
  it("fetches every page until a short page is returned", async () => {
    const firstPage = Array.from({ length: LIBRARY_PAGE_SIZE }, (_, index) =>
      makeBookSummary(index),
    );
    const secondPage = [makeBookSummary(LIBRARY_PAGE_SIZE)];
    const getLibrary = vi
      .fn()
      .mockResolvedValueOnce({ items: firstPage, count: firstPage.length })
      .mockResolvedValueOnce({ items: secondPage, count: secondPage.length });
    const apiClient = { getLibrary } as unknown as ApiClient;

    const repository = createLibraryRepository(apiClient);
    const response = await repository.listBooks("", "all");

    expect(getLibrary).toHaveBeenCalledTimes(2);
    expect(getLibrary).toHaveBeenNthCalledWith(
      1,
      undefined,
      LIBRARY_PAGE_SIZE,
      { conversionMode: "", offset: 0 },
      undefined,
    );
    expect(getLibrary).toHaveBeenNthCalledWith(
      2,
      undefined,
      LIBRARY_PAGE_SIZE,
      { conversionMode: "", offset: LIBRARY_PAGE_SIZE },
      undefined,
    );
    expect(response.count).toBe(LIBRARY_PAGE_SIZE + 1);
    expect(response.items).toHaveLength(LIBRARY_PAGE_SIZE + 1);
  });

  it("stops after one request when the library fits a single page", async () => {
    const items = [makeBookSummary(1), makeBookSummary(2)];
    const getLibrary = vi.fn().mockResolvedValue({ items, count: items.length });
    const apiClient = { getLibrary } as unknown as ApiClient;

    const repository = createLibraryRepository(apiClient);
    const response = await repository.listBooks("dune", "audiobook");

    expect(getLibrary).toHaveBeenCalledTimes(1);
    expect(getLibrary).toHaveBeenCalledWith(
      "dune",
      LIBRARY_PAGE_SIZE,
      { conversionMode: "audiobook", offset: 0 },
      undefined,
    );
    expect(response.items).toEqual(items);
    expect(response.count).toBe(2);
  });

  it("counts the unfiltered collection independently of a filtered list", async () => {
    const filteredItems = [makeBookSummary(1)];
    const getLibrary = vi.fn().mockResolvedValue({ items: filteredItems, count: filteredItems.length });
    const getLibraryCount = vi.fn().mockResolvedValue({ count: 2 });
    const apiClient = { getLibrary, getLibraryCount } as unknown as ApiClient;

    const repository = createLibraryRepository(apiClient);
    const filteredResponse = await repository.listBooks("missing", "all");
    const indexedCount = await repository.countBooks();

    expect(filteredResponse.items).toEqual(filteredItems);
    expect(indexedCount).toBe(2);
    expect(getLibrary).toHaveBeenCalledTimes(1);
    expect(getLibraryCount).toHaveBeenCalledWith(undefined);
  });

  it("surfaces an explicit error when every safety page is full", async () => {
    const fullPage = Array.from({ length: LIBRARY_PAGE_SIZE }, (_, index) =>
      makeBookSummary(index),
    );
    const getLibrary = vi.fn().mockResolvedValue({
      items: fullPage,
      count: fullPage.length,
    });
    const apiClient = { getLibrary } as unknown as ApiClient;

    const repository = createLibraryRepository(apiClient);

    await expect(repository.listBooks("", "all")).rejects.toThrow(
      "Library result reached the 25000-book safety limit; narrow your search or filter",
    );
    expect(getLibrary).toHaveBeenCalledTimes(50);
  });
});

describe("libraryRepository repair payloads", () => {
  it("does not hardcode narrator voice or manual four-worker repair defaults", async () => {
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
