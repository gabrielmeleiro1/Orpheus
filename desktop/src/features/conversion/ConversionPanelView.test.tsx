import { createRef } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { ConversionPanelView } from "./ConversionPanelView";
import type { ConversionPanelViewModel } from "./conversionPanelStateTypes";

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
      canRunParallelWarmup: false,
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
      parallelWorkerMode: "auto",
      manualParallelWorkerUpperBound: 1,
      selectedParallelWorkerLimit: 1,
      parallelManualMax: "1",
      parallelWarmupLoading: false,
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
      setParallelWorkerMode: noop,
      setParallelManualMax: noop,
      onBrowse: promiseNoop,
      onBrowserFilesSelected: noop,
      onAddPastedText: promiseNoop,
      onDropZoneDrop: noop,
      onInspectCurrentSource: noop,
      onPreviewVoice: promiseNoop,
      onRecommendVoice: promiseNoop,
      onRunQueue: promiseNoop,
      onStartConversion: promiseNoop,
      onFetchCleanerModels: promiseNoop,
      onRunParallelWarmup: promiseNoop,
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

  it("keeps Runtime setup available from the existing Settings panel", () => {
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

    expect(html).toContain('id="runtime-setup-btn"');
    expect(html).toContain("Runtime setup");
  });

  it("gives every editable conversion control a programmatic label", () => {
    const html = renderToString(<ConversionPanelView {...viewModel()} />);

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
      "parallel-worker-max-input": "Maximum parallel workers",
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
