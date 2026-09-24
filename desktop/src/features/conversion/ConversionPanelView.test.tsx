// @vitest-environment happy-dom

import { act, createRef } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { ConversionPanelView } from "./ConversionPanelView";
import type { ConversionPanelViewModel } from "./conversionPanelStateTypes";
import { SettingsPage } from "../setup/SettingsPage";

const conversionCss = readFileSync("src/features/conversion/conversion.css", "utf8");
const settingsCss = readFileSync("src/features/setup/settings.css", "utf8");

function viewModel(overrides: Partial<ConversionPanelViewModel> = {}): ConversionPanelViewModel {
  const noop = vi.fn();
  const promiseNoop = vi.fn(async () => undefined);
  return {
    shell: {
      activeTab: "document",
      hidden: false,
      isDocumentMode: true,
    },
    capabilities: {
      canAddPastedText: false,
      canFetchCleanerModels: false,
      canInspectSource: true,
      canPreviewVoice: false,
      canRecommendVoice: false,
      canRunQueue: false,
      canStartConversion: true,
      canCancelJob: false,
      canRefreshJob: true,
      showRunQueue: false,
      showStartConversion: true,
    },
    sourceQueue: {
      isDocumentMode: true,
      dragging: false,
      sourceFileName: "",
      sourceFileDir: "",
      pastedText: "",
      pastedTitle: "",
      pastedAuthor: "",
      pasteBusy: false,
      pasteStatus: "Idle",
      queueItems: [],
      queueCount: 0,
      queueRunning: false,
      queueStatus: "",
      formError: "",
      sourcePath: "/tmp/book.pdf",
      sourcePathPlaceholder: "/absolute/path/to/document.md",
      titleOverride: "",
      authorOverride: "",
      preflightCard: null,
      preflightStatus: "",
      browserFileInputRef: createRef<HTMLInputElement>(),
      browserFileAccept: ".pdf,.epub,.txt,.md",
    },
    ttsOptions: {
      selectedEngine: "",
      engineOptions: [],
      engineDisabled: true,
      voiceFilter: "",
      selectedVoice: "",
      filteredVoiceOptions: [],
      voiceSelectDisabled: true,
      voiceSuggestBusy: false,
      voiceSuggestStatus: "",
      previewText: "",
      previewBusy: false,
      previewStatus: "",
      previewError: false,
      previewAudioRef: createRef<HTMLAudioElement>(),
    },
    cleanerOptions: {
      selectedBackendDisplayName: "",
      cleanerBackend: "",
      cleanerBackendOptions: [],
      cleanerRuntimeStatus: "",
      runtimeStatusClass: "field-hint",
      cleanerModel: "",
      cleanerModelOptions: [],
      showCleanerCudaControls: false,
      cleanerCudaDevice: "",
      cleanerCudaDevicePlaceholder: "cuda:0",
      cleanerCudaQuantization: "",
      cleanerCudaQuantizationOptions: [],
      requiresApiKey: false,
      cleanerApiKey: "",
      openAiModelFetchLoading: false,
      openAiModelFetchError: false,
      openAiModelFetchStatus: "",
    },
    advancedOptions: {
      speedInput: "1",
      selectedQuantization: "",
      quantizationOptions: [],
      showQuantization: false,
      manualParallelWorkerUpperBound: 1,
      selectedParallelWorkerLimit: 1,
      parallelManualMax: "1",
      parallelWorkerError: false,
      parallelWorkerStatus: "",
      parallelWorkerDetail: "",
    },
    conversionActions: {
      hasActiveJob: false,
      blockingReason: null,
      showSettingsShortcut: false,
      startLabel: "Start conversion",
      cancelLabel: "Cancel",
      isStarting: false,
    },
    actions: {
      onOpenSettings: noop,
      onOpenRuntimeSetup: noop,
      setSourcePath: noop,
      setTitleOverride: noop,
      setAuthorOverride: noop,
      setDragging: noop,
      setPastedText: noop,
      setPastedTitle: noop,
      setPastedAuthor: noop,
      setPreviewText: noop,
      setCleanerBackend: noop,
      setCleanerModel: noop,
      setCleanerCudaDevice: noop,
      setCleanerCudaQuantization: noop,
      setCleanerApiKey: noop,
      setSelectedEngine: noop,
      setVoiceFilter: noop,
      setSelectedVoice: noop,
      setSpeed: noop,
      setSelectedQuantization: noop,
      setParallelManualMax: noop,
      onBrowse: promiseNoop,
      onBrowserFilesSelected: noop,
      onAddPastedText: vi.fn(async () => null),
      onDropZoneDrop: noop,
      onInspectCurrentSource: noop,
      onPreviewVoice: promiseNoop,
      onRecommendVoice: promiseNoop,
      onRunQueue: promiseNoop,
      onStartConversion: promiseNoop,
      onFetchCleanerModels: promiseNoop,
      onRefreshJob: promiseNoop,
      onCancelJob: promiseNoop,
      clearFormError: noop,
      selectQueueItem: noop,
      moveQueueItem: noop,
      removeQueueItem: noop,
      syncSelectedQueueEdits: noop,
    },
    ...overrides,
  };
}

describe("ConversionPanelView", () => {
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
    expect(container.querySelector<HTMLInputElement>("#cleaner-cuda-device-input")?.value).toBe(device.trim() ? "CUDA:1" : "CUDA");
  });

  it("uses one text cleaning heading and leaves model management to Settings", () => {
    const html = renderToString(
      <ConversionPanelView
        {...viewModel({
          shell: {
            activeTab: "config",
            hidden: false,
            isDocumentMode: false,
          },
        })}
      />,
    );

    expect(html).not.toContain('id="runtime-setup-btn"');
    expect(html).not.toContain("Runtime setup");
    expect(html).toContain("Text cleaning");
  });

  it("labels metadata with the selected queue item in both conversion modes", () => {
    for (const isDocumentMode of [true, false]) {
      const model = viewModel();
      model.shell = { ...model.shell, isDocumentMode, activeTab: isDocumentMode ? "document" : "convert" };
      model.sourceQueue = { ...model.sourceQueue, isDocumentMode, queueItems: [{ id: "paste", title: "Selected paste", selected: true } as typeof model.sourceQueue.queueItems[number]] };
      const html = renderToString(<ConversionPanelView {...model} />);
      expect(html.replace(/<[^>]*>/g, "")).toContain("Details — Selected paste");
      expect(html).not.toContain("Pasted text uses its own fields above");
      expect(html).toContain('id="title-input"');
      if (isDocumentMode) expect(html).toContain('id="paste-title-input"');
    }
  });

  it.each([true, false])("hides Details until a queue item is selected (document mode: %s)", (isDocumentMode) => {
    const model = viewModel();
    model.shell = { ...model.shell, isDocumentMode, activeTab: isDocumentMode ? "document" : "convert" };
    model.sourceQueue.isDocumentMode = isDocumentMode;
    for (const queueItems of [[], [{ id: "book", title: "Book", selected: false }]]) {
      model.sourceQueue.queueItems = queueItems as typeof model.sourceQueue.queueItems;
      const html = renderToString(<ConversionPanelView {...model} />);
      expect(html).not.toContain('id="conversion-review-anchor"');
      expect(html).not.toContain('id="title-input"');
      expect(html).not.toContain("Edit title &amp; author");
    }
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
    expect(renderToString(<ConversionPanelView {...withChoice} />)).toContain("tts-quantization-select");
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

  it("does not call a zero-chapter preflight ready", () => {
    const html = renderToString(
      <ConversionPanelView
        {...viewModel({
          sourceQueue: {
            ...viewModel().sourceQueue,
            queueItems: [{ id: "pdf", title: "Invalid PDF", selected: true } as ConversionPanelViewModel["sourceQueue"]["queueItems"][number]],
            preflightCard: {
              title: "Invalid PDF",
              meta: "pdf / 0 chapters / 1 KB",
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

    expect(html).toContain("Source cannot be converted — No readable chapters found in this source.");
    expect(html).not.toContain("Source ready — use Start conversion below.");
  });

  it("gives every editable conversion control a programmatic label", () => {
    const model = viewModel();
    model.sourceQueue.queueItems = [{ id: "book", title: "Book", selected: true } as typeof model.sourceQueue.queueItems[number]];
    const html = renderToString(<ConversionPanelView {...model} />);

    const labelsById = {
      "paste-text-input": "Pasted document text",
      "paste-title-input": "Paste title",
      "paste-author-input": "Paste author",
      "source-path": "Source path",
      "title-input": "Title override",
      "author-input": "Author override",
      "voice-search": "Search voices",
      "voice-preview-text": "Voice preview text",
      "speed-input": "Narration speed",
      "parallel-worker-max-input": "Chapters converted in parallel",
    };
    for (const [id, label] of Object.entries(labelsById)) {
      expect(html).toMatch(new RegExp(`id="${id}"[^>]*aria-label="${label}"`));
    }
  });

  it("advertises every source type accepted by the audiobook picker", () => {
    const html = renderToString(<ConversionPanelView {...viewModel()} />);

    expect(html).toContain(".pdf, .epub, .txt, or .md");
  });
});


it.each(["convert", "document"] as const)("reserves the %s action height and keeps the inactive Settings wrapper layout-neutral", async (activeTab) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  let height = 80;
  let resize = () => {};
  const disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { resize = callback; }
    observe() {}
    disconnect = disconnect;
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({ height }) as DOMRect);
  const container = document.createElement("div");
  container.className = "main-workspace";
  const style = document.createElement("style");
  style.textContent = `${conversionCss}\n${settingsCss}`;
  document.head.append(style);
  document.body.append(container);
  const root = createRoot(container);
  const model = viewModel({ shell: { activeTab, hidden: false, isDocumentMode: activeTab === "document" } });
  const panel = <ConversionPanelView {...model} />;
  try {
    await act(async () => root.render(panel));
    const unwrapped = container.querySelector<HTMLElement>(".conversion-panel")!;
    const originalStyle = getComputedStyle(unwrapped);
    const dimensions = [originalStyle.width, originalStyle.maxWidth, originalStyle.marginLeft, originalStyle.marginBottom];
    await act(async () => root.render(
      <SettingsPage active={false} apiClient={null} status={null} onManageModels={() => {}}>{panel}</SettingsPage>,
    ));
    const wrapped = container.querySelector<HTMLElement>(".conversion-panel")!;
    expect(getComputedStyle(container.querySelector(".settings-content")!).display).toBe("contents");
    const wrappedStyle = getComputedStyle(wrapped);
    expect([wrappedStyle.width, wrappedStyle.maxWidth, wrappedStyle.marginLeft, wrappedStyle.marginBottom]).toEqual(dimensions);
    expect(wrapped.style.getPropertyValue("--conversion-actions-height")).toBe("80px");
    height = 128;
    await act(async () => resize());
    expect(wrapped.style.getPropertyValue("--conversion-actions-height")).toBe("128px");
    const scroll = getComputedStyle(container.querySelector(".conversion-scroll")!);
    expect(scroll.paddingBottom).toContain("128px");
    expect(wrapped.lastElementChild?.className).toBe("conversion-actions");
  } finally {
    await act(async () => root.unmount());
    expect(disconnect).toHaveBeenCalled();
    style.remove();
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  }
});
