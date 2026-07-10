import { useCallback, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type { StartConversionRequest } from "../../types/api";
import type { ConversionQueueItem } from "./conversionPanelModel";
import type { QueueEditPatch } from "./conversionPanelStateTypes";
import type { ConversionJobController } from "./useConversionJob";

interface UseConversionQueueRunnerArgs {
  apiClient: ApiClient | null;
  conversionJob: ConversionJobController;
  queueItems: ConversionQueueItem[];
  selectedQueueItem: ConversionQueueItem | null;
  buildStartPayload: (item?: ConversionQueueItem) => StartConversionRequest;
  updateQueueItem: (
    itemId: string,
    updater: (item: ConversionQueueItem) => ConversionQueueItem,
  ) => void;
  syncSelectedQueueEdits: (patch: QueueEditPatch) => void;
  setQueueStatus: (status: string) => void;
}

export function useConversionQueueRunner({
  apiClient,
  conversionJob,
  queueItems,
  selectedQueueItem,
  buildStartPayload,
  updateQueueItem,
  syncSelectedQueueEdits,
  setQueueStatus,
}: UseConversionQueueRunnerArgs) {
  const [queueRunning, setQueueRunning] = useState(false);
  const startCommandInFlightRef = useRef(false);

  const onStartConversion = useCallback(async () => {
    if (startCommandInFlightRef.current) {
      return;
    }
    startCommandInFlightRef.current = true;
    const targetItem = selectedQueueItem;
    const payload = buildStartPayload(targetItem ?? undefined);
    try {
      if (targetItem) {
        syncSelectedQueueEdits({
          sourcePath: payload.source_path,
          title: payload.title ?? "",
          author: payload.author ?? "",
        });
        updateQueueItem(targetItem.id, (current) => ({
          ...current,
          status: "converting",
          error: "",
        }));
      }

      const startResult = await conversionJob.startConversion(payload);
      if (!targetItem) {
        return;
      }

      if (!startResult.ok) {
        updateQueueItem(targetItem.id, (current) => ({
          ...current,
          status: "failed",
          error: startResult.error,
        }));
        return;
      }

      updateQueueItem(targetItem.id, (current) => ({
        ...current,
        jobId: startResult.jobId,
        status: "converting",
      }));

      const finalStatus = (await conversionJob.waitForTerminalState(startResult.jobId)).status;
      if (finalStatus === "completed") {
        updateQueueItem(targetItem.id, (current) => ({
          ...current,
          status: "done",
          error: "",
        }));
        return;
      }

      const message =
        finalStatus === "cancelled" ? "conversion cancelled" : `conversion ${finalStatus}`;
      updateQueueItem(targetItem.id, (current) => ({
        ...current,
        status: "failed",
        error: message,
      }));
    } catch (error: unknown) {
      if (targetItem) {
        const message = error instanceof Error ? error.message : "conversion failed";
        updateQueueItem(targetItem.id, (current) => ({
          ...current,
          status: "failed",
          error: message,
        }));
      }
    } finally {
      startCommandInFlightRef.current = false;
    }
  }, [buildStartPayload, conversionJob, selectedQueueItem, syncSelectedQueueEdits, updateQueueItem]);

  const onRunQueue = useCallback(async () => {
    if (!apiClient) {
      setQueueStatus("Backend is not connected.");
      return;
    }
    if (queueRunning) {
      return;
    }

    const runnableItems = queueItems.filter(
      (item) => item.status === "ready" || item.status === "warning",
    );
    if (runnableItems.length === 0) {
      const hasPendingPreflight = queueItems.some((item) => item.status === "preflighting");
      setQueueStatus(
        hasPendingPreflight ? "Preflight is still running." : "No queued items to convert.",
      );
      return;
    }

    setQueueRunning(true);
    setQueueStatus(
      `Running ${runnableItems.length} queued item${runnableItems.length === 1 ? "" : "s"}...`,
    );

    try {
      let queueCompleted = true;
      for (const item of runnableItems) {
        updateQueueItem(item.id, (current) => ({
          ...current,
          status: "converting",
          error: "",
        }));
        const payload = buildStartPayload(item);
        try {
          const startResult = await conversionJob.startConversion(payload);
          if (!startResult.ok) {
            throw new Error(startResult.error);
          }
          updateQueueItem(item.id, (current) => ({
            ...current,
            jobId: startResult.jobId,
            status: "converting",
          }));
          setQueueStatus(`Converting ${item.label}...`);
          const finalStatus = (await conversionJob.waitForTerminalState(startResult.jobId)).status;
          if (finalStatus === "completed") {
            updateQueueItem(item.id, (current) => ({
              ...current,
              status: "done",
              error: "",
            }));
            continue;
          }
          const message =
            finalStatus === "cancelled" ? "conversion cancelled" : `conversion ${finalStatus}`;
          updateQueueItem(item.id, (current) => ({
            ...current,
            status: "failed",
            error: message,
          }));
          setQueueStatus(finalStatus === "cancelled" ? "Queue cancelled." : message);
          queueCompleted = false;
          break;
        } catch (error: unknown) {
          const message = error instanceof Error ? error.message : "conversion failed";
          updateQueueItem(item.id, (current) => ({
            ...current,
            status: "failed",
            error: message,
          }));
          setQueueStatus(message);
          queueCompleted = false;
          break;
        }
      }
      if (queueCompleted) {
        setQueueStatus("Queue run finished.");
      }
      await conversionJob.refreshActiveConversion();
    } finally {
      setQueueRunning(false);
    }
  }, [
    apiClient,
    buildStartPayload,
    conversionJob,
    queueItems,
    queueRunning,
    setQueueStatus,
    updateQueueItem,
  ]);

  return {
    queueRunning,
    onStartConversion,
    onRunQueue,
  };
}
