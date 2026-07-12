import { useEffect, type Dispatch, type SetStateAction } from "react";

import type { ConversionMode } from "../../types/api";
import { resolveConversionPrimaryAction } from "./conversionPrimaryAction";
import { cleanerBackendUsesCudaFields } from "./cleanerBackendHelpers";
import { compactSample, nowStatus, splitPath } from "./conversionPanelHelpers";
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
  ConversionPanelActions,
  ConversionPanelViewModel,
  QueueEditPatch,
  UseConversionPanelStateArgs,
} from "./conversionPanelStateTypes";

const BROWSER_SOURCE_ACCEPT = ".pdf,.epub,.txt,.md";

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
  connectionState,
  health,
  statusMessage,
  conversionJob,
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

  const startDisabled =
    !apiClient ||
    queueRunner.queueRunning ||
    conversionJob.isStarting ||
    !queueController.sourcePath.trim() ||
    !optionState.selectedEngine ||
    !optionState.selectedVoice ||
    optionState.cleanerRuntimeError ||
    (requiresCleanerApiKey && !optionState.cleanerApiKey.trim()) ||
    (optionState.cleanerModelOptions.length > 0 && !optionState.cleanerModel);

  const canRunQueue =
    !queueRunner.queueRunning &&
    Boolean(apiClient) &&
    queueController.queueItems.some((item) => item.status === "ready" || item.status === "warning");
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
        void queueRunner.onStartConversion();
      }
    };
    return () => {
      if (startCommandRef.current) {
        startCommandRef.current = null;
      }
    };
  }, [
    canRunQueue,
    primaryAction.primary,
    queueRunner.onRunQueue,
    queueRunner.onStartConversion,
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
        title: selectedPreflight.title || splitPath(queueController.sourcePath) || "untitled source",
        meta: preflightMeta(selectedPreflight),
        author: selectedPreflight.author || "unknown",
        sourceType: selectedPreflight.source_type || "unknown",
        warnings: selectedPreflight.warnings,
        sample: selectedPreflight.sample ? compactSample(selectedPreflight.sample, 360) : "No readable sample found.",
      }
    : null;
  const queueItemViewModels = queueController.queueItems.map((item, index) => {
    const selected = item.id === queueController.selectedQueueId;
    return {
      id: item.id,
      title: item.title || item.label,
      statusText: `${item.status}${item.error ? ` / ${item.error}` : ""}`,
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
      canRunParallelWarmup: Boolean(apiClient) && !optionState.parallelWarmupLoading,
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
      sourceFileLabel: queueController.sourceFileLabel,
      titleOverride: queueController.titleOverride,
      authorOverride: queueController.authorOverride,
      dragging: sourceQueue.dragging,
      pasteTitle: sourceQueue.pasteTitle,
      pastedText: sourceQueue.pastedText,
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
      parallelWorkerMode: optionState.parallelWorkerMode,
      manualParallelWorkerUpperBound: optionState.manualParallelWorkerUpperBound,
      selectedParallelWorkerLimit: optionState.selectedParallelWorkerLimit,
      parallelManualMax: optionState.parallelManualMax,
      parallelWarmupLoading: optionState.parallelWarmupLoading,
      parallelWorkerError: optionState.parallelWorkerError,
      parallelWorkerStatus: optionState.parallelWorkerStatus,
    },
    conversionActions: {
      backendStatus: nowStatus(connectionState),
      healthStatus: health ? `status=${health.status}` : "unavailable",
      jobStatus: conversionJob.activeJobId
        ? `${conversionJob.activeJobStatus} (${conversionJob.activeJobId})`
        : "no active conversion",
      detailStatus: conversionJob.statusText || statusMessage,
      startLabel: conversionJob.isStarting
        ? "starting..."
        : primaryAction.primary === "run_queue"
          ? "run queue"
          : "start conversion",
      cancelLabel: conversionJob.isCancelling ? "cancelling..." : "cancel",
      isStarting: conversionJob.isStarting,
    },
    actions: {
      setSourcePath: queueController.setSourcePath,
      setTitleOverride: queueController.setTitleOverride as Dispatch<SetStateAction<string>>,
      setAuthorOverride: queueController.setAuthorOverride as Dispatch<SetStateAction<string>>,
      setDragging: sourceQueue.setDragging,
      setPasteTitle: sourceQueue.setPasteTitle,
      setPastedText: sourceQueue.setPastedText,
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
      setParallelWorkerMode: optionState.setParallelWorkerMode,
      setParallelManualMax: optionState.setParallelManualMax,
      onBrowse: sourceQueue.onBrowse,
      onBrowserFilesSelected: sourceQueue.onBrowserFilesSelected,
      onAddPastedText: sourceQueue.onAddPastedText,
      onDropZoneDrop: sourceQueue.onDropZoneDrop,
      onInspectCurrentSource: sourceQueue.onInspectCurrentSource,
      onPreviewVoice: preview.onPreviewVoice,
      onRecommendVoice: preview.onRecommendVoice,
      onRunQueue: queueRunner.onRunQueue,
      onStartConversion: queueRunner.onStartConversion,
      onFetchCleanerModels: optionState.fetchOpenAiModelIds,
      onRunParallelWarmup: optionState.runParallelWarmup,
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
