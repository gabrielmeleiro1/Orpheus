import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import type { BackendConnectionState, ConversionMode } from "../../types/api";
import { chapterStageLabel } from "../events/progressStageModel";
import { dashboardNowStageLabel, formatStageDisplayName } from "../progress/progressTerminalPresentation";
import { resolveConversionPrimaryAction } from "./conversionPrimaryAction";
import { cleanerBackendUsesCudaFields } from "./cleanerBackendHelpers";
import { compactSample, pastedTextEstimate, splitPath } from "./conversionPanelHelpers";
import {
  estimatedAudioNote,
  isFinishedQueueItem,
  isPreflightConvertible,
  preflightBlockingReason,
} from "./conversionPanelModel";
import { useConversionOptions } from "./useConversionOptions";
import { useConversionPreview } from "./useConversionPreview";
import { useConversionQueueController } from "./useConversionQueueController";
import { useConversionQueueRunner } from "./useConversionQueueRunner";
import { useConversionSourceQueue } from "./useConversionSourceQueue";
import { useConversionSubmitPayload } from "./useConversionSubmitPayload";
import type {
  ConversionPanelViewModel,
  ConversionRuntimeState,
  QueueEditPatch,
  SourceInput,
  UseConversionPanelStateArgs,
} from "./conversionPanelStateTypes";
import type { ConversionQueueItem } from "./conversionPanelModel";

export type {
  ConversionPanelViewModel,
  UseConversionPanelStateArgs,
} from "./conversionPanelStateTypes";

const BROWSER_SOURCE_ACCEPT = ".pdf,.epub,.txt,.md";

/** Informational: converting again stays allowed. */
function existingBookNote(preflight: ConversionQueueItem["preflight"]): string {
  const titles = [...new Set((preflight?.existing_books ?? []).map((book) => book.title.trim() || "Untitled"))];
  return titles.length ? `Already in your library: ${titles.join(", ")}` : "";
}

interface ConversionBlockerInput {
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

interface ConversionBlocker {
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

/** The blocker shown while nothing is queued; the empty page already says what to do. */
export const SOURCE_REQUIRED_REASON = "Add a book to start.";

export function resolveConversionBlocker(input: ConversionBlockerInput): ConversionBlocker {
  if (!input.apiClientAvailable) {
    return { blockingReason: "Not connected.", showSettingsShortcut: false };
  }
  // Un-queued pasted text counts as a source: starting will queue it first.
  if (!input.sourcePath.trim() && !input.hasPastedText) {
    return { blockingReason: SOURCE_REQUIRED_REASON, showSettingsShortcut: false };
  }
  if (!input.selectedEngine) {
    return { blockingReason: "Choose a voice engine.", showSettingsShortcut: false };
  }
  if (!input.selectedVoice) {
    return { blockingReason: "Choose a voice.", showSettingsShortcut: false };
  }
  if (input.cleanerRuntimeError) {
    return { blockingReason: "Text cleaning is unavailable. Check Settings.", showSettingsShortcut: true };
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
    return { blockingReason: "Choose a text cleaning model in Settings.", showSettingsShortcut: true };
  }
  return { blockingReason: null, showSettingsShortcut: false };
}

/** Why Create cannot start anything while the local runtime is not verified. */
export function resolveRuntimeBlocker(runtimeState: ConversionRuntimeState): ConversionBlocker | null {
  if (runtimeState === "checking") {
    return { blockingReason: "Wait for the runtime check to finish.", showSettingsShortcut: false };
  }
  if (runtimeState === "unavailable") {
    return { blockingReason: "Conversion is unavailable. Set up a voice model in Settings.", showSettingsShortcut: true };
  }
  return null;
}

/** Why Create cannot start anything while the backend is not answering. */
export function resolveConnectionBlocker(connectionState: BackendConnectionState): ConversionBlocker | null {
  return connectionState === "connected" ? null
    : { blockingReason: "Orpheus is reconnecting. Start is available once it is back.", showSettingsShortcut: false };
}

/**
 * Cancel takes Start's place under the pointer, so an activation this soon
 * after Start is the tail of the same gesture, not a decision to stop.
 */
const START_TO_CANCEL_GUARD_MS = 500;

/** The queue runner's error for a conversion the user stopped. */
const CANCELLED_ERROR = "conversion cancelled";

const QUEUE_STATUS_LABELS:Record<ConversionQueueItem["status"], string> = {
  ready: "Ready",
  preflighting: "Inspecting…",
  warning: "Warning",
  converting: "Converting…",
  done: "Done",
  failed: "Failed",
};


function preflightMeta(preflight: NonNullable<ConversionQueueItem["preflight"]>): string {
  const count = Number(preflight.chapter_count || 0);
  const chapterText = count === 1 ? "1 chapter" : `${count} chapters`;
  const sizeText =
    preflight.size_bytes > 1024 * 1024
      ? `${(preflight.size_bytes / (1024 * 1024)).toFixed(1)} MB`
      : `${Math.max(1, Math.round(preflight.size_bytes / 1024))} KB`;
  return `${preflight.source_type ? preflight.source_type.toUpperCase() : "Source"} · ${chapterText} · ${sizeText}`;
}

export function useConversionPanelState({
  activeTab,
  hidden,
  apiClient,
  runtimeSelectionGeneration,
  runtimeState = "ready",
  connectionState,
  conversionJob,
  onOpenSettings,
  onOpenRuntimeSetup,
  onOpenConversionDetails,
  startCommandRef,
}: UseConversionPanelStateArgs): ConversionPanelViewModel {
  // Files and pasted text share one page. The format decides how the result is
  // narrated; the source input decides how a source is added.
  const [conversionMode, setConversionModeState] = useState<ConversionMode>("audiobook");
  const [sourceInput, setSourceInput] = useState<SourceInput>("file");
  const isDocumentMode = conversionMode === "document_audio";

  const optionState = useConversionOptions({
    apiClient,
    runtimeSelectionGeneration,
    conversionMode,
  });
  const sourcePathPlaceholder = "Full path to a PDF, EPUB, TXT or Markdown file";

  const queueController = useConversionQueueController({
    apiClient,
  });
  const { queueItems: currentQueueItems, updateQueueItem, noteJobBook } = queueController;

  const { activeJobBook } = conversionJob;
  const jobCompleted = conversionJob.uiState === "completed";
  useEffect(() => {
    if (activeJobBook) noteJobBook(activeJobBook.jobId, activeJobBook.bookId, jobCompleted);
  }, [activeJobBook, jobCompleted, noteJobBook]);

  // A format change applies to everything not yet converted, so the choice on
  // screen always matches what will be produced.
  const setConversionMode = useCallback((mode: ConversionMode) => {
    setConversionModeState(mode);
    for (const item of currentQueueItems) {
      if (item.status !== "converting" && item.status !== "done" && item.conversionMode !== mode) {
        updateQueueItem(item.id, (current) => ({ ...current, conversionMode: mode }));
      }
    }
  }, [currentQueueItems, updateQueueItem]);

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
    hidden,
  });

  const selectedPreflight = queueController.selectedQueueItem?.preflight ?? null;
  const payload = useConversionSubmitPayload({
    optionState,
    conversionMode,
    sourcePath: queueController.sourcePath,
    titleOverride: queueController.titleOverride,
    authorOverride: queueController.authorOverride,
  });
  const runtimeOptionsReady = !optionState.loading && !optionState.error;
  const queueRunner = useConversionQueueRunner({
    runtimeOptionsReady,
    apiClient,
    conversionJob,
    queueItems: queueController.queueItems,
    selectedQueueItem: queueController.selectedQueueItem,
    buildStartPayload: payload.buildStartPayload,
    addQueueItems: queueController.addQueueItems,
    updateQueueItem: queueController.updateQueueItem,
    syncSelectedQueueEdits: queueController.syncSelectedQueueEdits,
    setQueueStatus: queueController.setQueueStatus,
  });

  const requiresCleanerApiKey = optionState.selectedCleanerBackend?.requires_api_key ?? false;
  const hasPastedText = sourceInput === "paste" && Boolean(sourceQueue.pastedText.trim());

  // Without a client the connection is the problem to report, not the runtime.
  const runtimeBlocker = apiClient ? resolveRuntimeBlocker(runtimeState) : null;
  // A first connection is still "checking" too, and the runtime check already
  // says to wait for it; after that, a backend that stops answering comes first.
  const connectionBlocker = apiClient && runtimeState !== "checking" ? resolveConnectionBlocker(connectionState) : null;
  const conversionBlocker = connectionBlocker ?? runtimeBlocker ?? (!runtimeOptionsReady
    ? { blockingReason: optionState.error
      ? "Voices could not load."
      : "Loading voices…", showSettingsShortcut: false }
    : resolveConversionBlocker({
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
    }));
  const selectedPreflightBlocker =
    selectedPreflight ? preflightBlockingReason(selectedPreflight) : null;
  const startBlockingReason = selectedPreflightBlocker ?? conversionBlocker.blockingReason;

  const sourcelessPasteReady = hasPastedText && !queueController.sourcePath.trim();
  const startDisabled = resolveStartDisabled({
    blockingReason: startBlockingReason,
    queueRunning: queueRunner.commandRunning,
    isStarting: conversionJob.isStarting || conversionJob.isCheckingStart,
    hasActiveJob: Boolean(conversionJob.activeJobId),
    pasteBusy: sourceQueue.pasteBusy,
    sourcelessPasteReady,
  });

  const { onAddPastedText } = sourceQueue;
  const { onStartConversion: startQueueItemConversion, onRunQueue: runQueue } = queueRunner;
  const startActivatedAtRef = useRef(Number.NEGATIVE_INFINITY);
  const onStartConversion = useCallback(async () => {
    if (!runtimeOptionsReady) return;
    startActivatedAtRef.current = performance.now();
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
  }, [onAddPastedText, runtimeOptionsReady, sourcelessPasteReady, startQueueItemConversion]);

  const onRunQueue = useCallback(async () => {
    startActivatedAtRef.current = performance.now();
    await runQueue();
  }, [runQueue]);

  const canRunQueue = canRunConversionQueue({
    apiClientAvailable: Boolean(apiClient),
    queueRunning: queueRunner.commandRunning || conversionJob.isCheckingStart || Boolean(conversionJob.activeJobId),
    hasRunnableItems: queueController.queueItems.some((item) => item.status === "ready" || item.status === "warning"),
    blocker: conversionBlocker,
  });
  const primaryAction = resolveConversionPrimaryAction({
    queueItemCount: queueController.queueItems.filter((item) => !isFinishedQueueItem(item)).length,
    selectedFinished: Boolean(queueController.selectedQueueItem && isFinishedQueueItem(queueController.selectedQueueItem)),
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
          void onRunQueue();
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
    onRunQueue,
    primaryAction.primary,
    startCommandRef,
    startDisabled,
  ]);

  const cancelDisabled = (!conversionJob.activeJobId && !queueRunner.commandRunning) || conversionJob.isCancelling;
  // Cancel replaces Start under the pointer: a second click meant for Start
  // must not stop the conversion it just started. Separate fast clicks
  // (touch, automation) report a click count of 1, so time decides as well.
  const acceptsCancelClick = useCallback((clickCount = 0) => (
    clickCount <= 1 && performance.now() - startActivatedAtRef.current >= START_TO_CANCEL_GUARD_MS
  ), []);
  const onConfirmCancelJob = useCallback(async () => {
    // A running book is cancelled on its own and a queue moves on; without one,
    // the pending start or wait is what Cancel stops.
    if (!conversionJob.activeJobId) queueRunner.cancelCommand();
    await conversionJob.cancelActiveConversion();
  }, [conversionJob.activeJobId, conversionJob.cancelActiveConversion, queueRunner.cancelCommand]);
  const onCancelJob = useCallback(async (clickCount = 0) => {
    if (!acceptsCancelClick(clickCount)) return;
    await onConfirmCancelJob();
  }, [acceptsCancelClick, onConfirmCancelJob]);
  const mergedFormError = conversionJob.formError || optionState.error;
  const runtimeStatusClass = optionState.cleanerRuntimeError
    ? "field-hint is-error"
    : "field-hint";
  const selectedCleanerBackend = optionState.selectedCleanerBackend;
  const showCleanerCudaControls = cleanerBackendUsesCudaFields(selectedCleanerBackend);
  const selectedPreflightCard = selectedPreflight
    ? {
      title: selectedPreflight.title || splitPath(queueController.sourcePath) || "Untitled source",
        meta: preflightMeta(selectedPreflight),
        author: selectedPreflight.author?.trim() || "",
        sourceType: selectedPreflight.source_type?.trim().toUpperCase() || "",
      chapterCount: selectedPreflight.chapter_count,
      chapterTitles: selectedPreflight.chapter_titles,
      sizeLabel: preflightMeta(selectedPreflight).split(" · ").at(-1),
      // A blocked source repeats its reason as the only warning; say it once.
      warnings: selectedPreflight.warnings.filter((warning) => warning !== preflightBlockingReason(selectedPreflight)),
      sample: selectedPreflight.sample ? compactSample(selectedPreflight.sample, 360) : "No readable sample found.",
      canConvert: isPreflightConvertible(selectedPreflight),
      blockingReason: preflightBlockingReason(selectedPreflight),
      }
    : null;
  const jobRunning = conversionJob.uiState === "running";
  const jobProgressPct = Math.max(0, Math.min(100, Math.round(Number(conversionJob.progressPct) || 0)));
  const jobStage = jobRunning ? dashboardNowStageLabel(conversionJob.uiState, conversionJob.stages) : "";
  const jobEta = conversionJob.etaText ? conversionJob.etaText.replace(/^eta\s*/i, "").trim() : "";
  // A lost stream freezes the numbers; the row says so rather than showing a stale ETA.
  const jobProgressLabel = conversionJob.connectionLost
    ? conversionJob.statusText
    : [jobRunning ? chapterStageLabel(conversionJob.stages, formatStageDisplayName(jobStage)) : "", jobEta ? `${jobEta} left` : ""].filter(Boolean).join(" · ");
  const runningJobId = jobRunning ? conversionJob.activeJobId ?? null : null;
  const runningBookId = runningJobId !== null && activeJobBook?.jobId === runningJobId ? activeJobBook.bookId : null;
  const hasConvertingRow = queueController.queueItems.some((item) => item.status === "converting");
  // One book, one row: a running job no row is converting for still belongs to
  // the row that started it (its outcome could not be confirmed), or to the
  // earlier row of the book a repair from Library is rebuilding.
  const jobRow = runningJobId === null || hasConvertingRow ? null
    : queueController.queueItems.find((item) => item.jobId === runningJobId)
      ?? queueController.queueItems.find((item) => runningBookId !== null && item.bookId === runningBookId && isFinishedQueueItem(item))
      ?? null;
  // The job's title arrives a moment after the job itself; wait for it rather than show a nameless row.
  const untrackedJob = jobRunning && !hasConvertingRow && !jobRow && conversionJob.activeJobMetaLoaded
    ? { title: conversionJob.activeJobTitle || "Untitled", progressPct: jobProgressPct, progressLabel: jobProgressLabel }
    : null;
  const queueItemViewModels = queueController.queueItems.map((item, index) => {
    const selected = item.id === queueController.selectedQueueId;
    // While the connection is down, a row that could not confirm its own job keeps saying so.
    const converting = item.status === "converting"
      || (item.id === jobRow?.id && !(conversionJob.connectionLost && item.jobId === runningJobId));
    // A stop the user asked for is not a failure: say "Cancelled", quietly.
    const cancelled = !converting && item.status === "failed" && item.error === CANCELLED_ERROR;
    const status = converting ? "converting" : item.status;
    return {
      id: item.id,
      title: item.title || item.label,
      meta: [existingBookNote(item.preflight), item.author, item.preflight?.source_type?.toUpperCase(), item.preflight ? `${item.preflight.chapter_count} ${item.preflight.chapter_count === 1 ? "chapter" : "chapters"}` : "", estimatedAudioNote(item.preflight)].filter(Boolean).join(" · "),
      statusText: cancelled ? "Cancelled" : QUEUE_STATUS_LABELS[status] ?? status,
      statusClassName: `queue-item-status status-${cancelled ? "cancelled" : status}`,
      error: cancelled || converting ? undefined : item.error || undefined,
      inspecting: item.status === "preflighting",
      converting,
      finished: !converting && isFinishedQueueItem(item),
      progressPct: converting && jobRunning ? jobProgressPct : undefined,
      progressLabel: converting && jobRunning ? jobProgressLabel : undefined,
      selected,
      canMoveUp: index !== 0 && !converting,
      canMoveDown: index !== queueController.queueItems.length - 1 && !converting,
      canRemove: !converting,
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
      canPreviewVoice: runtimeOptionsReady && !preview.previewBusy && Boolean(optionState.selectedVoice) && Boolean(apiClient),
      recommendationUnavailableReason: preview.recommendationUnavailableReason,
      canRecommendVoice:
        Boolean(apiClient) &&
        !preview.voiceSuggestBusy &&
        !preview.recommendationUnavailableReason &&
        Boolean(queueController.sourcePath.trim()) &&
        Boolean(optionState.selectedEngine),
      canRunQueue: primaryAction.showRunQueue && canRunQueue,
      canStartConversion: primaryAction.showStartConversion && !startDisabled,
      canCancelJob: !cancelDisabled,
      canRefreshJob: Boolean(apiClient),
      canRetryRuntimeOptions: Boolean(apiClient) && Boolean(optionState.error) && !optionState.loading,
      showRunQueue: primaryAction.showRunQueue,
      showStartConversion: primaryAction.showStartConversion,
    },
    sourceQueue: {
      isDocumentMode,
      sourceInput,
      sourcePath: queueController.sourcePath,
      titleOverride: queueController.titleOverride,
      authorOverride: queueController.authorOverride,
      dragging: sourceQueue.dragging,
      pastedText: sourceQueue.pastedText,
      pastedTitle: sourceQueue.pastedTitle,
      pastedAuthor: sourceQueue.pastedAuthor,
      pasteBusy: sourceQueue.pasteBusy,
      // Before anything else is said about the paste, say how big it is.
      pasteStatus: sourceQueue.pasteStatus === "Idle" && sourceQueue.pastedText.trim()
        ? pastedTextEstimate(sourceQueue.pastedText) : sourceQueue.pasteStatus,
      queueItems: queueItemViewModels,
      untrackedJob,
      // The header counts every row shown, including a job started elsewhere (a repair).
      queueCount: queueController.queueItems.length + (untrackedJob ? 1 : 0),
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
      // A search with no matches keeps the menu (and its search field) open.
      voiceSelectDisabled: optionState.loading
        || (optionState.filteredVoiceOptions.length === 0 && !optionState.voiceFilter.trim()),
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
      cleanerCudaDevicePlaceholder: selectedCleanerBackend?.default_device?.trim() || "cuda",
      cleanerCudaQuantization: optionState.cleanerCudaQuantization,
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
      usableParallelWorkerLimit: optionState.usableParallelWorkerLimit,
      parallelManualMax: optionState.parallelManualMax,
      parallelWorkerError: optionState.parallelWorkerError,
      parallelWorkerStatus: optionState.parallelWorkerStatus,
      parallelWorkerDetail: optionState.parallelRecommendation?.calculation ?? "",
    },
    conversionActions: {
      hasActiveJob: Boolean(conversionJob.activeJobId) || queueRunner.commandRunning,
      showRefreshJob: conversionJob.isCheckingStart || Boolean(
        conversionJob.startReconciliation && !conversionJob.startReconciliation.result.ok,
      ),
      blockingReason:
        primaryAction.primary === "run_queue"
          ? conversionBlocker.blockingReason
          : startBlockingReason,
      showSettingsShortcut: conversionBlocker.showSettingsShortcut,
      startLabel: conversionJob.isCheckingStart
        ? "Checking…"
        : conversionJob.isStarting
          ? "Starting…"
          // Shown beside "Start all", the Start button keeps naming its own book.
          : primaryAction.primary === "run_queue" && !primaryAction.showStartConversion
            ? "Start all"
            // Starting a finished book converts it again as a new row.
            : queueController.selectedQueueItem?.status === "done"
              ? "Convert again"
              : "Start",
      // A job that cannot be observed is not known to be converting.
      runningStatus: conversionJob.connectionLost ? conversionJob.statusText : undefined,
      cancelLabel: conversionJob.isCancelling ? "Cancelling…" : "Cancel",
      // Stopping a book that is converting loses its unfinished chapters, so it asks first.
      cancelJobId: conversionJob.isCancelling ? null : conversionJob.activeJobId,
      isStarting: conversionJob.isStarting || conversionJob.isCheckingStart,
    },
    actions: {
      setSourceInput,
      setConversionMode,
      onRetryRuntimeOptions: optionState.retryRuntimeOptions,
      onOpenSettings,
      onOpenRuntimeSetup,
      onAddSourcePath: sourceQueue.onAddSourcePath,
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
      onRunQueue,
      onStartConversion,
      onFetchCleanerModels: optionState.fetchOpenAiModelIds,
      onRefreshJob: conversionJob.refreshActiveConversion,
      onCancelJob,
      acceptsCancelClick,
      onConfirmCancelJob,
      onOpenConversionDetails,
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
