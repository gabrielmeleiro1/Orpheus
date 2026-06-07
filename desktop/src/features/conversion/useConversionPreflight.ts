import { useCallback, useState } from "react";

import type { ApiClient } from "../../api/client";
import { compactSample } from "./conversionPanelHelpers";
import type { ConversionQueueItem } from "./conversionPanelModel";

interface UseConversionPreflightArgs {
  apiClient: ApiClient | null;
  setSelectedQueueId: (itemId: string | null) => void;
  setSourcePath: (sourcePath: string) => void;
  setSourceFileLabel: (label: string) => void;
  setTitleOverride: (updater: (current: string) => string) => void;
  setAuthorOverride: (updater: (current: string) => string) => void;
  setPreviewText: (text: string) => void;
  updateQueueItem: (
    itemId: string,
    updater: (item: ConversionQueueItem) => ConversionQueueItem,
  ) => void;
}

export function useConversionPreflight({
  apiClient,
  setSelectedQueueId,
  setSourcePath,
  setSourceFileLabel,
  setTitleOverride,
  setAuthorOverride,
  setPreviewText,
  updateQueueItem,
}: UseConversionPreflightArgs) {
  const [preflightStatus, setPreflightStatus] = useState("");

  const runPreflightForItem = useCallback(
    async (item: ConversionQueueItem, options?: { updateSelected?: boolean }) => {
      if (!apiClient) {
        updateQueueItem(item.id, (current) => ({
          ...current,
          status: "failed",
          error: "Backend is not connected.",
        }));
        return;
      }

      setPreflightStatus(`Inspecting ${item.label}...`);
      try {
        const preflight = item.preflight ?? (await apiClient.preflightSource(item.sourcePath));
        updateQueueItem(item.id, (current) => {
          const nextTitle = current.title || preflight.title?.trim() || "";
          const nextAuthor = current.author || preflight.author?.trim() || "";
          const preserveActiveStatus =
            current.status === "converting" ||
            current.status === "done" ||
            current.status === "failed";
          return {
            ...current,
            title: nextTitle,
            author: nextAuthor,
            preflight,
            status: preserveActiveStatus
              ? current.status
              : preflight.warnings.length
                ? "warning"
                : "ready",
            error: preserveActiveStatus ? current.error : "",
          };
        });

        if (options?.updateSelected) {
          setSelectedQueueId(item.id);
          setSourcePath(item.sourcePath);
          setSourceFileLabel(`${item.label} selected`);
          setTitleOverride((current) => current || preflight.title?.trim() || "");
          setAuthorOverride((current) => current || preflight.author?.trim() || "");
          if (preflight.sample) {
            setPreviewText(compactSample(preflight.sample, 220));
          }
        }

        setPreflightStatus(
          preflight.warnings.length
            ? "Preflight completed with warnings."
            : "Preflight completed.",
        );
      } catch (error: unknown) {
        const message = error instanceof Error ? error.message : "preflight failed";
        updateQueueItem(item.id, (current) => ({
          ...current,
          status: "failed",
          error: message,
        }));
        setPreflightStatus(message);
      }
    },
    [
      apiClient,
      setAuthorOverride,
      setPreviewText,
      setSelectedQueueId,
      setSourceFileLabel,
      setSourcePath,
      setTitleOverride,
      updateQueueItem,
    ],
  );

  const runPreflightQueue = useCallback(
    async (items: ConversionQueueItem[], options?: { selectedItemId?: string | null }) => {
      for (const item of items) {
        await runPreflightForItem(item, {
          updateSelected: item.id === options?.selectedItemId,
        });
      }
    },
    [runPreflightForItem],
  );

  return {
    preflightStatus,
    setPreflightStatus,
    runPreflightForItem,
    runPreflightQueue,
  };
}
