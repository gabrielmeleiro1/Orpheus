// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { ConversionPanelView } from "./ConversionPanelView";
import type { ConversionPanelViewModel } from "./conversionPanelStateTypes";
import { SettingsPage } from "../setup/SettingsPage";
import { conversionPanelViewModel as viewModel } from "../../test/conversionPanelViewModel";

const conversionCss = readFileSync("src/features/conversion/conversion.css", "utf8");
const settingsCss = readFileSync("src/features/setup/settings.css", "utf8");

describe("ConversionPanelView", () => {
  it("queues a typed path, without the quotes Windows Copy as path adds, and leaves the selected book alone", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    const root = createRoot(container);
    const model = viewModel();
    const onAddSourcePath = vi.fn();
    const syncSelectedQueueEdits = vi.fn();
    model.sourceQueue.sourceInput = "file";
    model.actions.onAddSourcePath = onAddSourcePath;
    model.actions.syncSelectedQueueEdits = syncSelectedQueueEdits;
    try {
      await act(async () => root.render(<ConversionPanelView {...model} />));
      const input = container.querySelector<HTMLInputElement>("#source-path")!;
      const add = container.querySelector<HTMLButtonElement>("#add-source-path-btn")!;
      expect(input.closest(".manual-path")?.querySelector("summary")?.textContent).toBe("Enter a file path");
      expect(add.disabled).toBe(true);
      const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      // Ordinary typing must keep spaces while the user is still editing.
      await act(async () => {
        setValue.call(input, "C:\\My ");
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      expect(input.value).toBe("C:\\My ");
      await act(async () => {
        setValue.call(input, '"C:\\My Books\\A book.epub"');
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      expect(input.value).toBe("C:\\My Books\\A book.epub");
      expect(add.disabled).toBe(false);
      // Enter in the field and the Add button both submit the path form.
      await act(async () => { add.form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
      expect(onAddSourcePath).toHaveBeenCalledExactlyOnceWith("C:\\My Books\\A book.epub");
      expect(input.value).toBe("");
      expect(syncSelectedQueueEdits).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
      vi.unstubAllGlobals();
    }
  });

  it.each([false, true])("shows source intake failures even when a ready source is selected: %s", (hasSource) => {
    const model = viewModel();
    model.sourceQueue.sourcePath = hasSource ? "/books/ready.epub" : "";
    model.sourceQueue.preflightStatus = "Could not open file picker: dialog unavailable";
    model.sourceQueue.preflightCard = hasSource ? {
      title: "Ready book", meta: "EPUB · 1 chapter", author: "", sourceType: "epub",
      sample: "A sample", warnings: [], canConvert: true, blockingReason: null,
    } : null;
    const container = document.createElement("div");
    container.innerHTML = renderToString(<ConversionPanelView {...model} />);
    const status = container.querySelector("#source-intake-status");
    expect(status?.textContent).toBe(model.sourceQueue.preflightStatus);
    expect(status?.closest(".sr-only, [hidden]")).toBeNull();
    expect(status?.getAttribute("role")).toBe("status");
  });

  it.each(["audiobook", "document"] as const)("preserves the viewport when %s sources finish inspection or the panel becomes visible", async (mode) => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const scrollIntoView = vi.spyOn(HTMLElement.prototype, "scrollIntoView");
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const model = viewModel();
    model.shell = { activeTab: "convert", isDocumentMode: mode === "document", hidden: false };
    model.sourceQueue.isDocumentMode = model.shell.isDocumentMode;
    model.sourceQueue.sourcePath = "";
    try {
      await act(async () => root.render(<ConversionPanelView {...model} />));
      model.sourceQueue.sourcePath = "/books/prepared.epub";
      model.sourceQueue.queueCount = 1;
      model.sourceQueue.queueItems = [{
        id: "book", title: "Prepared source", selected: true, statusText: "Ready",
        statusClassName: "queue-item-status", converting: false,
        canMoveUp: false, canMoveDown: false, canRemove: true,
      }];
      await act(async () => root.render(<ConversionPanelView {...model} />));
      model.sourceQueue.preflightCard = {
        title: "Prepared source", meta: "epub / 1 chapter", author: "Author",
        sourceType: "epub", sample: "Prepared sample.", warnings: [],
        canConvert: true, blockingReason: null,
      };
      await act(async () => root.render(<ConversionPanelView {...model} />));
      expect(container.querySelector(".queue-row.is-expanded .queue-item-detail")?.textContent).toContain("Prepared sample.");
      // Subsequent metadata, batch additions and tab changes must also leave
      // navigation to the user, even if inspection finishes on another tab.
      model.shell.hidden = true;
      model.sourceQueue.preflightCard = { ...model.sourceQueue.preflightCard, title: "Updated title" };
      model.sourceQueue.queueCount = 2;
      await act(async () => root.render(<ConversionPanelView {...model} />));
      model.shell.hidden = false;
      await act(async () => root.render(<ConversionPanelView {...model} />));
      expect(scrollIntoView).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
      container.remove();
      vi.restoreAllMocks();
      vi.unstubAllGlobals();
    }
  });

  it("shows the converting book's live ASCII progress in its queue row and opens the details", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    const root = createRoot(container);
    const model = viewModel();
    model.shell = { activeTab: "convert", isDocumentMode: false, hidden: false };
    const openDetails = vi.fn();
    model.actions.onOpenConversionDetails = openDetails;
    model.sourceQueue.queueCount = 2;
    model.sourceQueue.queueItems = [
      { id: "now", title: "Now converting", selected: false, statusText: "Converting", statusClassName: "queue-item-status status-converting",
        converting: true, canMoveUp: false, canMoveDown: false, canRemove: false, progressPct: 42, progressLabel: "Synthesis · 3m left" },
      { id: "next", title: "Up next", selected: true, statusText: "Ready", statusClassName: "queue-item-status status-ready",
        converting: false, canMoveUp: false, canMoveDown: false, canRemove: true },
    ];
    try {
      await act(async () => root.render(<ConversionPanelView {...model} />));
      const rows = container.querySelectorAll(".queue-row");
      expect(rows[0].classList.contains("is-converting")).toBe(true);
      // The progress line already says the row is converting; no badge repeats it.
      expect(rows[0].querySelector(".status-badge")).toBeNull();
      expect(rows[0].querySelector('[role="status"]')?.textContent).toBe("Converting");
      const bar = rows[0].querySelector('[role="progressbar"]');
      expect(bar?.getAttribute("aria-valuenow")).toBe("42");
      // The bar fills its row; without layout it draws its 32-cell fallback.
      expect(bar?.classList.contains("pbar-fit")).toBe(true);
      expect(bar?.querySelector(".pbar-cells-filled")?.textContent).toBe("█".repeat(13));
      // A converting row is not expanded: there is nothing to edit while it runs.
      expect(rows[0].querySelector(".queue-item-detail")).toBeNull();
      expect(rows[0].querySelector(".queue-row-progress-text")?.textContent).toBe("42% · Synthesis · 3m left");
      expect(rows[1].querySelector(".queue-row-progress")).toBeNull();
      await act(async () => rows[0].querySelector<HTMLButtonElement>(".queue-details-btn")!.click());
      expect(openDetails).toHaveBeenCalledOnce();
    } finally {
      await act(async () => root.unmount());
      vi.unstubAllGlobals();
    }
  });

  it("shows a conversion resumed without a queue row at the top of the queue", () => {
    const model = viewModel();
    model.shell = { activeTab: "convert", isDocumentMode: false, hidden: false };
    model.sourceQueue.untrackedJob = { title: "Resumed book", progressPct: 70, progressLabel: "Encoding" };
    model.sourceQueue.sourcePath = "";
    model.sourceQueue.queueItems = [];
    const container = document.createElement("div");
    container.innerHTML = renderToString(<ConversionPanelView {...model} />);
    expect(container.querySelector(".queue-row.is-converting .queue-item-title")?.textContent).toBe("Resumed book");
    expect(container.querySelector(".queue-empty")).toBeNull();
    expect(container.querySelector(".conversion-form.is-empty")).toBeNull();
  });

  it("renders a disabled-start blocker beside the actions with a Settings shortcut", () => {
    const model = viewModel() as ConversionPanelViewModel & {
      conversionActions: ConversionPanelViewModel["conversionActions"] & {
        blockingReason: string;
        showSettingsShortcut: boolean;
      };
    };
    model.capabilities.canStartConversion = false;
    model.capabilities.showStartConversion = false;
    model.conversionActions.blockingReason = "Cloud API requires an API key. Add it in Settings.";
    model.conversionActions.showSettingsShortcut = true;

    const html = renderToString(<ConversionPanelView {...model} />);

    expect(html).toContain('id="conversion-blocking-reason"');
    expect(html).toContain("Cloud API requires an API key. Add it in Settings.");
    expect(html).toContain("Open Settings");
  });

  it.each(["", "   ", "cuda:1"])("renders the cleaner device with its backend default and runtime label (%s)", (device) => {
    const model = viewModel();
    model.shell = { ...model.shell, activeTab: "config" };
    model.cleanerOptions = {
      ...model.cleanerOptions,
      cleanerBackend: "cuda",
      showCleanerCudaControls: true,
      cleanerCudaDevice: device,
      cleanerCudaDevicePlaceholder: "cuda",
    };
    const container = document.createElement("div");
    container.innerHTML = renderToString(<ConversionPanelView {...model} />);
    expect(container.querySelector("#cleaner-cuda-device")?.textContent).toBe(device.trim() ? "CUDA:1" : "CUDA");
  });

  it("labels metadata with the selected queue item in both conversion modes", () => {
    for (const isDocumentMode of [true, false]) {
      const model = viewModel();
      model.shell = { ...model.shell, isDocumentMode, activeTab: "convert" };
      model.sourceQueue = { ...model.sourceQueue, isDocumentMode, queueItems: [{ id: "paste", title: "Selected paste", selected: true } as typeof model.sourceQueue.queueItems[number]] };
      const html = renderToString(<ConversionPanelView {...model} />);
      expect(html.replace(/<[^>]*>/g, "")).toContain("Selected paste");
      expect(html).toContain('aria-label="Selected book"');
      expect(html).toContain('id="title-input"');
      if (isDocumentMode) expect(html).toContain('id="paste-title-input"');
    }
  });

  it.each([true, false])("shows a book's details only inside its selected row (document mode: %s)", (isDocumentMode) => {
    const model = viewModel();
    model.shell = { ...model.shell, isDocumentMode, activeTab: "convert" };
    model.sourceQueue.isDocumentMode = isDocumentMode;
    model.sourceQueue.sourcePath = "";
    for (const queueItems of [[], [{ id: "book", title: "Book", selected: false }]]) {
      model.sourceQueue.queueItems = queueItems as typeof model.sourceQueue.queueItems;
      const html = renderToString(<ConversionPanelView {...model} />);
      expect(html).not.toContain('id="conversion-review-anchor"');
      expect(html).not.toContain('id="title-input"');
      expect(html.includes('class="conversion-form is-empty"')).toBe(queueItems.length === 0);
    }
  });

  it("names each source once: no repeated title header or per-row Ready badge", () => {
    const model = viewModel();
    model.shell = { ...model.shell, activeTab: "convert" };
    model.sourceQueue.queueCount = 2;
    model.sourceQueue.queueItems = [
      { id: "a", title: "The Orchard Letters", meta: "MD · 10 chapters", selected: true, statusText: "Ready",
        statusClassName: "queue-item-status status-ready", converting: false, canMoveUp: false, canMoveDown: true, canRemove: true },
      { id: "b", title: "Broken scan", selected: false, statusText: "Failed", error: "This PDF requires OCR before conversion.",
        statusClassName: "queue-item-status status-failed", converting: false, canMoveUp: true, canMoveDown: false, canRemove: true },
    ];
    model.sourceQueue.preflightCard = {
      title: "The Orchard Letters", meta: "MD · 10 chapters · 1 KB", author: "", sourceType: "md",
      chapterCount: 10, sample: "Dear Ana", warnings: [], canConvert: true, blockingReason: null,
    };
    model.sourceQueue.titleOverride = "The Orchard Letters";
    const container = document.createElement("div");
    container.innerHTML = renderToString(<ConversionPanelView {...model} />);
    const rows = container.querySelectorAll(".queue-row");
    expect(rows[0].querySelector(".queue-item-title")?.textContent).toBe("The Orchard Letters");
    expect([...container.querySelectorAll("h1, h2, h3")].some((heading) => heading.textContent === "The Orchard Letters")).toBe(false);
    expect(rows[0].querySelector(".status-badge")).toBeNull();
    expect(rows[0].querySelector('[role="status"]')?.textContent).toBe("Ready");
    expect(rows[1].querySelector(".status-badge.is-danger")?.textContent).toBe("Failed");
    expect(rows[1].querySelector(".queue-item-meta.is-error")?.textContent).toBe("This PDF requires OCR before conversion.");
    expect(container.textContent).not.toMatch(/preflight/i);
  });

  it("shows the quantization selector only when the engine offers a real choice", () => {
    const base = viewModel();
    expect(renderToString(<ConversionPanelView {...base} />)).not.toContain("tts-quantization-select");

    const withChoice = viewModel({
      advancedOptions: {
        ...base.advancedOptions,
        quantizationOptions: [
          { value: "", label: "default" },
          { value: "bf16", label: "bf16" },
          { value: "8bit", label: "8bit" },
        ],
        showQuantization: true,
      },
    });
    const container = document.createElement("div");
    container.innerHTML = renderToString(<ConversionPanelView {...withChoice} />);
    expect(container.querySelector("#tts-quantization-select button")?.textContent).toBe("Default");
    withChoice.advancedOptions.selectedQuantization = "bf16";
    container.innerHTML = renderToString(<ConversionPanelView {...withChoice} />);
    expect(container.querySelector("#tts-quantization-select button")?.textContent).toBe("BF16");
  });

  it.each([true, false])("renders chapter titles and the count when titles are available: %s", (withTitles) => {
    const model = viewModel();
    model.sourceQueue.queueItems = [{ id: "book", title: "A book", selected: true } as typeof model.sourceQueue.queueItems[number]];
    model.sourceQueue.preflightCard = {
      title: "A book", meta: "EPUB · 15 chapters · 20 KB", author: "Writer", sourceType: "epub",
      chapterCount: 15, sizeLabel: "20 KB",
      ...(withTitles ? { chapterTitles: Array.from({ length: 15 }, (_, index) => `Chapter ${index + 1}`) } : {}),
      sample: "A text sample.", warnings: ["Low text layer"], canConvert: true, blockingReason: null,
    };
    const container = document.createElement("div");
    container.innerHTML = renderToString(<ConversionPanelView {...model} />);
    const chapters = container.querySelector('[aria-label="Chapters"]')!;
    expect(container.querySelector(".preflight-sample")?.textContent).toBe("A text sample.");
    expect(container.querySelector(".preflight-warnings")?.textContent).toContain("Low text layer");
    if (withTitles) {
      expect(Array.from(chapters.querySelectorAll("li"), (item) => item.textContent)).toEqual(
        Array.from({ length: 12 }, (_, index) => `Chapter ${index + 1}`),
      );
      expect(chapters.textContent).toContain("3 more");
      expect(chapters.textContent).not.toContain("Chapter 13");
    } else {
      expect(chapters.querySelector("ol")).toBeNull();
      expect(chapters.textContent).toContain("15 chapters");
      expect(chapters.textContent).not.toContain("more");
    }
  });

  it("summarizes the collapsed advanced settings in the disclosure header", () => {
    const base = viewModel();
    const html = renderToString(
      <ConversionPanelView
        {...viewModel({
          advancedOptions: {
            ...base.advancedOptions,
            speedInput: "1.2",
            selectedParallelWorkerLimit: 3,
          },
        })}
      />,
    );
    expect(html).toContain("Speed 1.2 · 3 chapters at once");
  });

  it.each([
    [2, 1, "Speed 1 · 1 of 2 chapters"],
    [4, 2, "Speed 1 · 2 of 4 chapters"],
    [2, 2, "Speed 1 · 2 chapters at once"],
  ])("summarizes the chapters that will run when %i are requested and %i fit", (requested, usable, summary) => {
    const base = viewModel();
    const html = renderToString(
      <ConversionPanelView
        {...viewModel({
          advancedOptions: {
            ...base.advancedOptions,
            speedInput: "1.0",
            selectedParallelWorkerLimit: requested,
            usableParallelWorkerLimit: usable,
          },
        })}
      />,
    );
    expect(html).toContain(`<span class="disclosure-card-meta">${summary}</span>`);
  });

  it("wraps a long collapsed summary instead of cutting it", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    const style = document.createElement("style");
    style.textContent = `${readFileSync("src/styles/controls.css", "utf8")}\n${conversionCss}`;
    document.head.append(style);
    document.body.append(container);
    const root = createRoot(container);
    try {
      await act(async () => root.render(<ConversionPanelView {...viewModel()} />));
      const meta = container.querySelector<HTMLElement>(".advanced-settings .disclosure-card-meta")!;
      expect(meta.textContent).toContain("Speed");
      const computed = getComputedStyle(meta);
      expect(computed.whiteSpace).toBe("normal");
      expect(computed.textOverflow).not.toBe("ellipsis");
      expect(computed.overflow).not.toBe("hidden");
    } finally {
      await act(async () => root.unmount());
      style.remove();
      container.remove();
      vi.unstubAllGlobals();
    }
  });

  it("does not call a zero-chapter preflight ready", () => {
    const html = renderToString(
      <ConversionPanelView
        {...viewModel({
          sourceQueue: {
            ...viewModel().sourceQueue,
            queueItems: [{ id: "pdf", title: "Invalid PDF", selected: true } as ConversionPanelViewModel["sourceQueue"]["queueItems"][number]],
            preflightCard: {
              title: "Invalid PDF",
              meta: "PDF · 0 chapters · 1 KB",
              author: "",
              sourceType: "pdf",
              warnings: ["Source may be malformed or unreadable"],
              sample: "No readable sample found.",
              canConvert: false,
              blockingReason: "No readable chapters found in this source.",
            },
          },
        })}
      />,
    );

    const container = document.createElement("div");
    container.innerHTML = html;
    expect(container.querySelector(".queue-item-detail")?.textContent).toContain("Can’t convert this source: No readable chapters found in this source.");
  });

  it("gives every editable conversion control a programmatic label", () => {
    // File and paste inputs are alternatives; render each and check both sets.
    const html = (["file", "paste"] as const).map((sourceInput) => {
      const model = viewModel();
      model.sourceQueue.sourceInput = sourceInput;
      model.sourceQueue.queueItems = [{ id: "book", title: "Book", selected: true } as typeof model.sourceQueue.queueItems[number]];
      return renderToString(<ConversionPanelView {...model} />);
    }).join("");

    const labelsById = {
      "paste-text-input": "Pasted document text",
      "paste-title-input": "Paste title",
      "paste-author-input": "Paste author",
      "source-path": "Source path",
      "title-input": "Title override",
      "author-input": "Author override",
      "voice-preview-text": "Voice preview text",
      "speed-input": "Narration speed",
      "parallel-worker-max-input": "Chapters converted in parallel",
    };
    for (const [id, label] of Object.entries(labelsById)) {
      expect(html).toMatch(new RegExp(`id="${id}"[^>]*aria-label="${label}"`));
    }
  });

  it("keeps the two layout panes out of the Tab order", () => {
    // In the narrow layout the panes do not scroll; focusing one outlined a box
    // taller than the window. Their controls are the keyboard stops.
    const host = document.createElement("div");
    host.innerHTML = renderToString(<ConversionPanelView {...viewModel()} />);
    for (const selector of [".conversion-scroll", ".conversion-options-sections"]) {
      const pane = host.querySelector<HTMLElement>(selector)!;
      expect(pane.hasAttribute("tabindex")).toBe(false);
      expect(pane.querySelector("button, input, summary, [tabindex='0']")).not.toBeNull();
    }
  });

  it("advertises every source type accepted by the audiobook picker", () => {
    const fileModel = viewModel();
    fileModel.sourceQueue.sourceInput = "file";
    const html = renderToString(<ConversionPanelView {...fileModel} />);

    expect(html).toContain("PDF, EPUB, TXT or Markdown");
    expect(html).toContain('accept=".pdf,.epub,.txt,.md"');
    const container = document.createElement("div");
    container.innerHTML = html;
    expect(container.querySelector("#drop-zone")).not.toBeNull();
    expect(container.querySelector("#source-path")).not.toBeNull();
    expect(container.querySelector(".paste-panel")).toBeNull();
    const pasteContainer = document.createElement("div");
    pasteContainer.innerHTML = renderToString(<ConversionPanelView {...viewModel()} />);
    expect(pasteContainer.querySelector("#drop-zone")).toBeNull();
    expect(pasteContainer.querySelector(".paste-panel")).not.toBeNull();
    // The preview text is reachable but folded away.
    expect(container.querySelector(".voice-sample:not([open]) #voice-preview-text")).not.toBeNull();
    // A single engine offers no choice, so its picker is hidden.
    expect(container.querySelector("#tts-engine-select")).toBeNull();
  });
});


it.each(["audiobook", "document"] as const)("pins %s actions beside the narration scroll area and keeps the inactive Settings wrapper layout-neutral", async (mode) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  container.className = "main-workspace";
  const style = document.createElement("style");
  style.textContent = `${conversionCss}\n${settingsCss}`;
  document.head.append(style);
  document.body.append(container);
  const root = createRoot(container);
  const model = viewModel({ shell: { activeTab: "convert", hidden: false, isDocumentMode: mode === "document" } });
  const panel = <ConversionPanelView {...model} />;
  try {
    await act(async () => root.render(panel));
    const unwrapped = container.querySelector<HTMLElement>(".conversion-panel")!;
    const originalStyle = getComputedStyle(unwrapped);
    const dimensions = [originalStyle.width, originalStyle.maxWidth, originalStyle.marginLeft, originalStyle.marginBottom];
    await act(async () => root.render(
      <SettingsPage active={false} apiClient={null}>{panel}</SettingsPage>,
    ));
    const wrapped = container.querySelector<HTMLElement>(".conversion-panel")!;
    expect(getComputedStyle(container.querySelector(".settings-content")!).display).toBe("contents");
    const wrappedStyle = getComputedStyle(wrapped);
    expect([wrappedStyle.width, wrappedStyle.maxWidth, wrappedStyle.marginLeft, wrappedStyle.marginBottom]).toEqual(dimensions);
    // The actions sit outside both scrolling panes, so they never scroll away.
    expect(wrapped.querySelector(".conversion-scroll .conversion-actions")).toBeNull();
    expect(wrapped.querySelector(".conversion-options-sections .conversion-actions")).toBeNull();
    expect(wrapped.querySelector(".conversion-narration .conversion-actions")).not.toBeNull();
  } finally {
    await act(async () => root.unmount());
    style.remove();
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  }
});

it("shows a failed row's whole reason instead of cutting it short", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  const style = document.createElement("style");
  style.textContent = conversionCss;
  document.head.append(style);
  document.body.append(container);
  const root = createRoot(container);
  const reason = "This PDF is password-protected. Open it in a PDF viewer, save a copy without the password, and add that copy.";
  const row = { statusText: "Ready", statusClassName: "queue-item-status status-ready", converting: false, selected: false,
    canMoveUp: false, canMoveDown: false, canRemove: true };
  const base = viewModel();
  try {
    await act(async () => root.render(<ConversionPanelView {...viewModel({ sourceQueue: { ...base.sourceQueue, queueCount: 2, queueItems: [
      { ...row, id: "locked", title: "Locked", statusText: "Failed", statusClassName: "queue-item-status status-failed", error: reason },
      { ...row, id: "fine", title: "Fine", meta: "MD · 2 chapters" },
    ] } })} />));
    const [failed, fine] = [...container.querySelectorAll<HTMLElement>(".queue-item-meta")];
    expect(failed.textContent).toBe(reason);
    expect(getComputedStyle(failed).whiteSpace).toBe("normal");
    expect(getComputedStyle(failed).overflowWrap).toBe("anywhere");
    // Ordinary details stay on one line.
    expect(getComputedStyle(fine).whiteSpace).toBe("nowrap");
  } finally {
    await act(async () => root.unmount());
    style.remove();
    container.remove();
    vi.unstubAllGlobals();
  }
});

describe("ConversionPanelView error box", () => {
  it("makes a long error reachable from the keyboard only while there is one", () => {
    const withError = viewModel();
    withError.sourceQueue.formError = `API request failed (500): ${"Traceback line. ".repeat(140)}`;
    const host = document.createElement("div");
    host.innerHTML = renderToString(<ConversionPanelView {...withError} />);
    expect(host.querySelector("#form-error")?.getAttribute("tabindex")).toBe("0");
    host.innerHTML = renderToString(<ConversionPanelView {...viewModel()} />);
    expect(host.querySelector("#form-error")?.hasAttribute("tabindex")).toBe(false);
  });
});
