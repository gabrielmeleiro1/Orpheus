import { useCallback, useEffect, type Dispatch, type SetStateAction } from "react";

import type { ConversionMode } from "../../types/api";
import { resolveConversionPrimaryAction } from "./conversionPrimaryAction";
import { cleanerBackendUsesCudaFields } from "./cleanerBackendHelpers";
import { compactSample, describeSourceDisplay, splitPath } from "./conversionPanelHelpers";
import { useConversionOptions } from "./useConversionOptions";
import { useConversionPreview } from "./useConversionPreview";
import { useConversionQueueController } from "./useConversionQueueController";
import { useConversionQueueRunner } from "./useConversionQueueRunner";
import { useConversionSourceQueue } from "./useConversionSourceQueue";
import { useConversionSubmitPayload } from "./useConversionSubmitPayload";
import type {
  ConversionPanelViewModel,
  QueueEditPatch,
  UseConversionPanelStateArgs,
} from "./conversionPanelStateTypes";
import type { ConversionQueueItem } from "./conversionPanelModel";

export type {
  ConversionPanelViewModel,
  UseConversionPanelStateArgs,
} from "./conversionPanelStateTypes";

const BROWSER_SOURCE_ACCEPT = ".pdf,.epub,.txt,.md";

export interface ConversionBlockerInput {
  apiClientAvailable: boolean;
  sourcePath: string;
  hasPastedText: boolean;
  selectedEngine: string;
  selectedVoice: string;
  cleanerRuntimeError: boolean;
  cleanerRequiresApiKey: boolean;
  cleanerApiKey: string;
  cleanerDisplayName: string;
  cleanerModelOptionsCount: number;
  cleanerModel: string;
}

export interface ConversionBlocker {
  blockingReason: string | null;
  showSettingsShortcut: boolean;
}

export function resolveStartDisabled(input: {
  blockingReason: string | null;
  queueRunning: boolean;
  isStarting: boolean;
  hasActiveJob: boolean;
  pasteBusy: boolean;
  sourcelessPasteReady: boolean;
}): boolean {
  // hasActiveJob closes two defects at once: a second "Start conversion" click
  // firing another POST while the first job is still in flight, and the button
  // looking actionable (but being a no-op) while a single queued job converts.
  return input.blockingReason !== null
    || input.queueRunning
    || input.isStarting
    || input.hasActiveJob
    || (input.pasteBusy && input.sourcelessPasteReady);
}

export function canRunConversionQueue(input: {
  apiClientAvailable: boolean;
  queueRunning: boolean;
  hasRunnableItems: boolean;
  blocker: ConversionBlocker;
}): boolean {
  return input.apiClientAvailable
    && !input.queueRunning
    && input.hasRunnableItems
    && input.blocker.blockingReason === null;
}

export function resolveConversionBlocker(input: ConversionBlockerInput): ConversionBlocker {
  if (!input.apiClientAvailable) {
    return { blockingReason: "Backend connection is unavailable.", showSettingsShortcut: false };
  }
  // Un-queued pasted text counts as a source: starting will queue it first.
  if (!input.sourcePath.trim() && !input.hasPastedText) {
    return { blockingReason: "Select a source before starting conversion.", showSettingsShortcut: false };
  }
  if (!input.selectedEngine) {
    return { blockingReason: "Select a TTS engine before starting conversion.", showSettingsShortcut: false };
  }
  if (!input.selectedVoice) {
    return { blockingReason: "Select a voice before starting conversion.", showSettingsShortcut: false };
  }
  if (input.cleanerRuntimeError) {
    return { blockingReason: "The selected cleaner runtime is unavailable.", showSettingsShortcut: false };
  }
  if (input.cleanerRequiresApiKey && !input.cleanerApiKey.trim()) {
    const cleanerName = input.cleanerDisplayName.trim() || "The selected cleaner";
    return {
      blockingReason: cleanerName + " requires an API key. Add it in Settings.",
      showSettingsShortcut: true,
    };
  }
  // API-backed cleaners list models live from the provider; until the pasted
  // key has produced one there is nothing valid to convert with.
  if (input.cleanerRequiresApiKey && !input.cleanerModel) {
    const cleanerName = input.cleanerDisplayName.trim() || "cleaner";
    return {
      blockingReason: "No " + cleanerName + " models loaded yet. Check the API key in Settings.",
      showSettingsShortcut: true,
    };
  }
  if (input.cleanerModelOptionsCount > 0 && !input.cleanerModel) {
    return { blockingReason: "Select a cleaner model before starting conversion.", showSettingsShortcut: false };
  }
  return { blockingReason: null, showSettingsShortcut: false };
}

const QUEUE_STATUS_LABELS: Record<ConversionQueueItem["status"], string> = {
  ready: "Ready",
  preflighting: "Inspecting…",
  warning: "Warning",
  converting: "Converting…",
  done: "Done",
  failed: "Failed",
};

function queueStatusText(item: ConversionQueueItem): string {
  const label = QUEUE_STATUS_LABELS[item.status] ?? item.status;
  return item.error ? `${label}: ${item.error}` : label;
}

function preflightMeta(preflight: NonNullable<ConversionQueueItem["preflight"]>): string {
  const count = Number(preflight.chapter_count || 0);
  const chapterText = count === 1 ? "1 chapter" : `${count} chapters`;
  const sizeText =
    preflight.size_bytes > 1024 * 1024
      ? `${(preflight.size_bytes / (1024 * 1024)).toFixed(1)} MB`
      : `${Math.max(1, Math.round(preflight.size_bytes / 1024))} KB`;
  return `${preflight.source_type || "source"} / ${chapterText} / ${sizeText}`;
}

export function useConversionPanelState({
  activeTab,
  hidden,
  apiClient,
  conversionJob,
  onOpenSettings,
  onOpenRuntimeSetup,
  startCommandRef,
}: UseConversionPanelStateArgs): ConversionPanelViewModel {
  const isDocumentMode = activeTab === "document";
  const conversionMode: ConversionMode = isDocumentMode ? "document_audio" : "audiobook";

  const optionState = useConversionOptions({
    apiClient,
    conversionMode,
  });
  const sourcePathPlaceholder = isDocumentMode
    ? "/absolute/path/to/document.md"
    : "/absolute/path/to/book.epub";

  const queueController = useConversionQueueController({
    apiClient,
  });

  const sourceQueue = useConversionSourceQueue({
    apiClient,
    conversionMode,
    conversionJob,
    queueController,
  });

  const preview = useConversionPreview({
    apiClient,
    optionState,
    sourcePath: queueController.sourcePath,
    selectedPreflight: queueController.selectedQueueItem?.preflight ?? null,
    sampleText: queueController.previewText,
  });

  const selectedPreflight = queueController.selectedQueueItem?.preflight ?? null;
  const payload = useConversionSubmitPayload({
    optionState,
    conversionMode,
    sourcePath: queueController.sourcePath,
    titleOverride: queueController.titleOverride,
    authorOverride: queueController.authorOverride,
  });
  const queueRunner = useConversionQueueRunner({
    apiClient,
    conversionJob,
    queueItems: queueController.queueItems,
    selectedQueueItem: queueController.selectedQueueItem,
    buildStartPayload: payload.buildStartPayload,
    updateQueueItem: queueController.updateQueueItem,
    syncSelectedQueueEdits: queueController.syncSelectedQueueEdits,
    setQueueStatus: queueController.setQueueStatus,
  });

  const requiresCleanerApiKey = optionState.selectedCleanerBackend?.requires_api_key ?? false;
  const hasPastedText = isDocumentMode && Boolean(sourceQueue.pastedText.trim());

  const conversionBlocker = resolveConversionBlocker({
    apiClientAvailable: Boolean(apiClient),
    sourcePath: queueController.sourcePath,
    hasPastedText,
    selectedEngine: optionState.selectedEngine,
    selectedVoice: optionState.selectedVoice,
    cleanerRuntimeError: optionState.cleanerRuntimeError,
    cleanerRequiresApiKey: requiresCleanerApiKey,
    cleanerApiKey: optionState.cleanerApiKey,
    cleanerDisplayName: optionState.selectedCleanerBackend?.display_name ?? "",
    cleanerModelOptionsCount: optionState.cleanerModelOptions.length,
    cleanerModel: optionState.cleanerModel,
  });

  const sourcelessPasteReady = hasPastedText && !queueController.sourcePath.trim();
  const startDisabled = resolveStartDisabled({
    blockingReason: conversionBlocker.blockingReason,
    queueRunning: queueRunner.queueRunning,
    isStarting: conversionJob.isStarting,
    hasActiveJob: Boolean(conversionJob.activeJobId),
    pasteBusy: sourceQueue.pasteBusy,
    sourcelessPasteReady,
  });

  const { onAddPastedText } = sourceQueue;
  const { onStartConversion: startQueueItemConversion } = queueRunner;
  const onStartConversion = useCallback(async () => {
    // Pasted text that was never queued is still a startable source: queue it
    // now and convert the resulting item directly.
    if (sourcelessPasteReady) {
      const pastedItem = await onAddPastedText();
      if (pastedItem) {
        await startQueueItemConversion(pastedItem);
      }
      return;
    }
    await startQueueItemConversion();
  }, [onAddPastedText, sourcelessPasteReady, startQueueItemConversion]);

  const canRunQueue = canRunConversionQueue({
    apiClientAvailable: Boolean(apiClient),
    queueRunning: queueRunner.queueRunning,
    hasRunnableItems: queueController.queueItems.some((item) => item.status === "ready" || item.status === "warning"),
    blocker: conversionBlocker,
  });
  const primaryAction = resolveConversionPrimaryAction({
    queueItemCount: queueController.queueItems.length,
    canStartConversion: !startDisabled,
    canRunQueue,
  });

  useEffect(() => {
    if (!startCommandRef) {
      return;
    }
    startCommandRef.current = () => {
      if (primaryAction.primary === "run_queue") {
        if (canRunQueue) {
          void queueRunner.onRunQueue();
        }
        return;
      }
      if (!startDisabled) {
        void onStartConversion();
      }
    };
    return () => {
      if (startCommandRef.current) {
        startCommandRef.current = null;
      }
    };
  }, [
    canRunQueue,
    onStartConversion,
    primaryAction.primary,
    queueRunner.onRunQueue,
    startCommandRef,
    startDisabled,
  ]);

  const cancelDisabled = !conversionJob.activeJobId || conversionJob.isCancelling;
  const mergedFormError = conversionJob.formError || optionState.error;
  const runtimeStatusClass = optionState.cleanerRuntimeError
    ? "field-hint is-error"
    : "field-hint";
  const selectedCleanerBackend = optionState.selectedCleanerBackend;
  const showCleanerCudaControls = cleanerBackendUsesCudaFields(selectedCleanerBackend);
  const cleanerCudaQuantizationOptions =
    selectedCleanerBackend?.supported_quantizations?.map((quantization) => ({
      value: quantization,
      label: quantization,
    })) ?? [];
  const selectedPreflightCard = selectedPreflight
    ? {
        title: selectedPreflight.title || splitPath(queueController.sourcePath) || "Untitled source",
        meta: preflightMeta(selectedPreflight),
        author: selectedPreflight.author?.trim() || "",
        sourceType: selectedPreflight.source_type?.trim() || "",
        warnings: selectedPreflight.warnings,
        sample: selectedPreflight.sample ? compactSample(selectedPreflight.sample, 360) : "No readable sample found.",
      }
    : null;
  const sourceDisplay = describeSourceDisplay(
    queueController.sourcePath,
    queueController.selectedQueueItem?.label,
  );
  const queueItemViewModels = queueController.queueItems.map((item, index) => {
    const selected = item.id === queueController.selectedQueueId;
    return {
      id: item.id,
      title: item.title || item.label,
      statusText: queueStatusText(item),
      statusClassName: `queue-item-status status-${item.status}`,
      converting: item.status === "converting",
      selected,
      canMoveUp: index !== 0 && !queueRunner.queueRunning,
      canMoveDown: index !== queueController.queueItems.length - 1 && !queueRunner.queueRunning,
      canRemove: !queueRunner.queueRunning,
    };
  });

  return {
    shell: {
      activeTab,
      hidden,
      isDocumentMode,
    },
    capabilities: {
      canAddPastedText: Boolean(apiClient) && !sourceQueue.pasteBusy && Boolean(sourceQueue.pastedText.trim()),
      canFetchCleanerModels:
        Boolean(apiClient) &&
        !optionState.openAiModelFetchLoading &&
        Boolean(optionState.cleanerApiKey.trim()),
      canInspectSource: Boolean(apiClient) && Boolean(queueController.sourcePath.trim()),
      canPreviewVoice: !preview.previewBusy && Boolean(optionState.selectedVoice) && Boolean(apiClient),
      canRecommendVoice:
        Boolean(apiClient) &&
        !preview.voiceSuggestBusy &&
        Boolean(queueController.sourcePath.trim()) &&
        Boolean(optionState.selectedEngine),
      canRunQueue: primaryAction.showRunQueue && canRunQueue,
      canStartConversion: primaryAction.showStartConversion && !startDisabled,
      canCancelJob: !cancelDisabled,
      canRefreshJob: Boolean(apiClient),
      showRunQueue: primaryAction.showRunQueue,
      showStartConversion: primaryAction.showStartConversion,
    },
    sourceQueue: {
      isDocumentMode,
      sourcePath: queueController.sourcePath,
      sourceFileName: sourceDisplay.name,
      sourceFileDir: sourceDisplay.dir,
      titleOverride: queueController.titleOverride,
      authorOverride: queueController.authorOverride,
      dragging: sourceQueue.dragging,
      pastedText: sourceQueue.pastedText,
      pastedTitle: sourceQueue.pastedTitle,
      pastedAuthor: sourceQueue.pastedAuthor,
      pasteBusy: sourceQueue.pasteBusy,
      pasteStatus: sourceQueue.pasteStatus,
      queueItems: queueItemViewModels,
      queueCount: queueController.queueItems.length,
      queueRunning: queueRunner.queueRunning,
      queueStatus: queueController.queueStatus,
      formError: mergedFormError,
      sourcePathPlaceholder,
      preflightCard: selectedPreflightCard,
      preflightStatus: queueController.preflightStatus,
      browserFileInputRef: sourceQueue.browserFileInputRef,
      browserFileAccept: BROWSER_SOURCE_ACCEPT,
    },
    ttsOptions: {
      selectedEngine: optionState.selectedEngine,
      engineOptions: optionState.engineOptions,
      engineDisabled: optionState.loading || optionState.engineOptions.length === 0,
      voiceFilter: optionState.voiceFilter,
      selectedVoice: optionState.selectedVoice,
      filteredVoiceOptions: optionState.filteredVoiceOptions,
      voiceSelectDisabled: optionState.loading || optionState.filteredVoiceOptions.length === 0,
      voiceSuggestBusy: preview.voiceSuggestBusy,
      voiceSuggestStatus: preview.voiceSuggestStatus,
      previewText: preview.previewText,
      previewBusy: preview.previewBusy,
      previewStatus: preview.previewStatus,
      previewError: preview.previewError,
      previewAudioRef: preview.previewAudioRef,
    },
    cleanerOptions: {
      selectedBackendDisplayName: selectedCleanerBackend?.display_name ?? "",
      cleanerBackend: optionState.cleanerBackend,
      cleanerBackendOptions: optionState.cleanerBackendOptions,
      cleanerRuntimeStatus: optionState.cleanerRuntimeStatus,
      runtimeStatusClass,
      cleanerModel: optionState.cleanerModel,
      cleanerModelOptions: optionState.cleanerModelOptions,
      showCleanerCudaControls,
      cleanerCudaDevice: optionState.cleanerCudaDevice,
      cleanerCudaDevicePlaceholder: selectedCleanerBackend?.default_device || "cuda:0",
      cleanerCudaQuantization: optionState.cleanerCudaQuantization,
      cleanerCudaQuantizationOptions,
      requiresApiKey: Boolean(selectedCleanerBackend?.requires_api_key),
      cleanerApiKey: optionState.cleanerApiKey,
      openAiModelFetchLoading: optionState.openAiModelFetchLoading,
      openAiModelFetchError: optionState.openAiModelFetchError,
      openAiModelFetchStatus: optionState.openAiModelFetchStatus,
    },
    advancedOptions: {
      speedInput: optionState.speedInput,
      selectedQuantization: optionState.selectedQuantization,
      quantizationOptions: optionState.quantizationOptions,
      // Options are ["default", <engine default>] when the engine supports a
      // single precision; only a third entry means there is a real choice.
      showQuantization: optionState.quantizationOptions.length > 2,
      manualParallelWorkerUpperBound: optionState.manualParallelWorkerUpperBound,
      selectedParallelWorkerLimit: optionState.selectedParallelWorkerLimit,
      parallelManualMax: optionState.parallelManualMax,
      parallelWorkerError: optionState.parallelWorkerError,
      parallelWorkerStatus: optionState.parallelWorkerStatus,
      parallelWorkerDetail: optionState.parallelRecommendation?.calculation ?? "",
    },
    conversionActions: {
      hasActiveJob: Boolean(conversionJob.activeJobId),
      blockingReason: conversionBlocker.blockingReason,
      showSettingsShortcut: conversionBlocker.showSettingsShortcut,
      startLabel: conversionJob.isStarting
        ? "Starting…"
        : primaryAction.primary === "run_queue"
          ? "Run queue"
          : "Start conversion",
      cancelLabel: conversionJob.isCancelling ? "Cancelling…" : "Cancel",
      isStarting: conversionJob.isStarting,
    },
    actions: {
      onOpenSettings,
      onOpenRuntimeSetup,
      setSourcePath: queueController.setSourcePath,
      setTitleOverride: queueController.setTitleOverride as Dispatch<SetStateAction<string>>,
      setAuthorOverride: queueController.setAuthorOverride as Dispatch<SetStateAction<string>>,
      setDragging: sourceQueue.setDragging,
      setPastedText: sourceQueue.setPastedText,
      setPastedTitle: sourceQueue.setPastedTitle,
      setPastedAuthor: sourceQueue.setPastedAuthor,
      setPreviewText: preview.setPreviewText,
      setCleanerBackend: optionState.setCleanerBackend,
      setCleanerModel: optionState.setCleanerModel,
      setCleanerCudaDevice: optionState.setCleanerCudaDevice,
      setCleanerCudaQuantization: optionState.setCleanerCudaQuantization,
      setCleanerApiKey: optionState.setCleanerApiKey,
      setSelectedEngine: optionState.setSelectedEngine,
      setVoiceFilter: optionState.setVoiceFilter,
      setSelectedVoice: optionState.setSelectedVoice,
      setSpeed: optionState.setSpeed,
      setSelectedQuantization: optionState.setSelectedQuantization,
      setParallelManualMax: optionState.setParallelManualMax,
      onBrowse: sourceQueue.onBrowse,
      onBrowserFilesSelected: sourceQueue.onBrowserFilesSelected,
      onAddPastedText: sourceQueue.onAddPastedText,
      onDropZoneDrop: sourceQueue.onDropZoneDrop,
      onInspectCurrentSource: sourceQueue.onInspectCurrentSource,
      onPreviewVoice: preview.onPreviewVoice,
      onRecommendVoice: preview.onRecommendVoice,
      onRunQueue: queueRunner.onRunQueue,
      onStartConversion,
      onFetchCleanerModels: optionState.fetchOpenAiModelIds,
      onRefreshJob: conversionJob.refreshActiveConversion,
      onCancelJob: conversionJob.cancelActiveConversion,
      clearFormError: conversionJob.clearFormError,
      selectQueueItem: (itemId: string) => {
        const item = queueController.queueItems.find((candidate) => candidate.id === itemId);
        if (item) {
          queueController.applySelectedQueueItem(item);
        }
      },
      moveQueueItem: queueController.moveQueueItem,
      removeQueueItem: queueController.removeQueueItem,
      syncSelectedQueueEdits: (patch: QueueEditPatch) =>
        queueController.syncSelectedQueueEdits(patch),
    },
  };
}
