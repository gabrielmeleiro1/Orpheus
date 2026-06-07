import type { Dispatch, DragEvent, Ref, SetStateAction } from "react";

import type { ApiClient } from "../../api/client";
import type {
  BackendConnectionState,
  DashboardTab,
  HealthResponse,
  SourcePreflight,
} from "../../types/api";
import type { ConversionJobController } from "./useConversionJob";
import type { useConversionOptions } from "./useConversionOptions";
import type { ConversionQueueItem } from "./conversionPanelModel";

export interface UseConversionPanelStateArgs {
  activeTab: DashboardTab | "config";
  hidden: boolean;
  apiClient: ApiClient | null;
  connectionState: BackendConnectionState;
  health: HealthResponse | null;
  statusMessage: string;
  conversionJob: ConversionJobController;
}

export type QueueEditPatch = Partial<
  Pick<ConversionQueueItem, "sourcePath" | "title" | "author">
>;

export interface ConversionPanelShellState {
  activeTab: DashboardTab | "config";
  hidden: boolean;
  apiClient: ApiClient | null;
  connectionState: BackendConnectionState;
  health: HealthResponse | null;
  statusMessage: string;
  conversionJob: ConversionJobController;
  optionState: ReturnType<typeof useConversionOptions>;
  isDocumentMode: boolean;
  sourcePathPlaceholder: string;
  startDisabled: boolean;
  cancelDisabled: boolean;
  mergedFormError: string;
  runtimeStatusClass: string;
}

export interface ConversionPanelFormState {
  sourcePath: string;
  sourceFileLabel: string;
  titleOverride: string;
  authorOverride: string;
  selectedPreflight: SourcePreflight | null;
  dragging: boolean;
  pasteTitle: string;
  pastedText: string;
  pasteBusy: boolean;
  pasteStatus: string;
  voiceSuggestBusy: boolean;
  voiceSuggestStatus: string;
  preflightStatus: string;
  previewText: string;
  previewBusy: boolean;
  previewStatus: string;
  previewError: boolean;
  previewAudioRef: Ref<HTMLAudioElement>;
}

export interface ConversionPanelQueueState {
  queueItems: ConversionQueueItem[];
  selectedQueueId: string | null;
  queueRunning: boolean;
  queueStatus: string;
}

export interface ConversionPanelActions {
  setSourcePath: Dispatch<SetStateAction<string>>;
  setTitleOverride: Dispatch<SetStateAction<string>>;
  setAuthorOverride: Dispatch<SetStateAction<string>>;
  setDragging: Dispatch<SetStateAction<boolean>>;
  setPasteTitle: Dispatch<SetStateAction<string>>;
  setPastedText: Dispatch<SetStateAction<string>>;
  setPreviewText: Dispatch<SetStateAction<string>>;
  onBrowse: () => Promise<void>;
  onAddPastedText: () => Promise<void>;
  onDropZoneDrop: (event: DragEvent<HTMLDivElement>) => void;
  onInspectCurrentSource: () => void;
  onPreviewVoice: () => Promise<void>;
  onRecommendVoice: () => Promise<void>;
  onRunQueue: () => Promise<void>;
  onStartConversion: () => Promise<void>;
  applySelectedQueueItem: (item: ConversionQueueItem) => void;
  moveQueueItem: (itemId: string, delta: number) => void;
  removeQueueItem: (itemId: string) => void;
  syncSelectedQueueEdits: (patch: QueueEditPatch) => void;
}

export interface ConversionPanelViewModel {
  shell: ConversionPanelShellState;
  form: ConversionPanelFormState;
  queue: ConversionPanelQueueState;
  actions: ConversionPanelActions;
}
