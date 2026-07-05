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
    },
    sourceQueue: {
      isDocumentMode: true,
      dragging: false,
      sourceFileLabel: "no source selected",
      pasteTitle: "",
      pastedText: "",
      pasteBusy: false,
      pasteStatus: "paste idle",
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
    },
    conversionActions: {
      backendStatus: "ready",
      healthStatus: "status=ok",
      jobStatus: "no active conversion",
      detailStatus: "idle",
      startLabel: "start conversion",
      cancelLabel: "cancel",
      isStarting: false,
    },
    actions: {
      setSourcePath: noop,
      setTitleOverride: noop,
      setAuthorOverride: noop,
      setDragging: noop,
      setPasteTitle: noop,
      setPastedText: noop,
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
  it("renders from a plain view model with preserved DOM hooks", () => {
    const html = renderToString(<ConversionPanelView {...viewModel()} />);

    expect(html).toContain('id="tab-document"');
    expect(html).toContain('id="start-btn"');
    expect(html).toContain('accept=".pdf,.epub,.txt,.md"');
    expect(html).toContain("start conversion");
  });
});
