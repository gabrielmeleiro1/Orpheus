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
  const mountedRef = useRef(false);
  const pasteInFlightRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

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
      queueController.addQueueItems(items, {
        selectFirst: !hasSelectedSourceRef.current,
      });
      if (items.length > 0) {
        conversionJob.clearFormError();
      }
    },
    [conversionJob, conversionMode, queueController],
  );

  useEffect(() => {
    applySourcePathsRef.current = applySourcePaths;
  }, [applySourcePaths]);

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
    if (isTauriRuntime()) {
      try {
        const selectedPaths = await invoke<string[]>("choose_source_files");
        if (Array.isArray(selectedPaths) && selectedPaths.length > 0) {
          applySourcePaths(selectedPaths);
        }
      } catch (error: unknown) {
        const detail = error instanceof Error ? error.message : String(error || "unknown error");
        queueController.setPreflightStatus(`Could not open file picker: ${detail}`);
      }
      return;
    }

    browserFileInputRef.current?.click();
  }, [applySourcePaths, queueController]);

  const onBrowserFilesSelected = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files ?? []);
      event.target.value = "";
      if (files.length === 0) {
        return;
      }
      if (!apiClient) {
        queueController.setPreflightStatus("Backend is not connected.");
        return;
      }

      const batchId = ++browserUploadBatchRef.current;
      queueController.setPreflightStatus(`Uploading ${files.length === 1 ? files[0].name : `${files.length} sources`}...`);
      const results = await Promise.allSettled(files.map(async (file) => apiClient.uploadSourceFile(file)));
      if (!mountedRef.current) {
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
        queueController.addQueueItems(items, {
          selectFirst,
        });
        conversionJob.clearFormError();
        if (batchId !== browserUploadBatchRef.current) {
          return;
        }
        if (failedCount === 0) {
          queueController.setPreflightStatus(
            files.length === 1 ? "Source uploaded." : `${files.length} sources uploaded.`,
          );
        } else {
          queueController.setPreflightStatus(
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
      queueController.setPreflightStatus(
        files.length === 1 ? `Source upload failed: ${detail}` : `All ${files.length} source uploads failed: ${detail}`,
      );
    },
    [apiClient, conversionJob, conversionMode, queueController],
  );

  const onAddPastedText = useCallback(async (): Promise<ConversionQueueItem | null> => {
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
      queueController.addPastedSource(item, { selectItem: true });
      setPastedText("");
      setPastedTitle("");
      setPastedAuthor("");
      setPasteStatus("Pasted markdown source added.");
      conversionJob.clearFormError();
      return item;
    } catch (error: unknown) {
      setPasteStatus(error instanceof Error ? error.message : "Paste upload failed");
      return null;
    } finally {
      setPasteBusy(false);
      pasteInFlightRef.current = false;
    }
  }, [
    apiClient,
    conversionJob,
    conversionMode,
    pastedAuthor,
    pastedText,
    pastedTitle,
    queueController,
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
      selectFirst: !hasSelectedSourceRef.current,
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
