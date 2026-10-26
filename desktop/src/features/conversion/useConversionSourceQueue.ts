import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type DragEvent,
  type ChangeEvent,
} from "react";

import { useGenerationGuard, useMountedRef } from "../../state/generationGuard";
import type { ApiClient } from "../../api/client";
import type { ConversionMode } from "../../types/api";
import { extractDroppedPaths, pathsFromUriList } from "./conversionPanelHelpers";
import { isTauriRuntime } from "../../platform/tauriRuntime";
import { createQueueItem, isFinishedQueueItem, type ConversionQueueItem } from "./conversionPanelModel";
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
  const browserUploadGuard = useGenerationGuard();
  const uploadSlotsRef = useRef({ active: 0, waiting: [] as Array<() => void> });
  const pasteInFlightRef = useRef(false);
  const queueControllerRef = useRef(queueController);
  const conversionJobRef = useRef(conversionJob);
  const conversionModeRef = useRef(conversionMode);
  const sourceContextRef = useRef({
    apiClient,
    generation: 0,
  });

  useLayoutEffect(() => {
    queueControllerRef.current = queueController;
  });
  useLayoutEffect(() => {
    conversionJobRef.current = conversionJob;
  });
  useLayoutEffect(() => {
    conversionModeRef.current = conversionMode;
  });
  if (sourceContextRef.current.apiClient !== apiClient) {
    sourceContextRef.current = {
      apiClient,
      generation: sourceContextRef.current.generation + 1,
    };
    pasteInFlightRef.current = false;
  }
  const sourceContextGeneration = sourceContextRef.current.generation;

  // Run only on mount/unmount: rerunning during a paste could let its completion
  // clear a newer paste's guard. Leave the generation unchanged so StrictMode's
  // effect replay keeps existing callbacks valid.
  const mountedRef = useMountedRef();
  useEffect(() => () => {
    pasteInFlightRef.current = false;
  }, []);

  useEffect(() => {
    setPasteBusy(false);
    setPasteStatus("Idle");
  }, [apiClient]);

  const isCurrentSourceContext = useCallback(
    (generation: number) =>
      mountedRef.current && sourceContextRef.current.generation === generation,
    [],
  );

  useEffect(() => {
    // A finished row does not hold the selection: the next book added takes
    // it, so the primary action starts that book instead of repeating this one.
    const selected = queueController.selectedQueueItem;
    hasSelectedSourceRef.current = selected
      ? !isFinishedQueueItem(selected)
      : Boolean(queueController.selectedQueueId || queueController.sourcePath.trim());
  }, [queueController.selectedQueueId, queueController.selectedQueueItem, queueController.sourcePath]);

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

  useLayoutEffect(() => {
    applySourcePathsRef.current = applySourcePaths;
  });

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

  const uploadBrowserFiles = useCallback(
    async (selected: File[]) => {
      let files = selected;
      if (files.length === 0) {
        return;
      }
      if (!isCurrentSourceContext(sourceContextGeneration)) {
        return;
      }
      if (!apiClient) {
        queueControllerRef.current.setPreflightStatus("Not connected.");
        return;
      }

      // The same file chosen again stays one row; say so instead of adding it twice.
      const queued = queueControllerRef.current.queueItems ?? [];
      const duplicates = files.filter((file) => queued.some((item) =>
        item.label === file.name && item.preflight?.size_bytes === file.size));
      files = files.filter((file) => !duplicates.includes(file));
      const duplicateNote = duplicates.length
        ? `${duplicates.map((file) => file.name).join(", ")} ${duplicates.length === 1 ? "is" : "are"} already in the queue.`
        : "";
      if (files.length === 0) {
        queueControllerRef.current.setPreflightStatus(duplicateNote);
        return;
      }

      browserUploadGuard.invalidate();
      const ownsBatch = browserUploadGuard.capture();
      queueControllerRef.current.setPreflightStatus(`Adding ${files.length === 1 ? files[0].name : `${files.length} sources`}…`);
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
      // Name each file that was not added, with the backend's reason.
      const failures = results.flatMap((result, index) => result.status === "rejected"
        ? [`${files[index].name}: ${result.reason instanceof Error ? result.reason.message : String(result.reason || "unknown error")}`]
        : []);

      if (responses.length > 0) {
        const items = responses.map((response) =>
          createQueueItem(response.source_handle, conversionModeRef.current, response.original_name, response.preflight),
        );
        const selectFirst = !hasSelectedSourceRef.current;
        if (selectFirst) {
          hasSelectedSourceRef.current = true;
        }
        queueControllerRef.current.addQueueItems(items, {
          selectFirst,
        });
        conversionJobRef.current.clearFormError();
        if (!ownsBatch()) {
          return;
        }
        // The new queue rows confirm success; only a partial failure needs words.
        queueControllerRef.current.setPreflightStatus([
          failedCount === 0 ? "" : `Added ${responses.length} ${responses.length === 1 ? "source" : "sources"}. Not added: ${failures.join("; ")}`,
          duplicateNote,
        ].filter(Boolean).join(" "));
        return;
      }

      if (!ownsBatch()) {
        return;
      }
      const firstFailure = results.find((result) => result.status === "rejected");
      const detail =
        firstFailure?.status === "rejected"
          ? firstFailure.reason instanceof Error
            ? firstFailure.reason.message
            : String(firstFailure.reason || "unknown error")
          : "unknown error";
      queueControllerRef.current.setPreflightStatus([
        files.length === 1 ? `Could not add the file: ${detail}` : `Could not add these files: ${failures.join("; ")}`,
        duplicateNote,
      ].filter(Boolean).join(" "));
    },
    [
      apiClient,
      isCurrentSourceContext,
      sourceContextGeneration,
    ],
  );

  const onBrowserFilesSelected = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files ?? []);
      event.target.value = "";
      await uploadBrowserFiles(files);
    },
    [uploadBrowserFiles],
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
      setPasteStatus("Not connected.");
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
    setPasteStatus("");
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
        // Format is now an editable choice on the same source page. A change
        // while intake is pending applies to the new row, without losing it.
        conversionModeRef.current,
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
      setPastedText((current) => current === pastedText ? "" : current);
      setPastedTitle((current) => current === pastedTitle ? "" : current);
      setPastedAuthor((current) => current === pastedAuthor ? "" : current);
      setPasteStatus("");
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

      if (!isTauriRuntime()) {
        void uploadBrowserFiles(files);
        return;
      }

      const desktopPaths = files
        .map((file) => (file as File & { path?: string }).path)
        .filter((path): path is string => Boolean(path?.trim()));
      if (desktopPaths.length === files.length) {
        applySourcePaths(desktopPaths);
        return;
      }

      queueController.setPreflightStatus("Drop files from your file manager.");
    },
    [applySourcePaths, queueController, uploadBrowserFiles],
  );

  const onAddSourcePath = useCallback((path: string) => {
    applySourcePaths([path]);
  }, [applySourcePaths]);

  const onInspectCurrentSource = useCallback(() => {
    const normalized = queueController.sourcePath.trim();
    if (!normalized) {
      queueController.setPreflightStatus("Choose a source first.");
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
    onAddSourcePath,
    onInspectCurrentSource,
  };
}
