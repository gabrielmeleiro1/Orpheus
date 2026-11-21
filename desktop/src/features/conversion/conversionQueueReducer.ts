import type { SourcePreflight } from "../../types/api";
import { compactSample, splitPath } from "./conversionPanelHelpers";
import {
  preflightBlockingReason,
  type ConversionQueueItem,
} from "./conversionPanelModel";
import type { QueueEditPatch } from "./conversionPanelStateTypes";

export interface PreflightStamp {
  itemId: string;
  sourcePath: string;
  revision: number;
}

export interface ConversionQueueState {
  sourcePath: string;
  titleOverride: string;
  authorOverride: string;
  queueItems: ConversionQueueItem[];
  selectedQueueId: string | null;
  queueStatus: string;
  preflightStatus: string;
  previewText: string | null;
  itemRevisions: Record<string, number>;
}

type ConversionQueueAction =
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
  | { type: "jobBookObserved"; jobId: string; bookId: number; completed: boolean }
  | { type: "clearSourceSelection" };

export function createConversionQueueInitialState(
  restored?: { items: ConversionQueueItem[]; selectedQueueId: string | null },
): ConversionQueueState {
  const state: ConversionQueueState = {
    sourcePath: "",
    titleOverride: "",
    authorOverride: "",
    queueItems: restored?.items ?? [],
    selectedQueueId: null,
    queueStatus: "",
    preflightStatus: "",
    previewText: null,
    itemRevisions: {},
  };
  const selected = restored?.items.find((item) => item.id === restored.selectedQueueId) ?? null;
  return selected ? applySelectedItem(state, selected) : state;
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
      titleOverride: "",
      authorOverride: "",
    };
  }

  return {
    ...state,
    selectedQueueId: item.id,
    sourcePath: item.sourcePath,
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
        queueStatus: "",
      };
      return action.selectFirst ? applySelectedItem(nextState, items[0]) : nextState;
    }
    case "pastedSourceAdded": {
      let nextState: ConversionQueueState = {
        ...state,
        queueItems: [...state.queueItems, action.item],
        queueStatus: "",
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
      const selectedItem = state.queueItems.find(
        (item) => item.id === state.selectedQueueId,
      );
      const selectedSourceChanged =
        action.patch.sourcePath !== undefined &&
        action.patch.sourcePath !== selectedItem?.sourcePath;
      if (
        selectedSourceChanged &&
        (selectedItem?.status === "done" || selectedItem?.status === "converting")
      ) {
        return {
          ...state,
          selectedQueueId: null,
          sourcePath: action.patch.sourcePath ?? state.sourcePath,
          titleOverride: action.patch.title ?? state.titleOverride,
          authorOverride: action.patch.author ?? state.authorOverride,
          preflightStatus: "",
          previewText: null,
        };
      }
      let changedSourcePath = false;
      const queueItems = state.queueItems.map((item) => {
        if (item.id !== state.selectedQueueId) {
          return item;
        }
        // A row that converted (or is converting) records what that job ran
        // with; edits meant for the next source must not rewrite its history.
        if (item.status === "done" || item.status === "converting") {
          return item;
        }
        changedSourcePath =
          action.patch.sourcePath !== undefined &&
          action.patch.sourcePath !== item.sourcePath;
        return {
          ...item,
          ...action.patch,
          label: action.patch.sourcePath ? splitPath(action.patch.sourcePath) : item.label,
          preflight: changedSourcePath ? null : item.preflight,
          status: changedSourcePath ? "ready" : item.status,
          error: changedSourcePath ? "" : item.error,
          startUncertain: changedSourcePath ? false : item.startUncertain,
        };
      });
      return {
        ...state,
        queueItems,
        sourcePath: action.patch.sourcePath ?? state.sourcePath,
        titleOverride: action.patch.title ?? state.titleOverride,
        authorOverride: action.patch.author ?? state.authorOverride,
        preflightStatus: changedSourcePath ? "" : state.preflightStatus,
        previewText: changedSourcePath ? null : state.previewText,
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
        preflightStatus: "",
        queueItems: state.queueItems.map((item) =>
          item.id === action.stamp.itemId && hasFreshStamp(state, item, action.stamp) && item.status !== "converting" && item.status !== "done"
            ? { ...item, status: "preflighting", error: "", startUncertain: false }
            : item,
        ),
      };
    case "preflightSucceeded": {
      let applied = false;
      const updateSelected = action.updateSelected && state.selectedQueueId === action.stamp.itemId;
      let nextPreviewText = state.previewText;
      const preflightError = preflightBlockingReason(action.preflight);
      const queueItems = state.queueItems.map((item) => {
        if (item.id !== action.stamp.itemId || !hasFreshStamp(state, item, action.stamp)) {
          return item;
        }
        applied = true;
        const nextTitle = item.title || action.preflight.title?.trim() || "";
        const nextAuthor = item.author || action.preflight.author?.trim() || "";
        const preserveActiveStatus =
          item.status === "converting" || item.status === "done" || item.status === "failed";
        if (updateSelected && action.preflight.sample) {
          nextPreviewText = compactSample(action.preflight.sample, 220);
        }
        return {
          ...item,
          title: nextTitle,
          author: nextAuthor,
          preflight: action.preflight,
          status: preserveActiveStatus
            ? item.status
            : preflightError
              ? "failed"
              : action.preflight.warnings.length
                ? "warning"
                : "ready",
          error: preserveActiveStatus
            ? item.error
            : preflightError
              ? preflightError
              : "",
        };
      });
      if (!applied) {
        return state;
      }
      return {
        ...state,
        queueItems,
        selectedQueueId: updateSelected ? action.stamp.itemId : state.selectedQueueId,
        sourcePath: updateSelected ? action.stamp.sourcePath : state.sourcePath,
        titleOverride: updateSelected
          ? state.titleOverride || action.preflight.title?.trim() || ""
          : state.titleOverride,
        authorOverride: updateSelected
          ? state.authorOverride || action.preflight.author?.trim() || ""
          : state.authorOverride,
        previewText: nextPreviewText,
        // The row's status and details report the outcome; no sentence needed.
        preflightStatus: "",
      };
    }
    case "preflightFailed": {
      let applied = false;
      const queueItems = state.queueItems.map((item) => {
        if (item.id !== action.stamp.itemId || !hasFreshStamp(state, item, action.stamp)) {
          return item;
        }
        applied = true;
        return item.status === "converting" || item.status === "done" ? item : { ...item, status: "failed" as const, error: action.error };
      });
      if (!applied) return state;
      // A failed row shows its own error; a finished row keeps its history, so
      // only then does the intake line report the failure.
      const target = queueItems.find((item) => item.id === action.stamp.itemId);
      return { ...state, queueItems, preflightStatus: target?.status === "failed" ? "" : action.error };
    }
    case "jobBookObserved": {
      let changed = false;
      const queueItems = state.queueItems.map((item) => {
        if (item.jobId === action.jobId) {
          // The row lost sight of its own job, which has now been seen to finish.
          const confirmed = action.completed && item.status === "warning";
          if (item.bookId === action.bookId && !confirmed) return item;
          changed = true;
          return confirmed
            ? { ...item, bookId: action.bookId, status: "done" as const, error: "" }
            : { ...item, bookId: action.bookId };
        }
        // Another job (a repair started from Library) finished this row's book,
        // so the row's earlier stop or failure no longer describes the book.
        if (
          action.completed && item.jobId !== null && item.bookId === action.bookId &&
          (item.status === "failed" || item.status === "warning")
        ) {
          changed = true;
          return { ...item, status: "done" as const, error: "" };
        }
        return item;
      });
      return changed ? { ...state, queueItems } : state;
    }
    case "clearSourceSelection":
      return applySelectedItem(state, null);
    default:
      return state;
  }
}
