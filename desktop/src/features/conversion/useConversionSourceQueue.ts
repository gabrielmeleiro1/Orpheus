import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import {
  type DragEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
} from "react";

import type { ApiClient } from "../../api/client";
import type { ConversionMode } from "../../types/api";
import { extractDroppedPaths, pathsFromUriList } from "./conversionPanelHelpers";
import { isTauriRuntime } from "../../platform/tauriRuntime";
import { createQueueItem, type ConversionQueueItem } from "./conversionPanelModel";
import type { ConversionJobController } from "./useConversionJob";
import type { useConversionQueueController } from "./useConversionQueueController";

const MAX_CONCURRENT_UPLOADS = 2;

type ConversionQueueController = ReturnType<typeof useConversionQueueController>;

interface UseConversionSourceQueueArgs {
  apiClient: ApiClient | null;
  conversionMode: ConversionMode;
  conversionJob: ConversionJobController;
  queueController: ConversionQueueController;
}

export function useConversionSourceQueue({
  apiClient,
  conversionMode,
  conversionJob,
  queueController,
}: UseConversionSourceQueueArgs) {
  const [pastedText, setPastedText] = useState("");
  const [pastedTitle, setPastedTitle] = useState("");
  const [pastedAuthor, setPastedAuthor] = useState("");
  const [pasteBusy, setPasteBusy] = useState(false);
  const [pasteStatus, setPasteStatus] = useState("Idle");
  const [dragging, setDragging] = useState(false);

  const applySourcePathsRef = useRef<(paths: string[]) => void>(() => {});
  const hasSelectedSourceRef = useRef(false);
  const browserFileInputRef = useRef<HTMLInputElement>(null);
  const browserUploadBatchRef = useRef(0);
  const uploadSlotsRef = useRef({ active: 0, waiting: [] as Array<() => void> });
  const mountedRef = useRef(false);
  const pasteInFlightRef = useRef(false);
  const queueControllerRef = useRef(queueController);
  const conversionJobRef = useRef(conversionJob);
  const sourceContextRef = useRef({
    apiClient,
    conversionMode,
    generation: 0,
  });

  queueControllerRef.current = queueController;
  conversionJobRef.current = conversionJob;
  if (
    sourceContextRef.current.apiClient !== apiClient ||
    sourceContextRef.current.conversionMode !== conversionMode
  ) {
    sourceContextRef.current = {
      apiClient,
      conversionMode,
      generation: sourceContextRef.current.generation + 1,
    };
    pasteInFlightRef.current = false;
  }
  const sourceContextGeneration = sourceContextRef.current.generation;

  // Run only on mount/unmount: rerunning during a paste could let its completion
  // clear a newer paste's guard. Leave the generation unchanged so StrictMode's
  // effect replay keeps existing callbacks valid.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      pasteInFlightRef.current = false;
    };
  }, []);

  useEffect(() => {
    setPasteBusy(false);
  }, [apiClient, conversionMode]);

  const isCurrentSourceContext = useCallback(
    (generation: number) =>
      mountedRef.current && sourceContextRef.current.generation === generation,
    [],
  );

  useEffect(() => {
    hasSelectedSourceRef.current = Boolean(
      queueController.selectedQueueId || queueController.sourcePath.trim(),
    );
  }, [queueController.selectedQueueId, queueController.sourcePath]);

  const applySourcePaths = useCallback(
    (paths: string[]) => {
      const items = paths
        .map((path) => path.trim())
        .filter(Boolean)
        .map((path) => createQueueItem(path, conversionMode));
      const selectFirst = !hasSelectedSourceRef.current;
      if (items.length > 0 && selectFirst) {
        hasSelectedSourceRef.current = true;
      }
      queueControllerRef.current.addQueueItems(items, {
        selectFirst,
      });
      if (items.length > 0) {
        conversionJobRef.current.clearFormError();
      }
    },
    [conversionMode],
  );

  applySourcePathsRef.current = applySourcePaths;

  useEffect(() => {
    if (!isTauriRuntime()) {
      return;
    }

    let mounted = true;
    const unlistenHandles = new Set<UnlistenFn>();

    const applyDropPayload = (payload: unknown) => {
      const paths = extractDroppedPaths(payload);
      if (!mounted || paths.length === 0) {
        return;
      }
      setDragging(false);
      applySourcePathsRef.current(paths);
    };

    const register = async (eventName: string, handler: Parameters<typeof listen>[1]) => {
      try {
        const unlisten = await listen(eventName, handler);
        if (!mounted) {
          unlisten();
          return;
        }
        unlistenHandles.add(unlisten);
      } catch {
        // Ignore event registration failures in non-desktop contexts.
      }
    };

    void register("tauri://file-drop", (event) => applyDropPayload(event.payload));
    void register("tauri://drag-drop", (event) => applyDropPayload(event.payload));
    void register("tauri://drag-enter", () => {
      if (mounted) {
        setDragging(true);
      }
    });
    void register("tauri://drag-over", () => {
      if (mounted) {
        setDragging(true);
      }
    });
    void register("tauri://drag-leave", () => {
      if (mounted) {
        setDragging(false);
      }
    });

    return () => {
      mounted = false;
      for (const unlisten of unlistenHandles) {
        unlistenHandles.delete(unlisten);
        unlisten();
      }
    };
  }, []);

  const onBrowse = useCallback(async () => {
    if (!isCurrentSourceContext(sourceContextGeneration)) {
      return;
    }

    if (isTauriRuntime()) {
      try {
        const selectedPaths = await invoke<string[]>("choose_source_files");
        if (
          isCurrentSourceContext(sourceContextGeneration) &&
          Array.isArray(selectedPaths) &&
          selectedPaths.length > 0
        ) {
          applySourcePathsRef.current(selectedPaths);
        }
      } catch (error: unknown) {
        if (!isCurrentSourceContext(sourceContextGeneration)) {
          return;
        }
        const detail = error instanceof Error ? error.message : String(error || "unknown error");
        queueControllerRef.current.setPreflightStatus(`Could not open file picker: ${detail}`);
      }
      return;
    }

    browserFileInputRef.current?.click();
  }, [isCurrentSourceContext, sourceContextGeneration]);

  const onBrowserFilesSelected = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files ?? []);
      event.target.value = "";
      if (files.length === 0) {
        return;
      }
      if (!isCurrentSourceContext(sourceContextGeneration)) {
        return;
      }
      if (!apiClient) {
        queueControllerRef.current.setPreflightStatus("Backend is not connected.");
        return;
      }

      const batchId = ++browserUploadBatchRef.current;
      queueControllerRef.current.setPreflightStatus(`Uploading ${files.length === 1 ? files[0].name : `${files.length} sources`}...`);
      // Share slots across overlapping selections so a second batch cannot
      // multiply backend preflight memory. Results retain the selected order.
      const results = await Promise.allSettled(files.map(async (file) => {
        const slots = uploadSlotsRef.current;
        if (slots.active >= MAX_CONCURRENT_UPLOADS) {
          await new Promise<void>((resolve) => slots.waiting.push(resolve));
        } else {
          slots.active += 1;
        }
        try {
          if (!isCurrentSourceContext(sourceContextGeneration)) {
            throw new Error("Source context changed before upload started.");
          }
          return await apiClient.uploadSourceFile(file);
        } finally {
          const next = slots.waiting.shift();
          if (next) {
            next();
          } else {
            slots.active -= 1;
          }
        }
      }));
      if (!isCurrentSourceContext(sourceContextGeneration)) {
        return;
      }

      const responses = results.flatMap((result) => (result.status === "fulfilled" ? [result.value] : []));
      const failedCount = results.length - responses.length;

      if (responses.length > 0) {
        const items = responses.map((response) =>
          createQueueItem(response.source_handle, conversionMode, response.original_name, response.preflight),
        );
        const selectFirst = !hasSelectedSourceRef.current;
        if (selectFirst) {
          hasSelectedSourceRef.current = true;
        }
        queueControllerRef.current.addQueueItems(items, {
          selectFirst,
        });
        conversionJobRef.current.clearFormError();
        if (batchId !== browserUploadBatchRef.current) {
          return;
        }
        if (failedCount === 0) {
          queueControllerRef.current.setPreflightStatus(
            files.length === 1 ? "Source uploaded." : `${files.length} sources uploaded.`,
          );
        } else {
          queueControllerRef.current.setPreflightStatus(
            `Added ${responses.length} ${responses.length === 1 ? "source" : "sources"}; ${failedCount} upload${failedCount === 1 ? "" : "s"} failed.`,
          );
        }
        return;
      }

      if (batchId !== browserUploadBatchRef.current) {
        return;
      }
      const firstFailure = results.find((result) => result.status === "rejected");
      const detail =
        firstFailure?.status === "rejected"
          ? firstFailure.reason instanceof Error
            ? firstFailure.reason.message
            : String(firstFailure.reason || "unknown error")
          : "unknown error";
      queueControllerRef.current.setPreflightStatus(
        files.length === 1 ? `Source upload failed: ${detail}` : `All ${files.length} source uploads failed: ${detail}`,
      );
    },
    [
      apiClient,
      conversionMode,
      isCurrentSourceContext,
      sourceContextGeneration,
    ],
  );

  const onAddPastedText = useCallback(async (): Promise<ConversionQueueItem | null> => {
    if (!isCurrentSourceContext(sourceContextGeneration)) {
      return null;
    }
    // A ref guard (not the pasteBusy state) closes the same-frame double-click
    // gap: two clicks dispatched before React re-renders both see the stale
    // disabled state, but the ref flips synchronously so only the first runs.
    if (pasteInFlightRef.current) {
      return null;
    }
    if (!apiClient) {
      setPasteStatus("Backend is not connected.");
      return null;
    }

    const text = pastedText.trim();
    if (!text) {
      setPasteStatus("Paste text before adding.");
      return null;
    }

    const pasteTitle = pastedTitle.trim();
    const pasteAuthor = pastedAuthor.trim();

    pasteInFlightRef.current = true;
    setPasteBusy(true);
    setPasteStatus("Creating markdown source…");
    try {
      const response = await apiClient.uploadSourceText({
        text,
        title: pasteTitle || null,
      });
      if (!isCurrentSourceContext(sourceContextGeneration)) {
        return null;
      }
      const item = createQueueItem(
        response.source_handle,
        conversionMode,
        response.original_name,
        response.preflight,
      );
      if (pasteTitle) {
        item.title = pasteTitle;
      }
      if (pasteAuthor) {
        item.author = pasteAuthor;
      }
      // A paste is always a new source; select it so the Title field and the
      // primary action target it rather than a stale earlier row.
      queueControllerRef.current.addPastedSource(item, { selectItem: true });
      setPastedText("");
      setPastedTitle("");
      setPastedAuthor("");
      setPasteStatus("Pasted markdown source added.");
      conversionJobRef.current.clearFormError();
      return item;
    } catch (error: unknown) {
      if (!isCurrentSourceContext(sourceContextGeneration)) {
        return null;
      }
      setPasteStatus(error instanceof Error ? error.message : "Paste upload failed");
      return null;
    } finally {
      // A context change (or unmount) already released the in-flight guard for
      // the context that replaced this one, so only the still-current paste
      // clears it here.
      if (isCurrentSourceContext(sourceContextGeneration)) {
        pasteInFlightRef.current = false;
        setPasteBusy(false);
      }
    }
  }, [
    apiClient,
    conversionMode,
    isCurrentSourceContext,
    pastedAuthor,
    pastedText,
    pastedTitle,
    sourceContextGeneration,
  ]);

  const onDropZoneDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragging(false);

      const maybePathList = event.dataTransfer.getData("text/uri-list");
      if (maybePathList) {
        const paths = pathsFromUriList(maybePathList);
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

      queueController.setPreflightStatus("Dropped files must come from the desktop runtime with native paths.");
    },
    [applySourcePaths, queueController],
  );

  const onInspectCurrentSource = useCallback(() => {
    const normalized = queueController.sourcePath.trim();
    if (!normalized) {
      queueController.setPreflightStatus("Choose a source before preflight.");
      return;
    }

    const selectedQueueItem = queueController.selectedQueueItem;
    if (selectedQueueItem && selectedQueueItem.sourcePath === normalized) {
      void queueController.runPreflightForItem(
        {
          ...selectedQueueItem,
          title: queueController.titleOverride,
          author: queueController.authorOverride,
          preflight: null,
        },
        { updateSelected: true },
      );
      return;
    }

    const item = createQueueItem(normalized, conversionMode);
    item.title = queueController.titleOverride.trim();
    item.author = queueController.authorOverride.trim();
    queueController.addQueueItems([item], {
      // This explicitly inspects the path currently in the form, so the new
      // row must own the selection even when an older queue row exists.
      // Otherwise its preflight result cannot gate the Start action.
      selectFirst: true,
    });
  }, [conversionMode, queueController]);

  return {
    pastedText,
    setPastedText,
    pastedTitle,
    setPastedTitle,
    pastedAuthor,
    setPastedAuthor,
    pasteBusy,
    pasteStatus,
    dragging,
    setDragging,
    browserFileInputRef,
    onBrowse,
    onBrowserFilesSelected,
    onAddPastedText,
    onDropZoneDrop,
    onInspectCurrentSource,
  };
}
