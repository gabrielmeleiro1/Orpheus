import type { SourcePreflight } from "../../types/api";
import { compactSample, splitPath } from "./conversionPanelHelpers";
import type { ConversionQueueItem } from "./conversionPanelModel";
import type { QueueEditPatch } from "./conversionPanelStateTypes";

export interface PreflightStamp {
  itemId: string;
  sourcePath: string;
  revision: number;
}

export interface ConversionQueueState {
  sourcePath: string;
  sourceFileLabel: string;
  titleOverride: string;
  authorOverride: string;
  queueItems: ConversionQueueItem[];
  selectedQueueId: string | null;
  queueStatus: string;
  preflightStatus: string;
  previewText: string | null;
  itemRevisions: Record<string, number>;
}

export type ConversionQueueAction =
  | { type: "sourcePathSet"; sourcePath: string }
  | { type: "titleOverrideSet"; title: string | ((current: string) => string) }
  | { type: "authorOverrideSet"; author: string | ((current: string) => string) }
  | { type: "itemsAdded"; items: ConversionQueueItem[]; selectFirst: boolean }
  | { type: "selectedItemApplied"; itemId: string }
  | { type: "itemMoved"; itemId: string; delta: number }
  | { type: "itemRemoved"; itemId: string }
  | { type: "queueItemUpdated"; itemId: string; updater: (item: ConversionQueueItem) => ConversionQueueItem }
  | { type: "selectedQueueItemEdited"; patch: QueueEditPatch }
  | { type: "queueStatusSet"; status: string }
  | { type: "preflightStatusSet"; status: string }
  | { type: "preflightStarted"; stamp: PreflightStamp }
  | {
      type: "preflightSucceeded";
      stamp: PreflightStamp;
      preflight: SourcePreflight;
      updateSelected: boolean;
    }
  | { type: "preflightFailed"; stamp: PreflightStamp; error: string }
  | { type: "pastedSourceAdded"; item: ConversionQueueItem; selectItem: boolean }
  | { type: "clearSourceSelection" };

export function createConversionQueueInitialState(): ConversionQueueState {
  return {
    sourcePath: "",
    sourceFileLabel: "",
    titleOverride: "",
    authorOverride: "",
    queueItems: [],
    selectedQueueId: null,
    queueStatus: "queue idle",
    preflightStatus: "",
    previewText: null,
    itemRevisions: {},
  };
}

export function getPreflightStamp(
  state: ConversionQueueState,
  item: ConversionQueueItem,
): PreflightStamp {
  return {
    itemId: item.id,
    sourcePath: item.sourcePath,
    revision: state.itemRevisions[item.id] ?? 0,
  };
}

function resolveValue(current: string, value: string | ((current: string) => string)): string {
  return typeof value === "function" ? value(current) : value;
}

function applySelectedItem(
  state: ConversionQueueState,
  item: ConversionQueueItem | null,
): ConversionQueueState {
  if (!item) {
    return {
      ...state,
      selectedQueueId: null,
      sourcePath: "",
      sourceFileLabel: "",
      titleOverride: "",
      authorOverride: "",
    };
  }

  return {
    ...state,
    selectedQueueId: item.id,
    sourcePath: item.sourcePath,
    sourceFileLabel: `${item.label} selected`,
    titleOverride: item.title,
    authorOverride: item.author,
    previewText: item.preflight?.sample ? compactSample(item.preflight.sample, 220) : state.previewText,
  };
}

function hasFreshStamp(
  state: ConversionQueueState,
  item: ConversionQueueItem,
  stamp: PreflightStamp,
): boolean {
  return (
    item.sourcePath === stamp.sourcePath &&
    (state.itemRevisions[item.id] ?? 0) === stamp.revision
  );
}

function bumpRevision(
  revisions: Record<string, number>,
  itemId: string,
): Record<string, number> {
  return {
    ...revisions,
    [itemId]: (revisions[itemId] ?? 0) + 1,
  };
}

function pruneRevision(
  revisions: Record<string, number>,
  itemId: string,
): Record<string, number> {
  const next = { ...revisions };
  delete next[itemId];
  return next;
}

export function conversionQueueReducer(
  state: ConversionQueueState,
  action: ConversionQueueAction,
): ConversionQueueState {
  switch (action.type) {
    case "sourcePathSet":
      return { ...state, sourcePath: action.sourcePath };
    case "titleOverrideSet":
      return { ...state, titleOverride: resolveValue(state.titleOverride, action.title) };
    case "authorOverrideSet":
      return { ...state, authorOverride: resolveValue(state.authorOverride, action.author) };
    case "itemsAdded": {
      const items = action.items.filter((item) => item.sourcePath.trim());
      if (items.length === 0) {
        return state;
      }
      const nextState = {
        ...state,
        queueItems: [...state.queueItems, ...items],
        queueStatus: `${items.length} item${items.length === 1 ? "" : "s"} added`,
      };
      return action.selectFirst ? applySelectedItem(nextState, items[0]) : nextState;
    }
    case "pastedSourceAdded": {
      let nextState: ConversionQueueState = {
        ...state,
        queueItems: [...state.queueItems, action.item],
        queueStatus: "1 item added",
        titleOverride: action.item.preflight?.title
          ? state.titleOverride || action.item.preflight.title.trim()
          : state.titleOverride,
        authorOverride: action.item.preflight?.author
          ? state.authorOverride || action.item.preflight.author.trim()
          : state.authorOverride,
        previewText: action.item.preflight?.sample
          ? compactSample(action.item.preflight.sample, 220)
          : state.previewText,
      };
      if (action.selectItem) {
        nextState = applySelectedItem(nextState, action.item);
      }
      return nextState;
    }
    case "selectedItemApplied": {
      const item = state.queueItems.find((queueItem) => queueItem.id === action.itemId) ?? null;
      return applySelectedItem(state, item);
    }
    case "itemMoved": {
      const index = state.queueItems.findIndex((item) => item.id === action.itemId);
      const nextIndex = index + action.delta;
      if (index < 0 || nextIndex < 0 || nextIndex >= state.queueItems.length) {
        return state;
      }
      const queueItems = [...state.queueItems];
      const [item] = queueItems.splice(index, 1);
      queueItems.splice(nextIndex, 0, item);
      return { ...state, queueItems };
    }
    case "itemRemoved": {
      const queueItems = state.queueItems.filter((item) => item.id !== action.itemId);
      const nextState = {
        ...state,
        queueItems,
        itemRevisions: pruneRevision(state.itemRevisions, action.itemId),
      };
      if (state.selectedQueueId !== action.itemId) {
        return nextState;
      }
      return applySelectedItem(nextState, queueItems[0] ?? null);
    }
    case "queueItemUpdated": {
      return {
        ...state,
        queueItems: state.queueItems.map((item) =>
          item.id === action.itemId ? action.updater(item) : item,
        ),
      };
    }
    case "selectedQueueItemEdited": {
      if (!state.selectedQueueId) {
        return state;
      }
      let changedSourcePath = false;
      const queueItems = state.queueItems.map((item) => {
        if (item.id !== state.selectedQueueId) {
          return item;
        }
        changedSourcePath = Boolean(action.patch.sourcePath && action.patch.sourcePath !== item.sourcePath);
        return {
          ...item,
          ...action.patch,
          label: action.patch.sourcePath ? splitPath(action.patch.sourcePath) : item.label,
          preflight: action.patch.sourcePath ? null : item.preflight,
          status: action.patch.sourcePath ? "ready" : item.status,
          error: action.patch.sourcePath ? "" : item.error,
        };
      });
      return {
        ...state,
        queueItems,
        sourcePath: action.patch.sourcePath ?? state.sourcePath,
        titleOverride: action.patch.title ?? state.titleOverride,
        authorOverride: action.patch.author ?? state.authorOverride,
        itemRevisions: changedSourcePath
          ? bumpRevision(state.itemRevisions, state.selectedQueueId)
          : state.itemRevisions,
      };
    }
    case "queueStatusSet":
      return { ...state, queueStatus: action.status };
    case "preflightStatusSet":
      return { ...state, preflightStatus: action.status };
    case "preflightStarted":
      return {
        ...state,
        preflightStatus: `Inspecting ${splitPath(action.stamp.sourcePath)}...`,
        queueItems: state.queueItems.map((item) =>
          item.id === action.stamp.itemId && hasFreshStamp(state, item, action.stamp)
            ? { ...item, status: "preflighting", error: "" }
            : item,
        ),
      };
    case "preflightSucceeded": {
      let applied = false;
      let nextPreviewText = state.previewText;
      const queueItems = state.queueItems.map((item) => {
        if (item.id !== action.stamp.itemId || !hasFreshStamp(state, item, action.stamp)) {
          return item;
        }
        applied = true;
        const nextTitle = item.title || action.preflight.title?.trim() || "";
        const nextAuthor = item.author || action.preflight.author?.trim() || "";
        const preserveActiveStatus =
          item.status === "converting" || item.status === "done" || item.status === "failed";
        if (action.updateSelected && action.preflight.sample) {
          nextPreviewText = compactSample(action.preflight.sample, 220);
        }
        return {
          ...item,
          title: nextTitle,
          author: nextAuthor,
          preflight: action.preflight,
          status: preserveActiveStatus
            ? item.status
            : action.preflight.warnings.length
              ? "warning"
              : "ready",
          error: preserveActiveStatus ? item.error : "",
        };
      });
      if (!applied) {
        return state;
      }
      return {
        ...state,
        queueItems,
        selectedQueueId: action.updateSelected ? action.stamp.itemId : state.selectedQueueId,
        sourcePath: action.updateSelected ? action.stamp.sourcePath : state.sourcePath,
        sourceFileLabel: action.updateSelected
          ? `${splitPath(action.stamp.sourcePath)} selected`
          : state.sourceFileLabel,
        titleOverride: action.updateSelected
          ? state.titleOverride || action.preflight.title?.trim() || ""
          : state.titleOverride,
        authorOverride: action.updateSelected
          ? state.authorOverride || action.preflight.author?.trim() || ""
          : state.authorOverride,
        previewText: nextPreviewText,
        preflightStatus: action.preflight.warnings.length
          ? "Preflight completed with warnings."
          : "Preflight completed.",
      };
    }
    case "preflightFailed": {
      let applied = false;
      const queueItems = state.queueItems.map((item) => {
        if (item.id !== action.stamp.itemId || !hasFreshStamp(state, item, action.stamp)) {
          return item;
        }
        applied = true;
        return { ...item, status: "failed" as const, error: action.error };
      });
      return applied ? { ...state, queueItems, preflightStatus: action.error } : state;
    }
    case "clearSourceSelection":
      return applySelectedItem(state, null);
    default:
      return state;
  }
}
