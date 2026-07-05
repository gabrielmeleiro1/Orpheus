import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";

import type { ApiClient } from "../../api/client";
import type { SourcePreflight } from "../../types/api";
import type { ConversionQueueItem } from "./conversionPanelModel";
import type { QueueEditPatch } from "./conversionPanelStateTypes";
import {
  conversionQueueReducer,
  createConversionQueueInitialState,
  getPreflightStamp,
  type ConversionQueueState,
  type PreflightStamp,
} from "./conversionQueueReducer";
import { isSilentAbort, runSourcePreflight } from "./preflightService";

interface UseConversionQueueControllerArgs {
  apiClient: ApiClient | null;
}

type PreflightService = (
  apiClient: ApiClient,
  sourcePath: string,
  signal: AbortSignal,
) => Promise<SourcePreflight>;

interface InFlightPreflight {
  stamp: PreflightStamp;
  controller: AbortController;
}

function resolveSetStateAction<T>(current: T, action: SetStateAction<T>): T {
  return typeof action === "function" ? (action as (value: T) => T)(current) : action;
}

export function useConversionQueueController({
  apiClient,
}: UseConversionQueueControllerArgs) {
  const [state, dispatch] = useReducer(
    conversionQueueReducer,
    undefined,
    createConversionQueueInitialState,
  );
  const stateRef = useRef<ConversionQueueState>(state);
  const inFlightPreflightsRef = useRef<Map<string, InFlightPreflight>>(new Map());
  const preflightServiceRef = useRef<PreflightService>(runSourcePreflight);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    return () => {
      for (const request of inFlightPreflightsRef.current.values()) {
        request.controller.abort();
      }
      inFlightPreflightsRef.current.clear();
    };
  }, []);

  const selectedQueueItem = useMemo(
    () => state.queueItems.find((item) => item.id === state.selectedQueueId) ?? null,
    [state.queueItems, state.selectedQueueId],
  );

  const abortPreflight = useCallback((itemId: string) => {
    const request = inFlightPreflightsRef.current.get(itemId);
    if (request) {
      request.controller.abort();
      inFlightPreflightsRef.current.delete(itemId);
    }
  }, []);

  const runPreflightForItem = useCallback(
    async (item: ConversionQueueItem, options?: { updateSelected?: boolean }) => {
      abortPreflight(item.id);

      if (!apiClient) {
        const stamp = getPreflightStamp(stateRef.current, item);
        dispatch({
          type: "preflightFailed",
          stamp,
          error: "Backend is not connected.",
        });
        return;
      }

      const stamp = getPreflightStamp(stateRef.current, item);
      const controller = new AbortController();
      inFlightPreflightsRef.current.set(item.id, { stamp, controller });
      dispatch({ type: "preflightStarted", stamp });

      try {
        const preflight =
          item.preflight ??
          (await preflightServiceRef.current(apiClient, item.sourcePath, controller.signal));
        dispatch({
          type: "preflightSucceeded",
          stamp,
          preflight,
          updateSelected: Boolean(options?.updateSelected),
        });
      } catch (error: unknown) {
        if (!isSilentAbort(error)) {
          dispatch({
            type: "preflightFailed",
            stamp,
            error: error instanceof Error ? error.message : "preflight failed",
          });
        }
      } finally {
        const current = inFlightPreflightsRef.current.get(item.id);
        if (current?.stamp.revision === stamp.revision) {
          inFlightPreflightsRef.current.delete(item.id);
        }
      }
    },
    [abortPreflight, apiClient],
  );

  const runPreflightQueue = useCallback(
    async (
      items: ConversionQueueItem[],
      options?: { selectedItemId?: string | null },
    ) => {
      for (const item of items) {
        await runPreflightForItem(item, {
          updateSelected: item.id === options?.selectedItemId,
        });
      }
    },
    [runPreflightForItem],
  );

  const addQueueItems = useCallback(
    (items: ConversionQueueItem[], options?: { selectFirst?: boolean }) => {
      const nextItems = items.filter((item) => item.sourcePath.trim());
      if (nextItems.length === 0) {
        return;
      }

      const shouldSelectFirst = Boolean(options?.selectFirst);
      dispatch({
        type: "itemsAdded",
        items: nextItems,
        selectFirst: shouldSelectFirst,
      });
      void runPreflightQueue(nextItems, {
        selectedItemId: shouldSelectFirst ? nextItems[0].id : null,
      });
    },
    [runPreflightQueue],
  );

  const addPastedSource = useCallback(
    (item: ConversionQueueItem, options?: { selectItem?: boolean }) => {
      dispatch({
        type: "pastedSourceAdded",
        item,
        selectItem: Boolean(options?.selectItem),
      });
    },
    [],
  );

  const applySelectedQueueItem = useCallback(
    (item: ConversionQueueItem) => {
      dispatch({ type: "selectedItemApplied", itemId: item.id });
    },
    [],
  );

  const moveQueueItem = useCallback((itemId: string, delta: number) => {
    dispatch({ type: "itemMoved", itemId, delta });
  }, []);

  const removeQueueItem = useCallback(
    (itemId: string) => {
      abortPreflight(itemId);
      dispatch({ type: "itemRemoved", itemId });
    },
    [abortPreflight],
  );

  const updateQueueItem = useCallback(
    (itemId: string, updater: (item: ConversionQueueItem) => ConversionQueueItem) => {
      dispatch({ type: "queueItemUpdated", itemId, updater });
    },
    [],
  );

  const syncSelectedQueueEdits = useCallback(
    (patch: QueueEditPatch) => {
      if (patch.sourcePath && selectedQueueItem) {
        abortPreflight(selectedQueueItem.id);
      }
      dispatch({ type: "selectedQueueItemEdited", patch });
    },
    [abortPreflight, selectedQueueItem],
  );

  const setQueueStatus = useCallback((status: string) => {
    dispatch({ type: "queueStatusSet", status });
  }, []);

  const setPreflightStatus = useCallback((status: string) => {
    dispatch({ type: "preflightStatusSet", status });
  }, []);

  const setSourcePath = useCallback<Dispatch<SetStateAction<string>>>((action) => {
    dispatch({
      type: "sourcePathSet",
      sourcePath: resolveSetStateAction(stateRef.current.sourcePath, action),
    });
  }, []);

  const setTitleOverride = useCallback<Dispatch<SetStateAction<string>>>((action) => {
    dispatch({ type: "titleOverrideSet", title: action });
  }, []);

  const setAuthorOverride = useCallback<Dispatch<SetStateAction<string>>>((action) => {
    dispatch({ type: "authorOverrideSet", author: action });
  }, []);

  return {
    ...state,
    selectedQueueItem,
    addQueueItems,
    addPastedSource,
    applySelectedQueueItem,
    moveQueueItem,
    removeQueueItem,
    updateQueueItem,
    syncSelectedQueueEdits,
    runPreflightForItem,
    setQueueStatus,
    setPreflightStatus,
    setSourcePath,
    setTitleOverride,
    setAuthorOverride,
  };
}
