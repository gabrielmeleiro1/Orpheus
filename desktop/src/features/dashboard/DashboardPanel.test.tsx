// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";

import type { UseLibraryResult } from "../library/useLibrary";
import { DashboardPanel } from "./DashboardPanel";

describe("DashboardPanel library count", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("labels paused playback truthfully and dismisses a completed conversion", async () => {
    const props = { hidden: false, library: { items: [], indexedCount: 0, selectedBookDetail: null } as unknown as UseLibraryResult, connectionState: "connected" as const, health: null, statusMessage: "", uiState: "completed" as const, progressPct: 100, etaText: "", stages: [], currentChapterLabel: "One", playerTitle: "Paused", playing: false, elapsedSec: 0, durationSec: 20 };
    root = createRoot(container);
    await act(async () => root.render(<DashboardPanel {...props} />));
    expect(container.querySelector(".now-playing-main .card-eyebrow")?.textContent).toBe("Continue listening");
    expect(container.querySelector('[aria-label="Conversion progress"]')?.classList.contains("pbar-done")).toBe(true);
    expect(container.querySelector(".conversion-card .dashboard-card-action")?.classList.contains("is-complete")).toBe(true);
    await act(async () => root.render(<DashboardPanel {...props} playing />));
    expect(container.querySelector(".now-playing-main .card-eyebrow")?.textContent).toBe("Now playing");
    await act(async () => (container.querySelector('[aria-label="Dismiss conversion"]') as HTMLButtonElement).click());
    expect(container.querySelector(".conversion-card")).toBeNull();
    await act(async () => root.render(<DashboardPanel {...props} uiState="running" />));
    expect(container.querySelector(".conversion-card")).not.toBeNull();
    expect(container.querySelector('[aria-label="Conversion progress"]')?.classList.contains("pbar-active")).toBe(true);
    expect(container.querySelector(".conversion-card .dashboard-card-action")?.classList.contains("is-complete")).toBe(false);
    await act(async () => root.render(<DashboardPanel {...props} uiState="failed" />));
    expect(container.querySelector('[aria-label="Conversion progress"]')?.classList.contains("pbar-danger")).toBe(true);
    expect(container.querySelector(".conversion-card .dashboard-card-action")?.classList.contains("is-error")).toBe(true);
  });

  it("checks the completed book by id even when filtered out, then hides it after deletion", async () => {
    const getLibraryBook = vi.fn(async () => ({ id: 42 }));
    const apiClient = { getActiveConversion: vi.fn(async () => ({ job: { id: "job", status: "completed", book_id: 42 } })), getLibraryBook } as unknown as ApiClient;
    const props = { apiClient, hidden: false, library: { items: [], indexedCount: 1, selectedBookDetail: null } as unknown as UseLibraryResult, connectionState: "connected" as const, health: null, statusMessage: "", uiState: "completed" as const, progressPct: 100, etaText: "", stages: [], currentChapterLabel: "", playerTitle: "", playing: false, elapsedSec: 0, durationSec: 0 };
    root = createRoot(container);
    await act(async () => root.render(<DashboardPanel {...props} />));
    expect(getLibraryBook).toHaveBeenCalledWith(42);
    expect(container.querySelector(".conversion-card")).not.toBeNull();
    getLibraryBook.mockRejectedValueOnce({ status: 404 });
    await act(async () => root.render(<DashboardPanel {...props} library={{ ...props.library, indexedCount: 0 }} />));
    expect(container.querySelector(".conversion-card")).toBeNull();
  });

  it("uses indexedCount when visible library items are filtered", async () => {
    const library = {
      items: [
        { id: 1, title: "Visible match", total_chapters: 4, completed_chapters: 4 },
        { id: 2, title: "Still converting", total_chapters: 4, completed_chapters: 2 },
      ],
      indexedCount: 4,
      selectedBookDetail: null,
    } as unknown as UseLibraryResult;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <DashboardPanel
          hidden={false}
          library={library}
          connectionState="connected"
          health={null}
          statusMessage="ready"
          uiState="idle"
          progressPct={0}
          etaText=""
          stages={[]}
          currentChapterLabel=""
          playerTitle=""
          playing={false}
          elapsedSec={0}
          durationSec={0}
        />,
      );
    });

    expect(container.querySelector(".recent-section .section-title")?.textContent).toContain("4 books");
    expect(container.querySelector('[aria-label="Visible match completion"]')).toBeNull();
    expect(container.querySelector('[aria-label="Still converting completion"]')?.getAttribute("aria-valuenow")).toBe("50");
  });

  it("shows a single Status tile and no ETA placeholder when ETA is empty", async () => {
    const library = {
      items: [],
      indexedCount: 0,
      selectedBookDetail: null,
    } as unknown as UseLibraryResult;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <DashboardPanel
          hidden={false}
          library={library}
          connectionState="connected"
          health={null}
          statusMessage="ready"
          uiState="running"
          progressPct={50}
          etaText=""
          stages={[]}
          currentChapterLabel=""
          playerTitle=""
          playing={false}
          elapsedSec={0}
          durationSec={0}
        />,
      );
    });

    const metricRow = container.querySelector(".metric-row-flow");
    expect(metricRow?.querySelectorAll(".metric").length).toBe(1);
    expect(container.textContent).not.toContain("ETA");
    expect(metricRow?.querySelector(".metric-label")?.textContent).toBe("Status");
  });

  it("keeps conversion progress exposed to assistive technology", async () => {
    const library = {
      items: [],
      indexedCount: 0,
      selectedBookDetail: null,
    } as unknown as UseLibraryResult;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <DashboardPanel
          hidden={false}
          library={library}
          connectionState="connected"
          health={null}
          statusMessage="running"
          uiState="running"
          progressPct={50}
          etaText=""
          stages={[]}
          currentChapterLabel=""
          playerTitle=""
          playing={false}
          elapsedSec={0}
          durationSec={0}
        />,
      );
    });

    const progress = container.querySelector('[aria-label="Conversion progress"]');
    expect(progress).not.toBeNull();
    expect(progress?.closest('[aria-hidden="true"]')).toBeNull();
  });

  it("offers the library without duplicating the shell conversion action", async () => {
    const library = {
      items: [],
      indexedCount: 0,
      selectedBookDetail: null,
    } as unknown as UseLibraryResult;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <DashboardPanel
          hidden={false}
          library={library}
          connectionState="connected"
          health={null}
          statusMessage="ready"
          uiState="idle"
          progressPct={0}
          etaText=""
          stages={[]}
          currentChapterLabel=""
          playerTitle=""
          playing={false}
          elapsedSec={0}
          durationSec={0}
          onOpenLibrary={() => undefined}
        />,
      );
    });

    expect(container.textContent).toContain("Open library");
    expect(container.querySelector(".now-playing-main .card-eyebrow")?.textContent).toBe("Listening");
    expect(container.querySelector(".now-playing-title")?.textContent).toBe("Nothing playing");
    expect([...container.querySelectorAll("button")].some(button => button.textContent === "Convert a source")).toBe(false);
  });
});
