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
import {
  extractDroppedPaths,
  isTauriRuntime,
} from "./conversionPanelHelpers";
import { createQueueItem } from "./conversionPanelModel";
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
  const [pasteTitle, setPasteTitle] = useState("");
  const [pastedText, setPastedText] = useState("");
  const [pasteBusy, setPasteBusy] = useState(false);
  const [pasteStatus, setPasteStatus] = useState("paste idle");
  const [dragging, setDragging] = useState(false);

  const applySourcePathsRef = useRef<(paths: string[]) => void>(() => {});
  const hasSelectedSourceRef = useRef(false);
  const browserFileInputRef = useRef<HTMLInputElement>(null);

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

      queueController.setPreflightStatus(`Uploading ${files.length === 1 ? files[0].name : `${files.length} sources`}...`);
      try {
        const responses = await Promise.all(files.map((file) => apiClient.uploadSourceFile(file)));
        const items = responses.map((response) =>
          createQueueItem(
            response.source_handle,
            conversionMode,
            response.original_name,
            response.preflight,
          ),
        );
        queueController.addQueueItems(items, {
          selectFirst: !hasSelectedSourceRef.current,
        });
        queueController.setPreflightStatus(
          files.length === 1 ? "Source uploaded." : `${files.length} sources uploaded.`,
        );
        conversionJob.clearFormError();
      } catch (error: unknown) {
        queueController.setPreflightStatus(error instanceof Error ? error.message : "source upload failed");
      }
    },
    [apiClient, conversionJob, conversionMode, queueController],
  );

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
      const item = createQueueItem(
        response.source_handle,
        conversionMode,
        response.original_name,
        response.preflight,
      );
      queueController.addPastedSource(item, {
        selectItem: !hasSelectedSourceRef.current,
      });
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
    conversionJob,
    conversionMode,
    pasteTitle,
    pastedText,
    queueController,
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
    pasteTitle,
    setPasteTitle,
    pastedText,
    setPastedText,
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
