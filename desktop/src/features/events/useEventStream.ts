import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type {
  ActiveConversionResponse,
  ConversionJobStatus,
  ConversionJobTerminalEvent,
  DesktopEventPayload,
  EventStreamConnectionState,
  EventStreamLogEntry,
  PerformanceSnapshot,
  ProgressStageView,
} from "../../types/api";
import {
  createInitialEventStreamState,
  eventStreamReducer,
  normalizeTerminalStatus,
} from "./eventStreamModel";
import { useEventStreamTransport } from "./useEventStreamTransport";
import { useTerminalStateWaiters } from "./useTerminalStateWaiters";

interface UseEventStreamArgs {
  apiClient: ApiClient | null;
  baseUrl: string;
  // Notified with every reconciliation response that wins its ownership fence,
  // so a consumer can derive its own state from the lookup this hook already
  // performs instead of issuing a second identical request.
  onActiveConversionReconciled?: (response: ActiveConversionResponse) => void;
}

interface UseEventStreamResult {
  streamState: EventStreamConnectionState;
  uiState: ConversionJobStatus;
  statusText: string;
  progressPct: number;
  etaText: string;
  stages: ProgressStageView[];
  currentStage: string;
  currentMessage: string;
  performanceSnapshot: PerformanceSnapshot | null;
  activeJobId: string | null;
  logs: EventStreamLogEntry[];
  isCancelling: boolean;
  cancelActiveConversion: () => Promise<void>;
  refreshActiveConversionState: () => Promise<ActiveConversionResponse | null>;
  markConversionStarted: (jobId: string) => void;
  waitForTerminalState: (jobId: string) => Promise<ConversionJobTerminalEvent>;
}

function nowTimestamp(): string {
  return new Date().toLocaleTimeString("en-US", { hour12: false });
}

// After a cancel is accepted the UI normally unlatches on the terminal SSE
// event. Poll the active-job endpoint as a fallback so a missed or delayed
// event cannot leave the interface stuck on "cancelling" forever.
const CANCEL_RECONCILE_INTERVAL_MS = 2_000;
const CANCEL_RECONCILE_MAX_ATTEMPTS = 30;

export function useEventStream({
  apiClient,
  baseUrl,
  onActiveConversionReconciled,
}: UseEventStreamArgs): UseEventStreamResult {
  const initialModelState = useRef(createInitialEventStreamState());
  const [modelState, setModelState] = useState(initialModelState.current);
  const modelStateRef = useRef(initialModelState.current);
  const logSequenceRef = useRef(0);
  const activeJobIdRef = useRef<string | null>(null);
  const lookupFailedRef = useRef(false);
  const runtimeMutationGenerationRef = useRef(0);
  const reconciliationRequestSequenceRef = useRef(0);
  const cancellationAttemptSequenceRef = useRef(0);
  const mountedRef = useRef(true);
  const hasOpenedStreamRef = useRef(false);
  const onReconciledRef = useRef(onActiveConversionReconciled);
  onReconciledRef.current = onActiveConversionReconciled;
  const refreshGenerationRef = useRef(0);

  const nextLogId = useCallback(() => {
    logSequenceRef.current += 1;
    return logSequenceRef.current;
  }, []);

  const resolveTerminalStateRef = useRef<(event: ConversionJobTerminalEvent) => void>(() => {});

  const applyReducerAction = useCallback(
    (action: Parameters<typeof eventStreamReducer>[1]) => {
      const result = eventStreamReducer(modelStateRef.current, action);
      modelStateRef.current = result.state;
      activeJobIdRef.current = result.state.activeJobId;
      setModelState(result.state);
      if (result.terminalEvent) {
        resolveTerminalStateRef.current(result.terminalEvent);
      }
    },
    [],
  );

  const pushLog = useCallback(
    (level: string, message: string, timestamp?: string) => {
      applyReducerAction({
        type: "pushLog",
        level,
        message,
        timestamp,
        logId: nextLogId(),
        fallbackTimestamp: nowTimestamp(),
      });
    },
    [applyReducerAction, nextLogId],
  );

  const applyEventPayload = useCallback(
    (payload: DesktopEventPayload) => {
      if (
        payload.event_type === "state" ||
        payload.event_type === "progress" ||
        payload.event_type === "log"
      ) {
        const eventJobId = String(payload.job_id ?? "").trim();
        if (
          eventJobId &&
          activeJobIdRef.current &&
          eventJobId !== activeJobIdRef.current
        ) {
          return;
        }
      }
      if (payload.event_type === "progress" || payload.event_type === "state") {
        runtimeMutationGenerationRef.current += 1;
      }
      // Any live event is newer than a reconciliation request already in
      // flight. Fence that request so its snapshot cannot roll state back.
      refreshGenerationRef.current += 1;
      if (payload.event_type === "progress") {
        applyReducerAction({ type: "progress", payload });
        return;
      }

      if (payload.event_type === "log") {
        applyReducerAction({
          type: "log",
          payload,
          logId: nextLogId(),
          fallbackTimestamp: nowTimestamp(),
        });
        return;
      }

      if (payload.event_type === "state") {
        applyReducerAction({
          type: "state",
          payload,
          logId: nextLogId(),
          fallbackTimestamp: nowTimestamp(),
        });
      }
    },
    [applyReducerAction, nextLogId],
  );

  const { resolveTerminalState, waitForTerminalState } = useTerminalStateWaiters({
    apiClient,
    onTerminalStatus: applyEventPayload,
  });

  useEffect(() => {
    resolveTerminalStateRef.current = resolveTerminalState;
  }, [resolveTerminalState]);

  const refreshActiveConversionState = useCallback(async () => {
    const requestId = ++reconciliationRequestSequenceRef.current;
    const reconciliationGeneration = runtimeMutationGenerationRef.current;
    const generation = ++refreshGenerationRef.current;
    if (!apiClient) {
      if (!mountedRef.current || requestId !== reconciliationRequestSequenceRef.current || reconciliationGeneration !== runtimeMutationGenerationRef.current) {
        return null;
      }
      applyReducerAction({ type: "refreshNoClient" });
      return null;
    }

    try {
      const response = await apiClient.getActiveConversion();
      if (!mountedRef.current || requestId !== reconciliationRequestSequenceRef.current || reconciliationGeneration !== runtimeMutationGenerationRef.current) {
        return null;
      }
      if (generation !== refreshGenerationRef.current) {
        return null;
      }
      // Claim this reconciliation before applying it so a concurrent lookup
      // that captured the same generation cannot land afterward and overwrite
      // the response accepted here.
      runtimeMutationGenerationRef.current += 1;
      lookupFailedRef.current = false;
      onReconciledRef.current?.(response);
      if (response.job) {
        const terminalStatus = normalizeTerminalStatus(response.job.status);
        if (terminalStatus) {
          applyEventPayload({
            event_type: "state",
            job_id: response.job.id,
            state: terminalStatus,
            message: response.job.error || `conversion ${terminalStatus}`,
          });
          return response;
        }
      }
      if (!response.active || !response.job) {
        applyReducerAction({ type: "refreshInactive" });
        return response;
      }

      applyReducerAction({
        type: "refreshActive",
        job: {
          id: response.job.id,
          status: response.job.status,
        },
      });
      return response;
    } catch {
      if (
        !mountedRef.current ||
        requestId !== reconciliationRequestSequenceRef.current ||
        reconciliationGeneration !== runtimeMutationGenerationRef.current ||
        generation !== refreshGenerationRef.current
      ) {
        return null;
      }
      // The reconcile fallback polls every couple of seconds while a cancel is
      // in flight; log only the first failure of a streak so a transient (or
      // persistent) outage cannot spam the log. A later success resets the
      // latch, and the reconcile loop's max-attempts guard still surfaces a
      // cancel that never lands.
      if (!lookupFailedRef.current) {
        lookupFailedRef.current = true;
        pushLog("warn", "active conversion lookup failed");
      }
      return null;
    }
  }, [apiClient, applyEventPayload, applyReducerAction, pushLog]);

  const markConversionStarted = useCallback(
    (jobId: string) => {
      const normalizedJobId = String(jobId ?? "").trim();
      if (!normalizedJobId || !mountedRef.current) {
        return;
      }
      runtimeMutationGenerationRef.current += 1;
      applyReducerAction({ type: "conversionStarted", jobId: normalizedJobId });
    },
    [applyReducerAction],
  );

  const handleStreamOpen = useCallback(() => {
    if (!hasOpenedStreamRef.current) {
      hasOpenedStreamRef.current = true;
      return;
    }
    void refreshActiveConversionState();
  }, [refreshActiveConversionState]);

  const streamState = useEventStreamTransport({
    apiClient,
    baseUrl,
    onLog: pushLog,
    onOpen: handleStreamOpen,
    onPayload: applyEventPayload,
  });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // A client or base-URL swap invalidates any reconciliation still in flight
  // and reopens the stream, so the next open is treated as a first open.
  useEffect(() => {
    refreshGenerationRef.current += 1;
    hasOpenedStreamRef.current = false;
  }, [apiClient, baseUrl]);

  // This hook owns startup reconciliation. Consumers that need the response
  // body take it via onActiveConversionReconciled rather than repeating the
  // lookup, so a mount issues exactly one request.
  useEffect(() => {
    void refreshActiveConversionState();
  }, [refreshActiveConversionState]);

  useEffect(() => {
    if (!modelState.isCancelling || !apiClient) {
      return undefined;
    }
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (attempts > CANCEL_RECONCILE_MAX_ATTEMPTS) {
        window.clearInterval(timer);
        pushLog("warn", "cancellation is taking too long; job state may be stale");
        applyReducerAction({ type: "cancelRejected" });
        return;
      }
      void refreshActiveConversionState();
    }, CANCEL_RECONCILE_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [
    apiClient,
    applyReducerAction,
    modelState.isCancelling,
    pushLog,
    refreshActiveConversionState,
  ]);

  const cancelActiveConversion = useCallback(async () => {
    if (!apiClient) {
      pushLog("warn", "backend is not connected");
      return;
    }

    const attemptId = ++cancellationAttemptSequenceRef.current;
    const lookupGeneration = runtimeMutationGenerationRef.current;
    applyReducerAction({ type: "cancelStart" });

    let targetJobId: string | null = activeJobIdRef.current;
    try {
      if (!targetJobId) {
        const active = await apiClient.getActiveConversion();
        if (!mountedRef.current || attemptId !== cancellationAttemptSequenceRef.current) {
          return;
        }
        if (lookupGeneration !== runtimeMutationGenerationRef.current) {
          pushLog("warn", "cancellation superseded by a newer conversion");
          applyReducerAction({ type: "cancelRejected" });
          return;
        }
        if (active.active && active.job) {
          targetJobId = active.job.id;
          activeJobIdRef.current = targetJobId;
          runtimeMutationGenerationRef.current += 1;
          applyReducerAction({ type: "setActiveJobId", jobId: targetJobId });
        }
      }

      if (!targetJobId) {
        pushLog("warn", "no active job");
        applyReducerAction({ type: "cancelRejected" });
        return;
      }

      if (!mountedRef.current || attemptId !== cancellationAttemptSequenceRef.current) {
        return;
      }
      if (
        activeJobIdRef.current === null ||
        modelStateRef.current.terminalJobIds.includes(targetJobId)
      ) {
        return;
      }
      if (activeJobIdRef.current !== targetJobId) {
        if (mountedRef.current) {
          pushLog("warn", "cancellation superseded by a newer conversion");
          applyReducerAction({ type: "cancelRejected" });
        }
        return;
      }

      const response = await apiClient.cancelConversion(targetJobId);
      if (!mountedRef.current || attemptId !== cancellationAttemptSequenceRef.current) {
        return;
      }
      if (
        activeJobIdRef.current === null ||
        modelStateRef.current.terminalJobIds.includes(targetJobId)
      ) {
        return;
      }
      if (activeJobIdRef.current !== targetJobId) {
        if (mountedRef.current) {
          pushLog("warn", "cancellation superseded by a newer conversion");
          applyReducerAction({ type: "cancelRejected" });
        }
        return;
      }
      if (!response.cancelled) {
        pushLog("error", "cancel failed");
        applyReducerAction({ type: "cancelRejected" });
        return;
      }

      applyReducerAction({
        type: "cancelAccepted",
        jobId: targetJobId,
        logId: nextLogId(),
        fallbackTimestamp: nowTimestamp(),
      });
    } catch (error: unknown) {
      if (
        !mountedRef.current ||
        attemptId !== cancellationAttemptSequenceRef.current ||
        (targetJobId !== null && activeJobIdRef.current !== targetJobId) ||
        (targetJobId === null && lookupGeneration !== runtimeMutationGenerationRef.current)
      ) {
        return;
      }
      const message = error instanceof Error ? error.message : "cancel request failed";
      pushLog("error", message);
      applyReducerAction({ type: "cancelRejected" });
    }
  }, [apiClient, applyReducerAction, nextLogId, pushLog]);

  return useMemo(() => ({
    streamState,
    uiState: modelState.uiState,
    statusText: modelState.statusText,
    progressPct: modelState.progressPct,
    etaText: modelState.etaText,
    stages: modelState.stages,
    currentStage: modelState.currentStage,
    currentMessage: modelState.currentMessage,
    performanceSnapshot: modelState.performanceSnapshot,
    activeJobId: modelState.activeJobId,
    logs: modelState.logs,
    isCancelling: modelState.isCancelling,
    cancelActiveConversion,
    refreshActiveConversionState,
    markConversionStarted,
    // Stable memoized callback from useTerminalStateWaiters; it normalizes the
    // job id itself, so no per-render wrapper is needed.
    waitForTerminalState,
  }), [
    cancelActiveConversion,
    modelState,
    refreshActiveConversionState,
    streamState,
    waitForTerminalState,
  ]);
}
