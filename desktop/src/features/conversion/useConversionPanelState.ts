import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";

import type { ConversionMode } from "../../types/api";
import { useConversionOptions } from "./useConversionOptions";
import { useConversionPreflight } from "./useConversionPreflight";
import { useConversionPreview } from "./useConversionPreview";
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

  const runPreflightQueueRef = useRef<
    (
      items: ConversionQueueItem[],
      options?: { selectedItemId?: string | null },
    ) => Promise<void>
  >(async () => {});
  const runPreflightForItemRef = useRef<
    (item: ConversionQueueItem, options?: { updateSelected?: boolean }) => Promise<void>
  >(async () => {});
  const setPreflightStatusRef = useRef<(status: string) => void>(() => {});
  const previewSetTextRef = useRef<(text: string) => void>(() => {});

  const sourceQueue = useConversionSourceQueue({
    apiClient,
    conversionMode,
    conversionJob,
    runPreflightQueue: (items, options) => runPreflightQueueRef.current(items, options),
    runPreflightForItem: (item, options) =>
      runPreflightForItemRef.current(item, options),
    setPreflightStatus: (status) => setPreflightStatusRef.current(status),
    setPreviewText: (text) => previewSetTextRef.current(text),
  });

  const preview = useConversionPreview({
    apiClient,
    optionState,
    sourcePath: sourceQueue.sourcePath,
    selectedPreflight: sourceQueue.selectedQueueItem?.preflight ?? null,
  });
  useEffect(() => {
    previewSetTextRef.current = preview.setPreviewText;
  }, [preview.setPreviewText]);

  const preflight = useConversionPreflight({
    apiClient,
    setSelectedQueueId: (itemId) => sourceQueue.setSelectedQueueId(itemId),
    setSourcePath: (sourcePath) => sourceQueue.setSourcePath(sourcePath),
    setSourceFileLabel: (label) => sourceQueue.setSourceFileLabel(label),
    setTitleOverride: (updater) =>
      sourceQueue.setTitleOverride(updater as SetStateAction<string>),
    setAuthorOverride: (updater) =>
      sourceQueue.setAuthorOverride(updater as SetStateAction<string>),
    setPreviewText: preview.setPreviewText,
    updateQueueItem: (itemId, updater) => sourceQueue.updateQueueItem(itemId, updater),
  });
  useEffect(() => {
    runPreflightQueueRef.current = preflight.runPreflightQueue;
    runPreflightForItemRef.current = preflight.runPreflightForItem;
    setPreflightStatusRef.current = preflight.setPreflightStatus;
  }, [
    preflight.runPreflightForItem,
    preflight.runPreflightQueue,
    preflight.setPreflightStatus,
  ]);

  const selectedPreflight = sourceQueue.selectedQueueItem?.preflight ?? null;
  const payload = useConversionSubmitPayload({
    optionState,
    conversionMode,
    sourcePath: sourceQueue.sourcePath,
    titleOverride: sourceQueue.titleOverride,
    authorOverride: sourceQueue.authorOverride,
  });
  const queueRunner = useConversionQueueRunner({
    apiClient,
    conversionJob,
    queueItems: sourceQueue.queueItems,
    selectedQueueItem: sourceQueue.selectedQueueItem,
    buildStartPayload: payload.buildStartPayload,
    updateQueueItem: sourceQueue.updateQueueItem,
    syncSelectedQueueEdits: sourceQueue.syncSelectedQueueEdits,
    setQueueStatus: sourceQueue.setQueueStatus,
  });

  const requiresCleanerApiKey = optionState.selectedCleanerBackend?.requires_api_key ?? false;

  const startDisabled =
    !apiClient ||
    queueRunner.queueRunning ||
    conversionJob.isStarting ||
    !sourceQueue.sourcePath.trim() ||
    !optionState.selectedEngine ||
    !optionState.selectedVoice ||
    optionState.cleanerRuntimeError ||
    (requiresCleanerApiKey && !optionState.cleanerApiKey.trim()) ||
    (optionState.cleanerModelOptions.length > 0 && !optionState.cleanerModel);

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
      sourcePath: sourceQueue.sourcePath,
      sourceFileLabel: sourceQueue.sourceFileLabel,
      titleOverride: sourceQueue.titleOverride,
      authorOverride: sourceQueue.authorOverride,
      selectedPreflight,
      dragging: sourceQueue.dragging,
      pasteTitle: sourceQueue.pasteTitle,
      pastedText: sourceQueue.pastedText,
      pasteBusy: sourceQueue.pasteBusy,
      pasteStatus: sourceQueue.pasteStatus,
      voiceSuggestBusy: preview.voiceSuggestBusy,
      voiceSuggestStatus: preview.voiceSuggestStatus,
      preflightStatus: preflight.preflightStatus,
      previewText: preview.previewText,
      previewBusy: preview.previewBusy,
      previewStatus: preview.previewStatus,
      previewError: preview.previewError,
      previewAudioRef: preview.previewAudioRef,
    },
    queue: {
      queueItems: sourceQueue.queueItems,
      selectedQueueId: sourceQueue.selectedQueueId,
      queueRunning: queueRunner.queueRunning,
      queueStatus: sourceQueue.queueStatus,
    },
    actions: {
      setSourcePath: sourceQueue.setSourcePath,
      setTitleOverride: sourceQueue.setTitleOverride as Dispatch<SetStateAction<string>>,
      setAuthorOverride: sourceQueue.setAuthorOverride as Dispatch<SetStateAction<string>>,
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
      applySelectedQueueItem: sourceQueue.applySelectedQueueItem,
      moveQueueItem: sourceQueue.moveQueueItem,
      removeQueueItem: sourceQueue.removeQueueItem,
      syncSelectedQueueEdits: (patch: QueueEditPatch) =>
        sourceQueue.syncSelectedQueueEdits(patch),
    },
  };
}
