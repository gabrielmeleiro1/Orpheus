import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { useMountedRef } from "../../state/generationGuard";
import type { ApiClient } from "../../api/client";
import type { StartConversionRequest } from "../../types/api";
import {
  APP_RESTARTED_MESSAGE,
  createQueueItem,
  isPreflightConvertible,
  isQueueRunnable,
  QUEUE_CANCELLED_MESSAGE,
  type ConversionQueueItem,
} from "./conversionPanelModel";
import type { QueueEditPatch } from "./conversionPanelStateTypes";
import type { ConversionJobController, StartConversionResult } from "./useConversionJob";

// Match terminal-state polling: a stalled status read must pause the queue
// without assuming the backend released its conversion slot.
const QUEUE_STATUS_REQUEST_TIMEOUT_MS = 8_000;
const CLEANUP_ADMISSION_MAX_WAIT_MS = 30_000;
/** The job was lost from view, which is not the same as seeing it fail. */
const UNCONFIRMED_CONVERSION_MESSAGE = "Could not confirm the conversion. Check Library before retrying.";

interface QueueCommandOwner {
  client: ApiClient;
  job: ConversionJobController;
  controller: AbortController;
  cancelRequested: boolean;
  itemId?: string;
}

/** The failed row's text: the backend's reason when it gave one. */
function failedRowMessage(status: string, reason: string | undefined): string {
  if (status === "cancelled") return "conversion cancelled";
  const message = reason?.trim() ?? "";
  // Generic terminal texts ("conversion failed") carry no reason of their own.
  return message && !/^conversion (failed|cancelled|completed)$/i.test(message) ? message : `conversion ${status}`;
}

/** How a row reads once its job reached a terminal state. */
function terminalRow(status: string, reason?: string): Pick<ConversionQueueItem, "status" | "error"> {
  if (status === "completed") return { status: "done", error: "" };
  if (status === "unknown") return { status: "warning", error: UNCONFIRMED_CONVERSION_MESSAGE };
  return { status: "failed", error: failedRowMessage(status, reason) };
}

function pauseCommand(owner: QueueCommandOwner, delayMs: number): Promise<void> {
  return new Promise((resolve) => {
    if (owner.controller.signal.aborted) {
      resolve();
      return;
    }
    const finish = () => {
      window.clearTimeout(timer);
      owner.controller.signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = window.setTimeout(finish, delayMs);
    owner.controller.signal.addEventListener("abort", finish, { once: true });
  });
}

function isRetryablePause(result: StartConversionResult): boolean {
  return !result.ok && (
    (result.status === 409 && result.serverCode === "conversion_cleanup_pending") ||
    result.ambiguous || result.serverCode === "runtime_options_pending" || result.serverCode === "runtime_check_pending" || result.serverCode === "queue_cancelled"
  );
}

/** How a row reads after a start that did not return a job. */
function stoppedRow(result: Extract<StartConversionResult, { ok: false }>): Pick<ConversionQueueItem, "status" | "error" | "startUncertain"> {
  const paused = isRetryablePause(result);
  return {
    status: paused ? "warning" : "failed",
    error: result.error,
    // An unanswered or cancelled request may have reached the backend.
    startUncertain: paused && (result.ambiguous === true || result.serverCode === "queue_cancelled"),
  };
}

interface UseConversionQueueRunnerArgs {
  apiClient: ApiClient | null;
  runtimeOptionsReady: boolean;
  conversionJob: ConversionJobController;
  queueItems: ConversionQueueItem[];
  selectedQueueItem: ConversionQueueItem | null;
  buildStartPayload: (item?: ConversionQueueItem) => StartConversionRequest;
  addQueueItems: (items: ConversionQueueItem[], options?: { selectFirst?: boolean; skipPreflight?: boolean }) => void;
  updateQueueItem: (
    itemId: string,
    updater: (item: ConversionQueueItem) => ConversionQueueItem,
  ) => void;
  syncSelectedQueueEdits: (patch: QueueEditPatch) => void;
  setQueueStatus: (status: string) => void;
}

export function useConversionQueueRunner({
  apiClient,
  runtimeOptionsReady,
  conversionJob,
  queueItems,
  selectedQueueItem,
  buildStartPayload,
  addQueueItems,
  updateQueueItem,
  syncSelectedQueueEdits,
  setQueueStatus,
}: UseConversionQueueRunnerArgs) {
  const [queueRunning, setQueueRunning] = useState(false);
  const [commandRunning, setCommandRunning] = useState(false);
  const queueRunningRef = useRef(queueRunning);
  useLayoutEffect(() => {
    queueRunningRef.current = queueRunning;
  });
  const uncertainItemRef = useRef<{ itemId: string; clientRequestId: string; client: ApiClient } | null>(null);
  const commandOwnerRef = useRef<QueueCommandOwner | null>(null);
  const latestRef = useRef({ apiClient, runtimeOptionsReady, queueItems, conversionJob, buildStartPayload });
  useLayoutEffect(() => {
    latestRef.current = { apiClient, runtimeOptionsReady, queueItems, conversionJob, buildStartPayload };
  });
  const mountedRef = useMountedRef(true);
  useEffect(() => () => {
    commandOwnerRef.current?.controller.abort();
    commandOwnerRef.current = null;
  }, []);

  const ownsCommand = useCallback((owner: QueueCommandOwner) => (
    mountedRef.current && commandOwnerRef.current === owner && latestRef.current.apiClient === owner.client
  ), []);

  useEffect(() => {
    const owner = commandOwnerRef.current;
    if (!owner || owner.client === apiClient) return;
    owner.controller.abort();
    commandOwnerRef.current = null;
    setQueueRunning(false);
    setCommandRunning(false);
    if (owner.itemId) {
      updateQueueItem(owner.itemId, (item) => item.status === "converting" ? {
        ...item, status: "warning", error: APP_RESTARTED_MESSAGE, startUncertain: true,
      } : item);
    }
  }, [apiClient, updateQueueItem]);

  // Replace the checking notice once the start is resolved; otherwise it
  // outlives the reconciliation and the job it describes.
  const checkingNoticeRef = useRef<{ client: ApiClient; queueRun: boolean } | null>(null);
  useEffect(() => {
    if (conversionJob.isCheckingStart) {
      const owner = commandOwnerRef.current;
      if (owner && ownsCommand(owner)) {
        checkingNoticeRef.current = { client: owner.client, queueRun: queueRunningRef.current };
        setQueueStatus("Checking whether the conversion started…");
      }
      return;
    }
    const notice = checkingNoticeRef.current;
    if (!notice) return;
    checkingNoticeRef.current = null;
    // A queue that is still running reports its own progress.
    if (notice.queueRun && queueRunningRef.current) return;
    if (notice.client !== apiClient) {
      setQueueStatus("The app restarted. Check Library before retrying.");
      return;
    }
    const reconciliation = conversionJob.startReconciliation;
    if (reconciliation && !reconciliation.result.ok) {
      setQueueStatus(reconciliation.result.error);
      return;
    }
    setQueueStatus(notice.queueRun
      ? "Conversion start confirmed. Run the queue again to continue."
      : "Conversion start confirmed.");
  }, [apiClient, conversionJob.isCheckingStart, conversionJob.startReconciliation, ownsCommand, setQueueStatus]);

  useEffect(() => {
    const pending = uncertainItemRef.current;
    const reconciliation = conversionJob.startReconciliation;
    if (!pending || !reconciliation || pending.client !== apiClient ||
      pending.clientRequestId !== reconciliation.clientRequestId) return;
    uncertainItemRef.current = null;
    const result = reconciliation.result;
    updateQueueItem(pending.itemId, (item) => ({
      ...item,
      jobId: result.ok ? result.jobId : item.jobId,
      status: result.ok ? "converting" : "failed",
      error: result.ok ? "" : result.error,
    }));
    if (result.ok) {
      void conversionJob.waitForTerminalState(result.jobId).then((terminal) => {
        if (!mountedRef.current || latestRef.current.apiClient !== pending.client) return;
        updateQueueItem(pending.itemId, (item) => ({ ...item, ...terminalRow(terminal.status, terminal.message) }));
      });
    }
  }, [apiClient, conversionJob, updateQueueItem]);

  const beginCommand = useCallback((): QueueCommandOwner | null => {
    const latest = latestRef.current;
    if (!mountedRef.current || !latest.apiClient || !latest.runtimeOptionsReady ||
      latest.conversionJob.isCheckingStart || latest.conversionJob.activeJobId || commandOwnerRef.current) {
      return null;
    }
    const owner = { client: latest.apiClient, job: latest.conversionJob,
      controller: new AbortController(), cancelRequested: false };
    commandOwnerRef.current = owner;
    setCommandRunning(true);
    return owner;
  }, []);

  const cancelCommand = useCallback(() => {
    const owner = commandOwnerRef.current;
    if (!owner || !ownsCommand(owner)) return;
    owner.cancelRequested = true;
    owner.controller.abort();
    setQueueStatus("Queue cancelled.");
  }, [ownsCommand, setQueueStatus]);

  const startWhenReady = useCallback(async (
    owner: QueueCommandOwner, payload: StartConversionRequest,
  ): Promise<StartConversionResult | null> => {
    const startedAt = Date.now();
    let attempt = 0;
    let waitingForCleanup = false;
    const cancelledResult: StartConversionResult = { ok: false, serverCode: "queue_cancelled", error: QUEUE_CANCELLED_MESSAGE };
    while (ownsCommand(owner)) {
      if (owner.cancelRequested) return cancelledResult;
      if (waitingForCleanup && Date.now() - startedAt >= CLEANUP_ADMISSION_MAX_WAIT_MS) {
        return { ok: false, status: 409, serverCode: "conversion_cleanup_pending",
          error: "Previous conversion is still finishing cleanup. Try this item again shortly." };
      }
      if (!latestRef.current.runtimeOptionsReady) {
        return { ok: false, serverCode: "runtime_options_pending",
          error: "Wait for runtime options to load before continuing." };
      }
      const result = await owner.job.startConversion(payload, {
        signal: owner.controller.signal,
      });
      if (!ownsCommand(owner)) return null;
      if (owner.cancelRequested) {
        // A request already on the wire may have started a job. Wait for its
        // identity before cancelling it, and never submit a replacement.
        if (result.ok) {
          await owner.job.cancelActiveConversion();
          return ownsCommand(owner) ? result : null;
        }
        return cancelledResult;
      }
      // Only this explicit rejection proves the request did not start a job.
      // Replaying a network failure could create a duplicate conversion.
      if (result.ok || result.status !== 409 || result.serverCode !== "conversion_cleanup_pending") {
        return result;
      }
      waitingForCleanup = true;
      setQueueStatus("Waiting for the previous conversion to finish cleanup…");
      const remainingMs = CLEANUP_ADMISSION_MAX_WAIT_MS - (Date.now() - startedAt);
      if (remainingMs > 0) await pauseCommand(owner, Math.min(500 * 2 ** attempt++, 2_000, remainingMs));
    }
    return null;
  }, [ownsCommand, setQueueStatus]);

  const onStartConversion = useCallback(async (itemOverride?: ConversionQueueItem) => {
    const owner = beginCommand();
    if (!owner) return;
    // A previous queue outcome ("Queue cancelled.") does not describe this run.
    setQueueStatus("");
    let targetItem = itemOverride ?? selectedQueueItem;
    const restarting = targetItem?.status === "done";
    try {
      const payload = latestRef.current.buildStartPayload(restarting && !itemOverride ? undefined : targetItem ?? undefined);
      if (restarting && targetItem) {
        targetItem = {
          ...createQueueItem(payload.source_path, payload.conversion_mode, targetItem.label, targetItem.preflight),
          title: payload.title ?? "",
          author: payload.author ?? "",
        };
        // Reuse the inspected source without refilling deliberately cleared metadata.
        addQueueItems([targetItem], { selectFirst: true, skipPreflight: true });
      }
      if (targetItem) {
        owner.itemId = targetItem.id;
        // An override (e.g. a just-queued paste) is the payload's source of
        // truth already; syncing would only reset its preflight state.
        if (!itemOverride && !restarting) {
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

      const startResult = await startWhenReady(owner, payload);
      if (!startResult || !ownsCommand(owner) || !targetItem) {
        return;
      }

      if (!startResult.ok) {
        if (startResult.ambiguous && startResult.clientRequestId) {
          uncertainItemRef.current = { itemId: targetItem.id, clientRequestId: startResult.clientRequestId, client: owner.client };
        }
        updateQueueItem(targetItem.id, (current) => ({ ...current, ...stoppedRow(startResult) }));
        return;
      }

      updateQueueItem(targetItem.id, (current) => ({
        ...current,
        jobId: startResult.jobId,
        status: "converting",
      }));

      const terminal = await owner.job.waitForTerminalState(startResult.jobId);
      if (!ownsCommand(owner)) return;
      updateQueueItem(targetItem.id, (current) => ({ ...current, ...terminalRow(terminal.status, terminal.message) }));
    } catch (error: unknown) {
      if (ownsCommand(owner) && targetItem) {
        const message = error instanceof Error ? error.message : "conversion failed";
        updateQueueItem(targetItem.id, (current) => ({
          ...current,
          status: "failed",
          error: message,
        }));
      }
    } finally {
      if (ownsCommand(owner)) {
        commandOwnerRef.current = null;
        setCommandRunning(false);
        if (owner.cancelRequested) {
          void Promise.resolve().then(() => owner.job.refreshActiveConversion()).catch(() => {
            // The row already asks the user to check Library if observation was lost.
          });
        }
      }
    }
  }, [addQueueItems, beginCommand, ownsCommand, selectedQueueItem, setQueueStatus, startWhenReady, syncSelectedQueueEdits, updateQueueItem]);

  const onRunQueue = useCallback(async () => {
    if (!apiClient) {
      setQueueStatus("Backend is not connected.");
      return;
    }
    if (!latestRef.current.runtimeOptionsReady || commandOwnerRef.current) {
      return;
    }

    const runnableItems = queueItems.filter((item) => Boolean(item.sourcePath.trim()) && isQueueRunnable(item));
    if (runnableItems.length === 0) {
      const hasPendingPreflight = queueItems.some((item) => item.status === "preflighting");
      setQueueStatus(
        hasPendingPreflight ? "Preflight is still running." : "No queued items to convert.",
      );
      return;
    }

    const owner = beginCommand();
    if (!owner) return;
    setQueueRunning(true);
    setQueueStatus(
      `Running ${runnableItems.length} queued item${runnableItems.length === 1 ? "" : "s"}…`,
    );

    try {
      let queueCompleted = true;
      let cancelledItems = 0;
      const attemptedIds = new Set<string>();
      let skippedItems = false;
      const remainingIds = new Set(runnableItems.map((item) => item.id));
      while (ownsCommand(owner) && !owner.cancelRequested) {
        // Rows added while the queue runs join it; rows it started with keep their order.
        const item = latestRef.current.queueItems.find((candidate) => remainingIds.has(candidate.id))
          ?? latestRef.current.queueItems.find((candidate) => !attemptedIds.has(candidate.id)
            && !runnableItems.some((started) => started.id === candidate.id)
            && candidate.status === "ready" && Boolean(candidate.sourcePath.trim()));
        if (!item) break;
        if (!latestRef.current.runtimeOptionsReady) {
          queueCompleted = false;
          setQueueStatus("Queue paused: wait for runtime options to load before continuing.");
          break;
        }
        remainingIds.delete(item.id);
        attemptedIds.add(item.id);
        if (!item.sourcePath.trim() ||
          !isQueueRunnable(item) ||
          (item.preflight && !isPreflightConvertible(item.preflight)) ||
          (runnableItems.some((candidate) => candidate.id === item.id)
            && item.sourcePath !== runnableItems.find((candidate) => candidate.id === item.id)?.sourcePath)) {
          skippedItems = true;
          continue;
        }
        owner.itemId = item.id;
        updateQueueItem(item.id, (current) => ({
          ...current,
          status: "converting",
          error: "",
        }));
        const payload = latestRef.current.buildStartPayload(item);
        try {
          const startResult = await startWhenReady(owner, payload);
          if (!startResult || !ownsCommand(owner)) return;
          if (!startResult.ok) {
            if (startResult.ambiguous && startResult.clientRequestId) {
              uncertainItemRef.current = { itemId: item.id, clientRequestId: startResult.clientRequestId, client: owner.client };
            }
            if (isRetryablePause(startResult)) {
              updateQueueItem(item.id, (current) => ({ ...current, ...stoppedRow(startResult) }));
              queueCompleted = false;
              setQueueStatus(owner.cancelRequested ? "Queue cancelled." : `Queue paused: ${startResult.error}`);
              break;
            }
            throw new Error(startResult.error);
          }
          updateQueueItem(item.id, (current) => ({
            ...current,
            jobId: startResult.jobId,
            status: "converting",
          }));
          setQueueStatus(`Converting ${item.title || item.label}…`);
          const terminal = await owner.job.waitForTerminalState(startResult.jobId);
          let finalStatus = terminal.status;
          if (!ownsCommand(owner)) return;
          if (finalStatus === "unknown") {
            setQueueStatus("Checking backend before continuing the queue…");
            // Losing observation does not release the backend's job slot.
            // Keep the original client until the slot is demonstrably idle.
            let reconciled = false;
            while (ownsCommand(owner) && !owner.cancelRequested) {
              try {
                const active = await owner.client.getActiveConversion({ timeoutMs: QUEUE_STATUS_REQUEST_TIMEOUT_MS });
                if (!ownsCommand(owner)) return;
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
              await pauseCommand(owner, 1500);
            }
            if (!ownsCommand(owner)) return;
            if (!reconciled || finalStatus === "unknown") {
              updateQueueItem(item.id, (current) => ({ ...current, ...terminalRow("unknown") }));
              queueCompleted = false;
              setQueueStatus(reconciled
                ? UNCONFIRMED_CONVERSION_MESSAGE
                : "Queue paused: backend status unavailable. Reconnect before running the remaining items.");
              if (!reconciled) break;
              continue;
            }
          }
          const row = terminalRow(finalStatus, terminal.message);
          updateQueueItem(item.id, (current) => ({ ...current, ...row }));
          if (finalStatus === "completed") continue;
          if (finalStatus === "cancelled") {
            // Cancelling a book stops that book only; the queue goes on to the next one.
            cancelledItems += 1;
            setQueueStatus(`Cancelled ${item.title || item.label}. Continuing with the next book.`);
            continue;
          }
          setQueueStatus(row.error);
          queueCompleted = false;
          break;
        } catch (error: unknown) {
          if (!ownsCommand(owner)) return;
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
      if (!ownsCommand(owner)) return;
      if (owner.cancelRequested) {
        setQueueStatus("Queue cancelled.");
      } else if (queueCompleted) {
        setQueueStatus([
          "Queue run finished.",
          cancelledItems ? `${cancelledItems} ${cancelledItems === 1 ? "book was" : "books were"} cancelled.` : "",
          skippedItems ? "Edited or unready items were skipped." : "",
        ].filter(Boolean).join(" "));
      }
      // Refresh the display without holding command ownership: an unreachable
      // backend must not keep a paused queue locked indefinitely.
      void Promise.resolve().then(() => owner.job.refreshActiveConversion()).catch(() => {
        // Preserve the queue outcome when the final display refresh fails.
      });
    } finally {
      if (ownsCommand(owner)) {
        commandOwnerRef.current = null;
        setQueueRunning(false);
        setCommandRunning(false);
      }
    }
  }, [
    apiClient,
    beginCommand,
    ownsCommand,
    queueItems,
    startWhenReady,
    setQueueStatus,
    updateQueueItem,
  ]);

  return {
    queueRunning,
    commandRunning,
    cancelCommand,
    onStartConversion,
    onRunQueue,
  };
}
