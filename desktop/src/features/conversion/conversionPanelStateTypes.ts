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
  DashboardTab,
  HealthResponse,
  TermSelectOption,
} from "../../types/api";
import type { ConversionJobController } from "./useConversionJob";
import type { ConversionQueueItem } from "./conversionPanelModel";

export interface UseConversionPanelStateArgs {
  activeTab: DashboardTab | "config";
  hidden: boolean;
  apiClient: ApiClient | null;
  connectionState: BackendConnectionState;
  health: HealthResponse | null;
  statusMessage: string;
  conversionJob: ConversionJobController;
  onOpenSettings: () => void;
  onOpenRuntimeSetup: () => void;
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
  canRunQueue: boolean;
  canStartConversion: boolean;
  canCancelJob: boolean;
  canRefreshJob: boolean;
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
  cleanerCudaQuantization: string;
  cleanerCudaQuantizationOptions: TermSelectOption[];
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
  warnings: string[];
  sample: string;
}

interface QueueItemViewModel {
  id: string;
  title: string;
  statusText: string;
  statusClassName: string;
  converting: boolean;
  selected: boolean;
  canMoveUp: boolean;
  canMoveDown: boolean;
  canRemove: boolean;
}

export interface SourceQueueViewModel {
  isDocumentMode: boolean;
  dragging: boolean;
  sourceFileName: string;
  sourceFileDir: string;
  pastedText: string;
  pastedTitle: string;
  pastedAuthor: string;
  pasteBusy: boolean;
  pasteStatus: string;
  queueItems: QueueItemViewModel[];
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
  hasActiveJob: boolean;
  blockingReason: string | null;
  showSettingsShortcut: boolean;
  startLabel: string;
  cancelLabel: string;
  isStarting: boolean;
}

interface ConversionPanelShellState {
  activeTab: DashboardTab | "config";
  hidden: boolean;
  isDocumentMode: boolean;
}

export interface ConversionPanelActions {
  onOpenSettings: () => void;
  onOpenRuntimeSetup: () => void;
  setSourcePath: Dispatch<SetStateAction<string>>;
  setTitleOverride: Dispatch<SetStateAction<string>>;
  setAuthorOverride: Dispatch<SetStateAction<string>>;
  setDragging: Dispatch<SetStateAction<boolean>>;
  setPastedText: Dispatch<SetStateAction<string>>;
  setPastedTitle: Dispatch<SetStateAction<string>>;
  setPastedAuthor: Dispatch<SetStateAction<string>>;
  setPreviewText: Dispatch<SetStateAction<string>>;
  setCleanerBackend: (value: string) => void;
  setCleanerModel: (value: string) => void;
  setCleanerCudaDevice: (value: string) => void;
  setCleanerCudaQuantization: (value: string) => void;
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
  onCancelJob: () => Promise<void>;
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
