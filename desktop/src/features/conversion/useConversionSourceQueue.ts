import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import {
  type Dispatch,
  type DragEvent,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { ApiClient } from "../../api/client";
import type { ConversionMode } from "../../types/api";
import {
  compactSample,
  extractDroppedPaths,
  isTauriRuntime,
  splitPath,
} from "./conversionPanelHelpers";
import { createQueueItem, type ConversionQueueItem } from "./conversionPanelModel";
import type { QueueEditPatch } from "./conversionPanelStateTypes";
import type { ConversionJobController } from "./useConversionJob";

interface UseConversionSourceQueueArgs {
  apiClient: ApiClient | null;
  conversionMode: ConversionMode;
  conversionJob: ConversionJobController;
  runPreflightQueue: (
    items: ConversionQueueItem[],
    options?: { selectedItemId?: string | null },
  ) => Promise<void>;
  runPreflightForItem: (
    item: ConversionQueueItem,
    options?: { updateSelected?: boolean },
  ) => Promise<void>;
  setPreflightStatus: (status: string) => void;
  setPreviewText: (text: string) => void;
}

export function useConversionSourceQueue({
  apiClient,
  conversionMode,
  conversionJob,
  runPreflightQueue,
  runPreflightForItem,
  setPreflightStatus,
  setPreviewText,
}: UseConversionSourceQueueArgs) {
  const [sourcePath, setSourcePath] = useState("");
  const [sourceFileLabel, setSourceFileLabel] = useState("");
  const [titleOverride, setTitleOverride] = useState("");
  const [authorOverride, setAuthorOverride] = useState("");
  const [queueItems, setQueueItems] = useState<ConversionQueueItem[]>([]);
  const [selectedQueueId, setSelectedQueueId] = useState<string | null>(null);
  const [queueStatus, setQueueStatus] = useState("queue idle");
  const [pasteTitle, setPasteTitle] = useState("");
  const [pastedText, setPastedText] = useState("");
  const [pasteBusy, setPasteBusy] = useState(false);
  const [pasteStatus, setPasteStatus] = useState("paste idle");
  const [dragging, setDragging] = useState(false);

  const applySourcePathsRef = useRef<(paths: string[]) => void>(() => {});
  const hasSelectedSourceRef = useRef(false);

  const selectedQueueItem = useMemo(
    () => queueItems.find((item) => item.id === selectedQueueId) ?? null,
    [queueItems, selectedQueueId],
  );

  useEffect(() => {
    hasSelectedSourceRef.current = Boolean(selectedQueueId || sourcePath.trim());
  }, [selectedQueueId, sourcePath]);

  const updateQueueItem = useCallback(
    (itemId: string, updater: (item: ConversionQueueItem) => ConversionQueueItem) => {
      setQueueItems((current) =>
        current.map((item) => (item.id === itemId ? updater(item) : item)),
      );
    },
    [],
  );

  const applySelectedQueueItem = useCallback(
    (item: ConversionQueueItem) => {
      setSelectedQueueId(item.id);
      setSourcePath(item.sourcePath);
      setSourceFileLabel(`${item.label} selected`);
      setTitleOverride(item.title);
      setAuthorOverride(item.author);
      if (item.preflight?.sample) {
        setPreviewText(compactSample(item.preflight.sample, 220));
      }
      conversionJob.clearFormError();
    },
    [conversionJob, setPreviewText],
  );

  const addQueueItems = useCallback(
    (items: ConversionQueueItem[]) => {
      const nextItems = items.filter((item) => item.sourcePath.trim());
      if (nextItems.length === 0) {
        return;
      }

      setQueueItems((current) => [...current, ...nextItems]);
      setQueueStatus(`${nextItems.length} item${nextItems.length === 1 ? "" : "s"} added`);

      const shouldSelectFirst = !hasSelectedSourceRef.current;
      if (shouldSelectFirst) {
        applySelectedQueueItem(nextItems[0]);
      }

      void runPreflightQueue(nextItems, {
        selectedItemId: shouldSelectFirst ? nextItems[0].id : null,
      });
    },
    [applySelectedQueueItem, runPreflightQueue],
  );

  const applySourcePaths = useCallback(
    (paths: string[]) => {
      const items = paths
        .map((path) => path.trim())
        .filter(Boolean)
        .map((path) => createQueueItem(path, conversionMode));
      addQueueItems(items);
    },
    [addQueueItems, conversionMode],
  );

  useEffect(() => {
    applySourcePathsRef.current = applySourcePaths;
  }, [applySourcePaths]);

  useEffect(() => {
    if (!isTauriRuntime()) {
      return;
    }

    let mounted = true;
    const unlistenHandles: UnlistenFn[] = [];

    const applyDropPayload = (payload: unknown) => {
      const paths = extractDroppedPaths(payload);
      if (!mounted || paths.length === 0) {
        return;
      }
      setDragging(false);
      applySourcePathsRef.current(paths);
    };

    const register = async () => {
      try {
        const listeners = await Promise.all([
          listen<unknown>("tauri://file-drop", (event) => applyDropPayload(event.payload)),
          listen<unknown>("tauri://drag-drop", (event) => applyDropPayload(event.payload)),
          listen("tauri://drag-enter", () => {
            if (mounted) {
              setDragging(true);
            }
          }),
          listen("tauri://drag-over", () => {
            if (mounted) {
              setDragging(true);
            }
          }),
          listen("tauri://drag-leave", () => {
            if (mounted) {
              setDragging(false);
            }
          }),
        ]);
        unlistenHandles.push(...listeners);
      } catch {
        // Ignore event registration failures in non-desktop contexts.
      }
    };

    void register();

    return () => {
      mounted = false;
      for (const unlisten of unlistenHandles) {
        unlisten();
      }
    };
  }, []);

  const onBrowse = useCallback(async () => {
    if (isTauriRuntime()) {
      const selectedPaths = await invoke<string[]>("choose_source_files");
      if (Array.isArray(selectedPaths) && selectedPaths.length > 0) {
        applySourcePaths(selectedPaths);
      }
      return;
    }

    setPreflightStatus("Desktop file picker unavailable; launch the Tauri app.");
  }, [applySourcePaths, setPreflightStatus]);

  const onAddPastedText = useCallback(async () => {
    if (!apiClient) {
      setPasteStatus("Backend is not connected.");
      return;
    }

    const text = pastedText.trim();
    if (!text) {
      setPasteStatus("Paste text before adding.");
      return;
    }

    setPasteBusy(true);
    setPasteStatus("Creating markdown source...");
    try {
      const response = await apiClient.uploadSourceText({
        text,
        title: pasteTitle.trim() || null,
      });
      const shouldSelectUpload = !hasSelectedSourceRef.current;
      const item = createQueueItem(
        response.source_handle,
        conversionMode,
        response.original_name,
        response.preflight,
      );
      setQueueItems((current) => [...current, item]);
      setQueueStatus("1 item added");
      if (shouldSelectUpload) {
        applySelectedQueueItem(item);
      }
      if (response.preflight.title) {
        setTitleOverride((current) => current || response.preflight.title?.trim() || "");
      }
      if (response.preflight.author) {
        setAuthorOverride((current) => current || response.preflight.author?.trim() || "");
      }
      if (response.preflight.sample) {
        setPreviewText(compactSample(response.preflight.sample, 220));
      }
      setPastedText("");
      setPasteTitle("");
      setPasteStatus("Pasted markdown source added.");
      conversionJob.clearFormError();
    } catch (error: unknown) {
      setPasteStatus(error instanceof Error ? error.message : "paste upload failed");
    } finally {
      setPasteBusy(false);
    }
  }, [
    apiClient,
    applySelectedQueueItem,
    conversionJob,
    conversionMode,
    pasteTitle,
    pastedText,
    setPreviewText,
  ]);

  const onDropZoneDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragging(false);

      const maybePathList = event.dataTransfer.getData("text/uri-list");
      if (maybePathList) {
        const paths = maybePathList
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean)
          .map((line) => line.replace(/^file:\/\//, ""));
        if (paths.length > 0) {
          applySourcePaths(paths);
          return;
        }
      }

      const files = Array.from(event.dataTransfer.files ?? []);
      if (files.length === 0) {
        return;
      }

      const desktopPaths = files
        .map((file) => (file as File & { path?: string }).path)
        .filter((path): path is string => Boolean(path?.trim()));
      if (desktopPaths.length === files.length) {
        applySourcePaths(desktopPaths);
        return;
      }

      setPreflightStatus("Dropped files must come from the desktop runtime with native paths.");
    },
    [applySourcePaths, setPreflightStatus],
  );

  const syncSelectedQueueEdits = useCallback(
    (updates: QueueEditPatch) => {
      if (!selectedQueueId) {
        return;
      }
      updateQueueItem(selectedQueueId, (item) => ({
        ...item,
        ...updates,
        label: updates.sourcePath ? splitPath(updates.sourcePath) : item.label,
        preflight: updates.sourcePath ? null : item.preflight,
        status: updates.sourcePath ? "ready" : item.status,
        error: updates.sourcePath ? "" : item.error,
      }));
    },
    [selectedQueueId, updateQueueItem],
  );

  const onInspectCurrentSource = useCallback(() => {
    const normalized = sourcePath.trim();
    if (!normalized) {
      setPreflightStatus("Choose a source before preflight.");
      return;
    }

    if (selectedQueueItem && selectedQueueItem.sourcePath === normalized) {
      void runPreflightForItem(
        {
          ...selectedQueueItem,
          title: titleOverride,
          author: authorOverride,
          preflight: null,
        },
        { updateSelected: true },
      );
      return;
    }

    const item = createQueueItem(normalized, conversionMode);
    item.title = titleOverride.trim();
    item.author = authorOverride.trim();
    addQueueItems([item]);
  }, [
    addQueueItems,
    authorOverride,
    conversionMode,
    runPreflightForItem,
    selectedQueueItem,
    setPreflightStatus,
    sourcePath,
    titleOverride,
  ]);

  const removeQueueItem = useCallback(
    (itemId: string) => {
      setQueueItems((current) => {
        const next = current.filter((item) => item.id !== itemId);
        if (selectedQueueId === itemId) {
          const replacement = next[0] ?? null;
          if (replacement) {
            window.setTimeout(() => applySelectedQueueItem(replacement), 0);
          } else {
            setSelectedQueueId(null);
            setSourcePath("");
            setTitleOverride("");
            setAuthorOverride("");
            setSourceFileLabel("");
          }
        }
        return next;
      });
    },
    [applySelectedQueueItem, selectedQueueId],
  );

  const moveQueueItem = useCallback((itemId: string, delta: number) => {
    setQueueItems((current) => {
      const index = current.findIndex((item) => item.id === itemId);
      const nextIndex = index + delta;
      if (index < 0 || nextIndex < 0 || nextIndex >= current.length) {
        return current;
      }
      const next = [...current];
      const [item] = next.splice(index, 1);
      next.splice(nextIndex, 0, item);
      return next;
    });
  }, []);

  return {
    sourcePath,
    setSourcePath,
    sourceFileLabel,
    setSourceFileLabel,
    titleOverride,
    setTitleOverride: setTitleOverride as Dispatch<SetStateAction<string>>,
    authorOverride,
    setAuthorOverride: setAuthorOverride as Dispatch<SetStateAction<string>>,
    queueItems,
    setQueueItems,
    selectedQueueId,
    setSelectedQueueId,
    selectedQueueItem,
    queueStatus,
    setQueueStatus,
    pasteTitle,
    setPasteTitle,
    pastedText,
    setPastedText,
    pasteBusy,
    pasteStatus,
    dragging,
    setDragging,
    updateQueueItem,
    applySelectedQueueItem,
    moveQueueItem,
    removeQueueItem,
    syncSelectedQueueEdits,
    onBrowse,
    onAddPastedText,
    onDropZoneDrop,
    onInspectCurrentSource,
  };
}
