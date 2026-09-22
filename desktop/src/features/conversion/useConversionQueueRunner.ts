import { useCallback, useEffect, useRef, useState } from "react";

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
  const commandInFlightRef = useRef(false);
  const latestRef = useRef({ apiClient, queueItems });
  latestRef.current = { apiClient, queueItems };
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);


  const onStartConversion = useCallback(async (itemOverride?: ConversionQueueItem) => {
    if (commandInFlightRef.current) {
      return;
    }
    const targetItem = itemOverride ?? selectedQueueItem;
    const payload = buildStartPayload(targetItem ?? undefined);
    commandInFlightRef.current = true;
    try {
      if (targetItem) {
        // An override (e.g. a just-queued paste) is the payload's source of
        // truth already; syncing would only reset its preflight state.
        if (!itemOverride) {
          syncSelectedQueueEdits({
            sourcePath: payload.source_path,
            title: payload.title ?? "",
            author: payload.author ?? "",
          });
        }
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

      if (finalStatus === "unknown") {
        // We lost sight of the job rather than seeing it fail; say so.
        updateQueueItem(targetItem.id, (current) => ({
          ...current,
          status: "warning",
          error: "conversion status unknown - check Library",
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
      commandInFlightRef.current = false;
    }
  }, [buildStartPayload, conversionJob, selectedQueueItem, syncSelectedQueueEdits, updateQueueItem]);

  const onRunQueue = useCallback(async () => {
    if (!apiClient) {
      setQueueStatus("Backend is not connected.");
      return;
    }
    if (queueRunning || commandInFlightRef.current) {
      return;
    }

    const runnableItems = queueItems.filter(
      (item) =>
        Boolean(item.sourcePath.trim()) &&
        (item.status === "ready" || item.status === "warning"),
    );
    if (runnableItems.length === 0) {
      const hasPendingPreflight = queueItems.some((item) => item.status === "preflighting");
      setQueueStatus(
        hasPendingPreflight ? "Preflight is still running." : "No queued items to convert.",
      );
      return;
    }

    commandInFlightRef.current = true;
    setQueueRunning(true);
    setQueueStatus(
      `Running ${runnableItems.length} queued item${runnableItems.length === 1 ? "" : "s"}...`,
    );

    try {
      let queueCompleted = true;
      const remainingIds = new Set(runnableItems.map((item) => item.id));
      while (mountedRef.current) {
        const item = latestRef.current.queueItems.find((candidate) => remainingIds.has(candidate.id));
        if (!item) break;
        remainingIds.delete(item.id);
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
          let finalStatus = (await conversionJob.waitForTerminalState(startResult.jobId)).status;
          if (finalStatus === "unknown") {
            const unknownMessage = "conversion status unknown - check Library";
            setQueueStatus("Checking backend before continuing the queue...");
            // Losing observation does not release the backend's job slot.
            // Poll with the current client until the slot is demonstrably idle.
            let reconciled = false;
            while (mountedRef.current) {
              try {
                const client = latestRef.current.apiClient;
                if (!client) break;
                const active = await client.getActiveConversion();
                if (!active.active && active.job?.status !== "running" && active.job?.status !== "idle") {
                  if (active.job?.id === startResult.jobId &&
                    (active.job.status === "completed" || active.job.status === "failed" || active.job.status === "cancelled")) {
                    finalStatus = active.job.status;
                  }
                  reconciled = true;
                  break;
                }
              } catch {
                break;
              }
              await new Promise((resolve) => window.setTimeout(resolve, 1500));
            }
            if (!reconciled || finalStatus === "unknown") {
              updateQueueItem(item.id, (current) => ({
                ...current,
                status: "warning",
                error: unknownMessage,
              }));
              queueCompleted = false;
              setQueueStatus(unknownMessage);
              if (!reconciled) {
                setQueueStatus("Queue paused: backend status unavailable. Reconnect before running the remaining items.");
                break;
              }
              continue;
            }
          }
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
      commandInFlightRef.current = false;
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
