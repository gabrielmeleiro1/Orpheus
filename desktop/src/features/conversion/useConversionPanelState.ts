import { useEffect, type Dispatch, type SetStateAction } from "react";

import type { ConversionMode } from "../../types/api";
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

export type {
  ConversionPanelActions,
  ConversionPanelFormState,
  ConversionPanelQueueState,
  ConversionPanelShellState,
  ConversionPanelViewModel,
  QueueEditPatch,
  UseConversionPanelStateArgs,
} from "./conversionPanelStateTypes";

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

  useEffect(() => {
    if (!startCommandRef) {
      return;
    }
    startCommandRef.current = () => {
      if (!startDisabled) {
        void queueRunner.onStartConversion();
      }
    };
    return () => {
      if (startCommandRef.current) {
        startCommandRef.current = null;
      }
    };
  }, [queueRunner.onStartConversion, startCommandRef, startDisabled]);

  const cancelDisabled = !conversionJob.activeJobId || conversionJob.isCancelling;
  const mergedFormError = conversionJob.formError || optionState.error;
  const runtimeStatusClass = optionState.cleanerRuntimeError
    ? "field-hint is-error"
    : "field-hint";

  return {
    shell: {
      activeTab,
      hidden,
      apiClient,
      connectionState,
      health,
      statusMessage,
      conversionJob,
      optionState,
      isDocumentMode,
      sourcePathPlaceholder,
      startDisabled,
      cancelDisabled,
      mergedFormError,
      runtimeStatusClass,
    },
    form: {
      sourcePath: queueController.sourcePath,
      sourceFileLabel: queueController.sourceFileLabel,
      titleOverride: queueController.titleOverride,
      authorOverride: queueController.authorOverride,
      selectedPreflight,
      dragging: sourceQueue.dragging,
      pasteTitle: sourceQueue.pasteTitle,
      pastedText: sourceQueue.pastedText,
      pasteBusy: sourceQueue.pasteBusy,
      pasteStatus: sourceQueue.pasteStatus,
      voiceSuggestBusy: preview.voiceSuggestBusy,
      voiceSuggestStatus: preview.voiceSuggestStatus,
      preflightStatus: queueController.preflightStatus,
      previewText: preview.previewText,
      previewBusy: preview.previewBusy,
      previewStatus: preview.previewStatus,
      previewError: preview.previewError,
      previewAudioRef: preview.previewAudioRef,
    },
    queue: {
      queueItems: queueController.queueItems,
      selectedQueueId: queueController.selectedQueueId,
      queueRunning: queueRunner.queueRunning,
      queueStatus: queueController.queueStatus,
    },
    actions: {
      setSourcePath: queueController.setSourcePath,
      setTitleOverride: queueController.setTitleOverride as Dispatch<SetStateAction<string>>,
      setAuthorOverride: queueController.setAuthorOverride as Dispatch<SetStateAction<string>>,
      setDragging: sourceQueue.setDragging,
      setPasteTitle: sourceQueue.setPasteTitle,
      setPastedText: sourceQueue.setPastedText,
      setPreviewText: preview.setPreviewText,
      onBrowse: sourceQueue.onBrowse,
      onAddPastedText: sourceQueue.onAddPastedText,
      onDropZoneDrop: sourceQueue.onDropZoneDrop,
      onInspectCurrentSource: sourceQueue.onInspectCurrentSource,
      onPreviewVoice: preview.onPreviewVoice,
      onRecommendVoice: preview.onRecommendVoice,
      onRunQueue: queueRunner.onRunQueue,
      onStartConversion: queueRunner.onStartConversion,
      applySelectedQueueItem: queueController.applySelectedQueueItem,
      moveQueueItem: queueController.moveQueueItem,
      removeQueueItem: queueController.removeQueueItem,
      syncSelectedQueueEdits: (patch: QueueEditPatch) =>
        queueController.syncSelectedQueueEdits(patch),
    },
  };
}
