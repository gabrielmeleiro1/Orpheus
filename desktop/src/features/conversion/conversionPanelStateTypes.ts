import type {
  ChangeEvent,
  Dispatch,
  DragEvent,
  MutableRefObject,
  Ref,
  SetStateAction,
} from "react";

import type { ApiClient } from "../../api/client";
import type {
  BackendConnectionState,
  ConversionMode,
  DashboardTab,
  TermSelectOption,
} from "../../types/api";
import type { ConversionJobController } from "./useConversionJob";
import type { ConversionQueueItem } from "./conversionPanelModel";

/** Whether the local runtime can convert: verified, still being checked, or not set up. */
export type ConversionRuntimeState = "ready" | "checking" | "unavailable";

export interface UseConversionPanelStateArgs {
  activeTab: DashboardTab | "config";
  hidden: boolean;
  apiClient: ApiClient | null;
  runtimeSelectionGeneration?: number;
  /** Defaults to "ready"; anything else leaves the page usable but blocks starting. */
  runtimeState?: ConversionRuntimeState;
  connectionState: BackendConnectionState;
  conversionJob: ConversionJobController;
  onOpenSettings: () => void;
  /** Opens the stages-and-log drawer for the running conversion. */
  onOpenConversionDetails?: () => void;
  startCommandRef?: MutableRefObject<(() => void) | null>;
}

export type QueueEditPatch = Partial<
  Pick<ConversionQueueItem, "sourcePath" | "title" | "author">
>;

export interface ConversionCapabilities {
  canAddPastedText: boolean;
  canFetchCleanerModels: boolean;
  canInspectSource: boolean;
  canPreviewVoice: boolean;
  canRecommendVoice: boolean;
  recommendationUnavailableReason?: string;
  canRunQueue: boolean;
  canStartConversion: boolean;
  canCancelJob: boolean;
  canRefreshJob: boolean;
  canRetryRuntimeOptions: boolean;
  showRunQueue: boolean;
  showStartConversion: boolean;
}

export interface CleanerOptionsViewModel {
  selectedBackendDisplayName: string;
  cleanerBackend: string;
  cleanerBackendOptions: TermSelectOption[];
  cleanerRuntimeStatus: string;
  runtimeStatusClass: string;
  cleanerModel: string;
  cleanerModelOptions: TermSelectOption[];
  showCleanerCudaControls: boolean;
  cleanerCudaDevice: string;
  cleanerCudaDevicePlaceholder: string;
  requiresApiKey: boolean;
  cleanerApiKey: string;
  openAiModelFetchLoading: boolean;
  openAiModelFetchError: boolean;
  openAiModelFetchStatus: string;
}

export interface TtsOptionsViewModel {
  selectedEngine: string;
  engineOptions: TermSelectOption[];
  engineDisabled: boolean;
  voiceFilter: string;
  selectedVoice: string;
  filteredVoiceOptions: TermSelectOption[];
  voiceSelectDisabled: boolean;
  voiceSuggestBusy: boolean;
  voiceSuggestStatus: string;
  previewText: string;
  previewBusy: boolean;
  previewStatus: string;
  previewError: boolean;
  previewAudioRef: Ref<HTMLAudioElement>;
}

export interface AdvancedOptionsViewModel {
  speedInput: string;
  selectedQuantization: string;
  quantizationOptions: TermSelectOption[];
  // Hidden while the active engine offers no real precision alternatives
  // (a lone "default"/engine-default pair loads the identical model).
  showQuantization: boolean;
  manualParallelWorkerUpperBound: number;
  selectedParallelWorkerLimit: number;
  /** How many of those will actually run at once; the selection itself when not given. */
  usableParallelWorkerLimit?: number;
  parallelManualMax: string;
  parallelWorkerError: boolean;
  parallelWorkerStatus: string;
  parallelWorkerDetail: string;
}

interface PreflightCardViewModel {
  title: string;
  meta: string;
  author: string;
  sourceType: string;
  chapterCount?: number;
  chapterTitles?: string[];
  sizeLabel?: string;
  warnings: string[];
  sample: string;
  canConvert: boolean;
  blockingReason: string | null;
}

export interface QueueItemViewModel {
  id: string;
  title: string;
  meta?: string;
  statusText: string;
  statusClassName: string;
  /** Why the source failed or needs attention, shown in place of its details line. */
  error?: string;
  /** Chapters and a sample are still being read. */
  inspecting?: boolean;
  converting: boolean;
  selected: boolean;
  canMoveUp: boolean;
  canMoveDown: boolean;
  canRemove: boolean;
  finished?: boolean;
  /** Its start may have made a book: Start all skips it, and only its own Start retries it. */
  needsCheck?: boolean;
  /** Live progress while this item converts. */
  progressPct?: number;
  progressLabel?: string;
}

export type SourceInput = "file" | "paste";

export interface SourceQueueViewModel {
  isDocumentMode: boolean;
  sourceInput: SourceInput;
  dragging: boolean;
  pastedText: string;
  pastedTitle: string;
  pastedAuthor: string;
  pasteBusy: boolean;
  pasteStatus: string;
  queueItems: QueueItemViewModel[];
  /** A conversion running without a queue row, such as one resumed after a reload. */
  untrackedJob: { title: string; progressPct: number; progressLabel: string } | null;
  queueCount: number;
  queueRunning: boolean;
  queueStatus: string;
  formError: string;
  sourcePath: string;
  sourcePathPlaceholder: string;
  titleOverride: string;
  authorOverride: string;
  preflightCard: PreflightCardViewModel | null;
  preflightStatus: string;
  browserFileInputRef: Ref<HTMLInputElement>;
  browserFileAccept: string;
}

export interface ConversionActionsViewModel {
  /** Recovery only: the start request's outcome is unknown and can be re-checked. */
  showRefreshJob?: boolean;
  hasActiveJob: boolean;
  blockingReason: string | null;
  showSettingsShortcut: boolean;
  startLabel: string;
  /** What to say instead of "Converting…" while the running job cannot be observed. */
  runningStatus?: string;
  cancelLabel: string;
  /** The book Cancel would stop; set only when stopping it needs a confirmation. */
  cancelJobId?: string | null;
  isStarting: boolean;
}

interface ConversionPanelShellState {
  activeTab: DashboardTab | "config";
  hidden: boolean;
  isDocumentMode: boolean;
}

export interface ConversionPanelActions {
  onRetryRuntimeOptions: () => void;
  onOpenSettings: () => void;
  /** Queues a typed file path, the keyboard alternative to the picker. */
  onAddSourcePath: (path: string) => void;
  setTitleOverride: Dispatch<SetStateAction<string>>;
  setAuthorOverride: Dispatch<SetStateAction<string>>;
  setDragging: Dispatch<SetStateAction<boolean>>;
  setPastedText: Dispatch<SetStateAction<string>>;
  setSourceInput: (input: SourceInput) => void;
  setConversionMode: (mode: ConversionMode) => void;
  setPastedTitle: Dispatch<SetStateAction<string>>;
  setPastedAuthor: Dispatch<SetStateAction<string>>;
  setPreviewText: Dispatch<SetStateAction<string>>;
  setCleanerBackend: (value: string) => void;
  setCleanerModel: (value: string) => void;
  setCleanerCudaDevice: (value: string) => void;
  setCleanerApiKey: (value: string) => void;
  setSelectedEngine: (value: string) => void;
  setVoiceFilter: (value: string) => void;
  setSelectedVoice: (value: string) => void;
  setSpeed: (value: string) => void;
  setSelectedQuantization: (value: string) => void;
  setParallelManualMax: (value: string) => void;
  onBrowse: () => Promise<void>;
  onBrowserFilesSelected: (event: ChangeEvent<HTMLInputElement>) => void;
  onAddPastedText: () => Promise<ConversionQueueItem | null>;
  onDropZoneDrop: (event: DragEvent<HTMLDivElement>) => void;
  onInspectCurrentSource: () => void;
  onPreviewVoice: () => Promise<void>;
  onRecommendVoice: () => Promise<void>;
  onRunQueue: () => Promise<void>;
  onStartConversion: () => Promise<void>;
  onFetchCleanerModels: () => Promise<void>;
  onRefreshJob: () => Promise<void>;
  /**
   * clickCount is the pointer click count (event.detail). Repeat clicks, and any
   * activation right after Start put Cancel in its place, are ignored.
   */
  onCancelJob: (clickCount?: number) => Promise<void>;
  /** Whether a Cancel click is the user's own, not the tail of a Start gesture. */
  acceptsCancelClick: (clickCount?: number) => boolean;
  /** Stops the running book once the user has confirmed it. */
  onConfirmCancelJob: () => Promise<void>;
  onOpenConversionDetails?: () => void;
  clearFormError: () => void;
  selectQueueItem: (itemId: string) => void;
  moveQueueItem: (itemId: string, delta: number) => void;
  removeQueueItem: (itemId: string) => void;
  syncSelectedQueueEdits: (patch: QueueEditPatch) => void;
}

export interface ConversionPanelViewModel {
  shell: ConversionPanelShellState;
  capabilities: ConversionCapabilities;
  sourceQueue: SourceQueueViewModel;
  ttsOptions: TtsOptionsViewModel;
  cleanerOptions: CleanerOptionsViewModel;
  advancedOptions: AdvancedOptionsViewModel;
  conversionActions: ConversionActionsViewModel;
  actions: ConversionPanelActions;
}
