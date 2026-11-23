import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";

import { useMountedRef } from "../../state/generationGuard";
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
import { readStoredQueue, writeStoredQueue } from "./conversionQueuePersistence";
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
  clientGeneration: number;
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
    () => createConversionQueueInitialState(readStoredQueue()),
  );
  const stateRef = useRef<ConversionQueueState>(state);
  const inFlightPreflightsRef = useRef<Map<string, InFlightPreflight>>(new Map());
  const preflightServiceRef = useRef<PreflightService>(runSourcePreflight);
  const clientContextRef = useRef({
    apiClient,
    generation: 0,
  });

  if (clientContextRef.current.apiClient !== apiClient) {
    clientContextRef.current = {
      apiClient,
      generation: clientContextRef.current.generation + 1,
    };
  }
  const clientGeneration = clientContextRef.current.generation;

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    writeStoredQueue(state.queueItems, state.selectedQueueId);
  }, [state.queueItems, state.selectedQueueId]);

  const mountedRef = useMountedRef();
  useEffect(() => () => {
    for (const request of inFlightPreflightsRef.current.values()) {
      request.controller.abort();
    }
    inFlightPreflightsRef.current.clear();
  }, []);

  useEffect(() => {
    for (const [itemId, request] of inFlightPreflightsRef.current) {
      if (request.clientGeneration !== clientGeneration) {
        request.controller.abort();
        inFlightPreflightsRef.current.delete(itemId);
      }
    }
  }, [clientGeneration]);

  const isCurrentClientGeneration = useCallback(
    (generation: number) =>
      mountedRef.current && clientContextRef.current.generation === generation,
    [],
  );

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
      if (!isCurrentClientGeneration(clientGeneration)) {
        return;
      }
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
      inFlightPreflightsRef.current.set(item.id, {
        stamp,
        controller,
        clientGeneration,
      });
      dispatch({ type: "preflightStarted", stamp });

      const isCurrentRequest = () => {
        const current = inFlightPreflightsRef.current.get(item.id);
        // Controller identity already pins the entry to this call, which
        // recorded `clientGeneration` when it was registered.
        return isCurrentClientGeneration(clientGeneration) && current?.controller === controller;
      };

      try {
        const preflight =
          item.preflight ??
          (await preflightServiceRef.current(apiClient, item.sourcePath, controller.signal));
        if (!isCurrentRequest()) {
          return;
        }
        dispatch({
          type: "preflightSucceeded",
          stamp,
          preflight,
          updateSelected: Boolean(options?.updateSelected),
        });
      } catch (error: unknown) {
        if (isCurrentRequest() && !isSilentAbort(error)) {
          dispatch({
            type: "preflightFailed",
            stamp,
            error: error instanceof Error ? error.message : "preflight failed",
          });
        }
      } finally {
        const current = inFlightPreflightsRef.current.get(item.id);
        if (current?.controller === controller) {
          inFlightPreflightsRef.current.delete(item.id);
        }
      }
    },
    [
      abortPreflight,
      apiClient,
      clientGeneration,
      isCurrentClientGeneration,
    ],
  );

  const runPreflightQueue = useCallback(
    async (
      items: ConversionQueueItem[],
      options?: { selectedItemId?: string | null },
    ) => {
      for (const item of items) {
        const inspection = runPreflightForItem(item, {
          updateSelected: item.id === options?.selectedItemId,
        });
        // A row that arrives already inspected settles at once. Yielding after
        // it would let its bookkeeping clear a notice the caller writes about
        // the whole batch right after adding it.
        if (!item.preflight) {
          await inspection;
        }
      }
    },
    [runPreflightForItem],
  );

  useEffect(() => {
    if (!apiClient) return;
    // Client replacement aborts the old requests and their queued callbacks.
    // Resume every unfinished inspection through the new client, in order.
    const pending = stateRef.current.queueItems.filter((item) => item.status === "preflighting");
    void runPreflightQueue(pending, { selectedItemId: stateRef.current.selectedQueueId });
  }, [apiClient, runPreflightQueue]);

  const addQueueItems = useCallback(
    (items: ConversionQueueItem[], options?: { selectFirst?: boolean; skipPreflight?: boolean }) => {
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
      if (!options?.skipPreflight) {
        void runPreflightQueue(nextItems, {
          selectedItemId: shouldSelectFirst ? nextItems[0].id : null,
        });
      }
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
    if (stateRef.current.queueItems.find((item) => item.id === itemId)?.status === "converting") return;
    dispatch({ type: "itemMoved", itemId, delta });
  }, []);

  // stateRef only catches up after a commit, so rows removed together are judged as one
  // batch: an upload is deleted once, and only when no row that stays still uses it.
  const removeQueueItems = useCallback(
    (itemIds: readonly string[]) => {
      const requested = new Set(itemIds);
      const removed = stateRef.current.queueItems.filter(
        (item) => requested.has(item.id) && item.status !== "converting",
      );
      if (removed.length === 0) return;
      const removedIds = new Set(removed.map((item) => item.id));
      for (const item of removed) {
        abortPreflight(item.id);
        dispatch({ type: "itemRemoved", itemId: item.id });
      }
      const remaining = stateRef.current.queueItems.filter((item) => !removedIds.has(item.id));
      const uploads = new Set(removed.map((item) => item.sourcePath).filter((path) => path.startsWith("upload:")));
      for (const upload of uploads) {
        if (remaining.some((candidate) => candidate.sourcePath === upload)) continue;
        void apiClient?.deleteUpload(upload).catch(() => {
          dispatch({ type: "queueStatusSet", status: "Item removed. Its upload could not be deleted yet." });
        });
      }
    },
    [abortPreflight, apiClient],
  );

  const removeQueueItem = useCallback(
    (itemId: string) => removeQueueItems([itemId]),
    [removeQueueItems],
  );

  const updateQueueItem = useCallback(
    (itemId: string, updater: (item: ConversionQueueItem) => ConversionQueueItem) => {
      dispatch({ type: "queueItemUpdated", itemId, updater });
    },
    [],
  );

  const syncSelectedQueueEdits = useCallback(
    (patch: QueueEditPatch) => {
      if (
        patch.sourcePath !== undefined &&
        selectedQueueItem &&
        patch.sourcePath !== selectedQueueItem.sourcePath
      ) {
        abortPreflight(selectedQueueItem.id);
      }
      dispatch({ type: "selectedQueueItemEdited", patch });
    },
    [abortPreflight, selectedQueueItem],
  );

  const noteJobBook = useCallback((jobId: string, bookId: number, completed: boolean) => {
    dispatch({ type: "jobBookObserved", jobId, bookId, completed });
  }, []);

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
    removeQueueItems,
    updateQueueItem,
    noteJobBook,
    syncSelectedQueueEdits,
    runPreflightForItem,
    setQueueStatus,
    setPreflightStatus,
    setSourcePath,
    setTitleOverride,
    setAuthorOverride,
  };
}
